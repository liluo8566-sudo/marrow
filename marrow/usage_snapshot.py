"""OAuth /api/oauth/usage snapshot writer -> ct_rate_limit kv.

Standalone entry point (`python -m marrow.usage_snapshot`), invoked by
cortex's tick collector as a subprocess (own venv/deps, C4 queue item 2).
The stream `rate_limit_event` writer (llm.py:_snapshot_rate_limit) carries
NO utilization percentage — only this separate OAuth HTTP endpoint has
five_hour_pct / seven_day_pct. Minimal replication of synapse_core/usage.py's
OAuth-usage HTTP pattern (marrow must not import synapse — this is our own
MIT code, duplicated intentionally rather than cross-imported).

Writes exactly the keys cortex/bulletin.py (commit 9ee25e5) reads:
five_hour_pct, seven_day_pct, seven_day_reset_at, and five_hour_reset_at
when the endpoint supplies it. Same ct_rate_limit table as the stream
writer — different keys today (*_pct vs *_status/*_reset_at extras),
last-write-wins on any overlap is fine (storage.py v31).

Never fabricates: missing creds / network failure / bad response -> writes
nothing, exits non-zero. Caller (cortex collect_tick) logs the failure.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import storage

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


def fetch_and_write() -> None:
    """Full pipeline: load token -> call endpoint -> parse -> write kv.
    Raises UsageSnapshotError on any failure before writing anything."""
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
