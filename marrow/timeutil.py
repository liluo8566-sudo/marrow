"""Timezone conversion helpers for read-out boundaries.

DB stores timestamps as UTC ISO strings. These helpers convert to Melbourne
local time at read boundaries only — storage is never modified.
"""
from __future__ import annotations

import datetime

from . import config as _config

_MELB = _config.get_tz()


def format_recall_ts(s: str, *, now: datetime.datetime | None = None) -> str:
    """Return '[MM-DD Day · Xd ago]' label for a UTC ISO timestamp string.

    Absolute part: MM-DD Day in Melbourne local time (e.g. 06-08 Mon).
    Relative part: <1h -> 'Xm ago' or 'just now'; <24h -> 'Xh ago';
                   <14d -> 'Xd ago'; <8w -> 'Xw ago'; else 'Xmo ago'.
    `now` defaults to datetime.now(timezone.utc) — injectable for tests.
    Falls back to raw slice on parse error.
    """
    if not s:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        local = dt.astimezone(_MELB)
        abs_part = local.strftime("%m-%d %a")
        ref = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
        delta = ref - dt
        secs = delta.total_seconds()
        if secs < 60:
            rel = "just now"
        elif secs < 3600:
            rel = f"{int(secs // 60)}m ago"
        elif secs < 86400:
            rel = f"{int(secs // 3600)}h ago"
        elif secs < 14 * 86400:
            rel = f"{int(secs // 86400)}d ago"
        elif secs < 8 * 7 * 86400:
            rel = f"{int(secs // (7 * 86400))}w ago"
        else:
            rel = f"{int(secs // (30 * 86400))}mo ago"
        return f"[{abs_part} · {rel}]"
    except Exception:
        return f"[{s[:10]}]"


def reltime_short(s: str, *, now: datetime.datetime | None = None) -> str:
    """Return a short time label for a UTC ISO timestamp — no 'ago' suffix.

    <24h -> 'Xh'; <7d -> 'Xd'; 7d-365d -> 'MM-DD' (Melbourne local);
    >=365d -> 'YYYY' (Melbourne local year). Distinct from format_recall_ts's
    longer '[MM-DD Day · Xd ago]' label — used where a compact single token
    is needed (event-row recall header short format).
    `now` defaults to datetime.now(timezone.utc) — injectable for tests.
    Falls back to '' on empty input / parse error.
    """
    if not s:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        ref = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
        secs = max(0.0, (ref - dt).total_seconds())
        local = dt.astimezone(_MELB)
        if secs < 86400:
            return f"{int(secs // 3600)}h"
        if secs < 7 * 86400:
            return f"{int(secs // 86400)}d"
        if secs < 365 * 86400:
            return local.strftime("%m-%d")
        return local.strftime("%Y")
    except Exception:
        return ""


def utc_iso_to_local_date(s: str) -> str:
    """Parse a UTC ISO string and return YYYY-MM-DD in Melbourne local time.

    Falls back to slicing the first 10 chars if parsing fails (preserves
    existing behaviour for already-local or malformed strings).
    """
    if not s:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(_MELB).strftime("%Y-%m-%d")
    except Exception:
        return s[:10]


def utc_iso_to_local_datetime(s: str) -> str:
    """Parse a UTC ISO string and return YYYY-MM-DD HH:MM in Melbourne local time.

    Falls back to slicing the first 16 chars (replacing T with space) on error.
    """
    if not s:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(_MELB).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s[:16].replace("T", " ")
