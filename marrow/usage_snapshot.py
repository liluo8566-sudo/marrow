"""Usage snapshot collector -> ct_rate_limit kv.

Standalone entry point (`python -m marrow.usage_snapshot`). Primary carrier is
marrow's own watcher (sync_loop.UsageSnapshotLoop, ~5min tick) — always alive,
independent of cortex, so usage stays fresh even when cortex is off. Cortex's
tick collector may also call it as a subprocess; either caller's write is an
idempotent upsert, so overlap is harmless. Single writer of the usage kv every
consumer (wakeup note render, SessionStart line, in-window threshold inject,
dashboard) reads.

Writes:
- five_hour_pct / seven_day_pct + *_reset_at: Anthropic OAuth /api/oauth/usage
  (utilization %, statusline 口径). Minimal own-code replication of
  synapse_core/usage.py (no cross-import; MIT-duplicated intentionally).
- cdx_five_hour_pct / cdx_seven_day_pct: Codex quota, read from
  ~/.codex/auth.json -> chatgpt usage endpoint (mirrors ~/.claude/statusline.py).
- today_net_tokens: global net spend today (cacheCreation + output) via ccusage,
  when runnable — else that key is simply skipped (line drops in consumers).

Each source is independent: a cdx/ccusage failure never blocks the Anthropic
usage write, and vice-versa. Never fabricates a value. Exits non-zero only when
the primary Anthropic usage write produced nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import storage

CDX_AUTH = Path.home() / ".codex" / "auth.json"
CDX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "marrow (claude-code-oauth)"
KEYCHAIN_SERVICE = "Claude Code-credentials"
FALLBACK_CREDS = Path.home() / ".claude" / ".credentials.json"
HTTP_TIMEOUT_SEC = 10.0


class UsageSnapshotError(Exception):
    """Any failure path (no creds, network, bad json). Caller must never
    write fabricated values when this is raised."""


def _load_token() -> str | None:
    """macOS keychain first, then `~/.claude/.credentials.json` fallback.
    Mirrors synapse_core/usage.py:_load_token."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=3.0, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            tok = _extract_token(out.stdout.strip())
            if tok:
                return tok
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        text = FALLBACK_CREDS.read_text()
    except OSError:
        return None
    try:
        return _extract_token(text)
    except json.JSONDecodeError:
        return None


def _extract_token(blob: str) -> str | None:
    data = json.loads(blob)
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    tok = oauth.get("accessToken")
    return tok if isinstance(tok, str) and tok else None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA_HEADER,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def _http_get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read() or b""
        except Exception:
            body = b""
        return e.code, body


def _window_pct(window: object) -> float | None:
    if not isinstance(window, dict):
        return None
    v = window.get("utilization")
    if isinstance(v, bool):  # bool is int — exclude defensively
        return None
    return float(v) if isinstance(v, (int, float)) else None


def _window_reset_iso(window: object) -> str | None:
    if not isinstance(window, dict):
        return None
    s = window.get("resets_at")
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _rows_from_usage(data: dict) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    five_hour = data.get("five_hour")
    seven_day = data.get("seven_day")

    pct = _window_pct(five_hour)
    if pct is not None:
        rows.append(("five_hour_pct", str(pct)))
    reset = _window_reset_iso(five_hour)
    if reset is not None:
        rows.append(("five_hour_reset_at", reset))

    pct = _window_pct(seven_day)
    if pct is not None:
        rows.append(("seven_day_pct", str(pct)))
    reset = _window_reset_iso(seven_day)
    if reset is not None:
        rows.append(("seven_day_reset_at", reset))

    return rows


def _write_kv_rows(rows: list[tuple[str, str]]) -> None:
    """Upsert (key, value) pairs into ct_rate_limit, latest-wins. Raises on
    DB failure — caller decides whether that's fatal for exit code purposes."""
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = storage.connect()
    try:
        with conn:
            conn.executemany(
                "INSERT INTO ct_rate_limit (key, value, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " value=excluded.value, updated_at=excluded.updated_at",
                [(k, v, now) for k, v in rows],
            )
    finally:
        conn.close()


def _codex_rows() -> list[tuple[str, str]]:
    """Codex 5h/7d used-% from ~/.codex/auth.json + chatgpt usage endpoint.
    Best-effort — any failure returns no rows (line drops in consumers)."""
    try:
        auth = json.loads(CDX_AUTH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    tok = auth.get("tokens", {}) if isinstance(auth, dict) else {}
    access = tok.get("access_token")
    if not access:
        return []
    req = urllib.request.Request(
        CDX_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": tok.get("account_id", ""),
            "originator": "codex_cli_rs",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
    except Exception:
        return []
    rl = data.get("rate_limit", {}) if isinstance(data, dict) else {}
    rows: list[tuple[str, str]] = []
    pw = rl.get("primary_window") or {}
    if isinstance(pw.get("used_percent"), (int, float)):
        rows.append(("cdx_five_hour_pct", str(float(pw["used_percent"]))))
    sw = rl.get("secondary_window") or {}
    if isinstance(sw.get("used_percent"), (int, float)):
        rows.append(("cdx_seven_day_pct", str(float(sw["used_percent"]))))
    return rows


def _today_net_rows() -> list[tuple[str, str]]:
    """Global net spend today (cacheCreation + output) via ccusage, when it is
    runnable. Best-effort — no ccusage / any failure returns no rows."""
    day = time.strftime("%Y%m%d")
    try:
        out = subprocess.run(
            ["npx", "--yes", "ccusage@latest", "daily", "--json", "--since", day],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0 or not out.stdout.strip():
        return []
    try:
        data = json.loads(out.stdout)
        rows_in = data.get("daily", []) if isinstance(data, dict) else []
    except (ValueError, TypeError):
        return []
    if not rows_in:
        return []
    d = rows_in[-1]
    net = int(d.get("cacheCreationTokens", 0) or 0) + int(d.get("outputTokens", 0) or 0)
    if net <= 0:
        return []
    return [("today_net_tokens", str(net))]


def fetch_and_write() -> None:
    """Full pipeline: Anthropic usage (required) + Codex quota + today net
    (both best-effort). Raises UsageSnapshotError only when the required
    Anthropic write produced nothing — a partial-source outage still writes
    whatever succeeded."""
    token = _load_token()
    if not token:
        raise UsageSnapshotError("no oauth token available")

    try:
        status, body = _http_get(USAGE_URL, _headers(token))
    except Exception as e:
        raise UsageSnapshotError(f"http error: {e}") from e

    if status != 200:
        raise UsageSnapshotError(f"http {status}")

    try:
        data = json.loads(body)
    except Exception as e:
        raise UsageSnapshotError(f"bad json: {e}") from e

    if not isinstance(data, dict):
        raise UsageSnapshotError("usage response not a JSON object")

    rows = _rows_from_usage(data)
    if not rows:
        raise UsageSnapshotError("no usable window data in usage response")

    try:
        rows += _codex_rows()
    except Exception:  # best-effort — never block the primary write
        pass
    try:
        rows += _today_net_rows()
    except Exception:
        pass

    _write_kv_rows(rows)


def main() -> int:
    try:
        fetch_and_write()
    except UsageSnapshotError as e:
        print(f"usage_snapshot: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # unexpected — still never write, still fail loud
        print(f"usage_snapshot: unexpected error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
