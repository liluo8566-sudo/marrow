"""Cross-window tl sync: surface tl rows written by *other* live sessions.

Injection side (turn_inject): each prompt queries role='tl' rows newer than
this session's last-seen id, excluding this session's own rows, and renders a
`## TL update` fragment. Per-session last-seen id is a state file under
DATA_DIR/state/tl_seen/<sid> (mirrors the tl_nudge counter pattern).

First prompt of a session (no state file) initialises last-seen to the current
max(id) WITHOUT injecting — a fresh window's SessionStart timeline already
covers history; never backfill it here.

Also provides last_tl_hhmm(): the local HH:mm of a session's own most recent
tl row, shared by tl_nudge ({last_tl}) and the tl_add return hint.
"""
from __future__ import annotations

import sqlite3

from . import config
from .timeline import _hhmm_local

_MAX_ROWS = 5


def _cfg() -> dict:
    return config.load().get("tl_sync", {}) or {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def _seen_path(sid: str):
    return config.DATA_DIR / "state" / "tl_seen" / sid


def _load_seen(sid: str) -> int | None:
    try:
        return int(_seen_path(sid).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _save_seen(sid: str, last_id: int) -> None:
    p = _seen_path(sid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(last_id), encoding="utf-8")
    except OSError:
        pass


def _max_tl_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events WHERE role='tl'").fetchone()
    return int(row["m"]) if row else 0


def last_tl_hhmm(conn: sqlite3.Connection, sid: str) -> str:
    """Local HH:mm of this session's most recent tl row (max ts_end, falling
    back to ts_start), or 'n/a' if the session has none."""
    if not sid:
        return "n/a"
    row = conn.execute(
        "SELECT ts_start, ts_end FROM events"
        " WHERE role='tl' AND session_id=?"
        " ORDER BY COALESCE(ts_end, ts_start) DESC, id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if not row:
        return "n/a"
    return _hhmm_local(row["ts_end"] or row["ts_start"])


def render_update(conn: sqlite3.Connection, sid: str) -> str:
    """Cross-window `## TL update` fragment for sid; advances last-seen.

    Returns "" when disabled, on first prompt (init only, no inject), or when
    no newer rows from other sessions exist.
    """
    if not enabled() or not sid:
        return ""

    seen = _load_seen(sid)
    if seen is None:
        # First prompt of this session: initialise without backfilling.
        _save_seen(sid, _max_tl_id(conn))
        return ""

    rows = conn.execute(
        "SELECT id, content, channel, ts_start, ts_end FROM events"
        " WHERE role='tl' AND id > ? AND session_id != ?"
        " ORDER BY id ASC",
        (seen, sid),
    ).fetchall()
    if not rows:
        # Still advance past any own rows written since last seen.
        new_max = _max_tl_id(conn)
        if new_max > seen:
            _save_seen(sid, new_max)
        return ""

    max_id = max(r["id"] for r in rows)
    self_max = _max_tl_id(conn)
    _save_seen(sid, max(max_id, self_max))

    shown = rows[-_MAX_ROWS:]
    dropped = len(rows) - len(shown)
    lines = ["## TL update"]
    if dropped > 0:
        lines.append(f"- +{dropped} more")
    for r in shown:
        ch = (r["channel"] or "?").strip() or "?"
        start = r["ts_start"]
        rng = ""
        if start:
            s = _hhmm_local(start)
            e = _hhmm_local(r["ts_end"]) if r["ts_end"] else None
            rng = (f"{s}-{e} " if e else f"{s} ")
        lines.append(f"- {rng}{r['content']} ({ch})")
    return "\n".join(lines)
