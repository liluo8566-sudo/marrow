"""Timezone conversion helpers for read-out boundaries.

DB stores timestamps as UTC ISO strings. These helpers convert to Melbourne
local time at read boundaries only — storage is never modified.
"""
from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

_MELB = ZoneInfo("Australia/Melbourne")


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
