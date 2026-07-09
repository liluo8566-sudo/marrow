"""Usage rendering + agent-token transcript scan (single source for all
sessions).

Consumers: SessionStart Plan Used line, turn_inject in-window threshold line,
cortex 亮牌 gate, lie_down deny guard. The collector (usage_snapshot) writes the
ct_rate_limit kv this module reads. Window OCCUPANCY (the `main` figure in the
threshold line — same metric as statusline `total` and the rotate/fuse
thresholds) is scanned by hooks._window_tokens_from_transcript, not here — no
second occupancy scanner. This module owns only the agent-token scan (shared
by turn_inject) and the line renderers.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import storage, config

_SUBAGENT_RE = re.compile(r"subagent_tokens[:>]?\s*([0-9,]+)")


# --------------------------------------------------------------------------- #
# transcript scan (shared)
# --------------------------------------------------------------------------- #

def agent_tokens_from_transcript(tpath: str) -> int:
    """Accumulated subagent (Task tool) token total, scanned off user/attachment
    lines' `subagent_tokens` markers. Mirrors ~/.claude/statusline.py. 0 on any
    missing/unreadable transcript."""
    if not tpath:
        return 0
    try:
        lines = open(tpath, encoding="utf-8").read().splitlines()
    except OSError:
        return 0
    total = 0
    for line in lines:
        if '"type":"user"' in line or '"type":"attachment"' in line:
            for m in _SUBAGENT_RE.finditer(line):
                total += int(m.group(1).replace(",", ""))
    return total


# --------------------------------------------------------------------------- #
# kv read
# --------------------------------------------------------------------------- #

def read_kv() -> dict[str, str]:
    """Whole ct_rate_limit kv the collector maintains. {} on any failure."""
    try:
        conn = storage.connect(config.db_path())
    except Exception:
        return {}
    try:
        rows = conn.execute("SELECT key, value FROM ct_rate_limit").fetchall()
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


def _as_float(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _local_hm(iso: str) -> str | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.astimezone(config.get_tz()).strftime("%H:%M")


def _countdown(iso: str) -> str | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return None
    days, rem = divmod(secs, 86400)
    hours = rem // 3600
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{(rem % 3600) // 60}m" if (rem % 3600) // 60 else f"{hours}h"
    return f"{(rem % 3600) // 60}m"


# --------------------------------------------------------------------------- #
# line renderers
# --------------------------------------------------------------------------- #

def _plan_used_segments(kv: dict, with_cdx: bool) -> list[str]:
    """5h/7d/cdx segments rendered straight from the kv the collector writes.
    The watcher's UsageSnapshotLoop keeps the kv fresh; a dead watcher is an ops
    problem, not a rendering one — usage is never hidden by snapshot age."""
    parts: list[str] = []
    five = _as_float(kv.get("five_hour_pct"))
    if five is not None:
        seg = f"5h {five:.0f}%"
        hm = _local_hm(kv.get("five_hour_reset_at", "")) if kv.get("five_hour_reset_at") else None
        if hm:
            seg += f" ({hm})"
        parts.append(seg)
    seven = _as_float(kv.get("seven_day_pct"))
    if seven is not None:
        seg = f"7d {seven:.0f}%"
        cd = _countdown(kv.get("seven_day_reset_at", "")) if kv.get("seven_day_reset_at") else None
        if cd:
            seg += f" ({cd})"
        parts.append(seg)
    if with_cdx:
        cp = _as_float(kv.get("cdx_five_hour_pct"))
        cs = _as_float(kv.get("cdx_seven_day_pct"))
        if cp is not None and cs is not None:
            parts.append(f"cdx 5h {cp:.0f}% 7d {cs:.0f}%")
        elif cp is not None:
            parts.append(f"cdx 5h {cp:.0f}%")
    return parts


def sessionstart_lines(kv: dict | None = None) -> list[str]:
    """SessionStart usage block (plan §二):
    `Plan Used: 5h .. | 7d .. | cdx ..` and `Net Token Used today: 1.2M`.
    Empty list when no data at all."""
    kv = read_kv() if kv is None else kv
    lines: list[str] = []
    segs = _plan_used_segments(kv, with_cdx=True)
    if segs:
        lines.append("Plan Used: " + " | ".join(segs))
    today = _as_float(kv.get("today_net_tokens"))
    if today is not None:
        lines.append(f"Net Token Used today: {_fmt_tokens(int(today))}")
    return lines


def threshold_line(main_occupancy: int, agent_net: int, kv: dict | None = None) -> str:
    """In-window threshold line (plan §二):
    `Plan Used: 5h 20% (04:50) | Net Session Token: main 70k agent 120k`.
    `main` is WINDOW OCCUPANCY (statusline `total` / rotate-fuse metric), not
    cumulative net-spend — the label kept its plan-approved wording. `agent` is
    cumulative subagent_tokens, always live-computed. 5h/7d segments render
    straight from the kv."""
    kv = read_kv() if kv is None else kv
    segs = _plan_used_segments(kv, with_cdx=False)
    net_seg = f"Net Session Token: main {main_occupancy // 1000}k agent {agent_net // 1000}k"
    segs.append(net_seg)
    return "Plan Used: " + " | ".join(segs)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n // 1000}k"
