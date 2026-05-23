"""Daily catchup: pending-day scan + 6AM boundary + fcntl lock.

Called by both daily.run (19:00 launchd) and sessionstart_catchup. Pure
read-only utility — never writes to the diary table itself.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

_TZ = ZoneInfo("Australia/Melbourne")
_CUTOFF_H = 6  # 6AM boundary (was 4AM at diary.py:319)

CATCHUP_WINDOW_DAYS = 7
CATCHUP_MAX = 3


def _to_local(utc_iso: str) -> _dt.datetime:
    s = utc_iso.strip().replace("Z", "+00:00")
    d = _dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_TZ)


def diary_day(utc_iso: str) -> str:
    """UTC ISO → local diary day. 00:00–05:59 belongs to previous day."""
    return (_to_local(utc_iso)
            - _dt.timedelta(hours=_CUTOFF_H)).date().isoformat()


def routine_target() -> str:
    """Last fully-closed diary day. Run at 07:00 → yesterday."""
    now = _dt.datetime.now(_TZ)
    cur = (now - _dt.timedelta(hours=_CUTOFF_H)).date()
    return (cur - _dt.timedelta(days=1)).isoformat()


def _scan_rows(conn, window_days: int) -> list[dict]:
    cutoff = (_dt.date.today()
              - _dt.timedelta(days=window_days + 2)).isoformat()
    rows = conn.execute(
        "SELECT session_id, role, content, timestamp FROM events "
        "WHERE timestamp >= ? AND timestamp != '' ORDER BY timestamp, id",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def pending_days(conn, window_days: int = CATCHUP_WINDOW_DAYS) -> list[str]:
    """Days in [today-window, last-closed] that have events but no diary."""
    floor = (_dt.date.today()
             - _dt.timedelta(days=window_days)).isoformat()
    done = {r["date"] for r in conn.execute("SELECT date FROM diary")}
    days = {diary_day(r["timestamp"]) for r in _scan_rows(conn, window_days)}
    ceil = routine_target()
    return sorted(d for d in days
                  if floor <= d <= ceil and d not in done)


def day_events(conn, date: str) -> list[dict]:
    out = []
    for r in _scan_rows(conn, CATCHUP_WINDOW_DAYS + 1):
        if diary_day(r["timestamp"]) == date:
            out.append({"session_id": r["session_id"], "role": r["role"],
                        "content": r["content"],
                        "timestamp": r["timestamp"]})
    return out


def has_diary(conn, date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM diary WHERE date = ?", (date,)
    ).fetchone() is not None


@contextlib.contextmanager
def app_lock(path: str | None = None, *, blocking: bool = True):
    """fcntl.flock for routine/catchup/manual serialisation."""
    lf = path or str(Path(config.DATA_DIR) / "daily.lock")
    Path(lf).parent.mkdir(parents=True, exist_ok=True)
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    fd = open(lf, "a")
    try:
        fcntl.flock(fd.fileno(), flags)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
