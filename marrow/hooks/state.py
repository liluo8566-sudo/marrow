"""Per-session hook state files: recall dedup, sticker nudge,
ct cursor, ct_activity, recall logs."""
from __future__ import annotations

import json
import re as _re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .. import config

_RECALL_TZ = config.get_tz()


# ── recall dedup state (per-session, hook-only) ──────────────────────────────

_TABLE_KINDS = {"milestone", "memes", "entity", "diary", "task"}

# Strip WX-injected `[time: ... | gap: ...]` prefix from event content.
# recall.py strips it for the main-hit content; mirror here for neighbors + log.
_WX_TIME_PREFIX_RE = _re.compile(r"^\[time:[^\]]+\]\s*")


def _strip_wx_time_prefix(s: str) -> str:
    return _WX_TIME_PREFIX_RE.sub("", s or "")


def _recall_seen_path(sid: str) -> Path:
    return config.DATA_DIR / "state" / "recall_seen" / f"{sid}.json"


def _load_recall_seen(sid: str) -> set[tuple[str, int]]:
    if not sid:
        return set()
    try:
        data = json.loads(_recall_seen_path(sid).read_text())
        return {(str(k), int(i)) for k, i in data}
    except Exception:
        return set()


def _save_recall_seen(sid: str, seen: set[tuple[str, int]]) -> None:
    if not sid:
        return
    p = _recall_seen_path(sid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(seen)))
    except Exception:
        pass


def _wipe_recall_seen(sid: str) -> None:
    if not sid:
        return
    try:
        _recall_seen_path(sid).unlink(missing_ok=True)
    except Exception:
        pass


def _sticker_nudge_path(sid: str) -> Path:
    return config.DATA_DIR / "state" / "sticker_nudge" / f"{sid}.json"


def _load_sticker_nudge(sid: str) -> dict:
    if not sid:
        return {"turn_count": 0, "last_sticker_turn": 0}
    try:
        return json.loads(_sticker_nudge_path(sid).read_text())
    except Exception:
        return {"turn_count": 0, "last_sticker_turn": 0}


def _save_sticker_nudge(sid: str, state: dict) -> None:
    if not sid:
        return
    p = _sticker_nudge_path(sid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state))
    except Exception:
        pass


def _wipe_sticker_nudge(sid: str) -> None:
    if not sid:
        return
    try:
        _sticker_nudge_path(sid).unlink(missing_ok=True)
    except Exception:
        pass


# ── per-turn ingest cursor (Stop hook) ───────────────────────────────────────
# Mirrors the recall_seen storage pattern: one small json per sid holding the
# last-ingested tail uuid + byte offset, so a long session tail-reads instead
# of re-parsing the whole transcript each turn.

def _ct_cursor_path(sid: str) -> Path:
    return config.DATA_DIR / "state" / "ct_cursor" / f"{sid}.json"


def _load_ct_cursor(sid: str) -> dict | None:
    if not sid:
        return None
    try:
        d = json.loads(_ct_cursor_path(sid).read_text())
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _save_ct_cursor(sid: str, last_uuid: str | None, offset: int) -> None:
    if not sid:
        return
    p = _ct_cursor_path(sid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last_uuid": last_uuid, "offset": offset}))
    except Exception:
        pass


def _ensure_ct_activity(conn: sqlite3.Connection) -> None:
    """Create ct_activity if absent. Cortex C1 collector reads (ts, sid, channel)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ct_activity ("
        " id INTEGER PRIMARY KEY,"
        " ts TEXT NOT NULL,"
        " sid TEXT,"
        " channel TEXT)"
    )


def _write_ct_activity(conn: sqlite3.Connection, sid: str, channel: str) -> None:
    _ensure_ct_activity(conn)
    with conn:
        conn.execute(
            "INSERT INTO ct_activity (ts, sid, channel) VALUES (?, ?, ?)",
            (_now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), sid, channel),
        )


def _recall_log_dir() -> Path:
    """~/.config/marrow/logs/recall/ — created on first use."""
    d = config.DATA_DIR / "logs" / "recall"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _recall_local_date(utc_now: datetime) -> str:
    """UTC datetime → local recall-day string (YYYY-MM-DD), natural midnight."""
    return utc_now.astimezone(_RECALL_TZ).date().isoformat()


def _recall_session_log_path(sid: str, utc_now: datetime) -> Path:
    """Per-session recall log: recall/recall-YYYY-MM-DD-<sid8>.md."""
    day = _recall_local_date(utc_now)
    sid8 = (sid or "unknown")[:8]
    return _recall_log_dir() / f"recall-{day}-{sid8}.md"


def _prune_recall_logs() -> None:
    """Delete recall log files older than today-1 (keep today + yesterday).

    Mirrors digest prune: natural midnight local-day boundary, mtime-based
    safety floor, today/yesterday whitelisted by filename."""
    try:
        now = datetime.now(timezone.utc)
        today = _recall_local_date(now)
        yesterday = _recall_local_date(now - timedelta(days=1))
        cutoff = now.timestamp() - 1.5 * 24 * 3600
        log_dir = _recall_log_dir()
        for f in log_dir.glob("recall-*.md"):
            name = f.stem  # "recall-YYYY-MM-DD-<sid8>"
            parts = name.split("-", 4)  # ["recall", "YYYY", "MM", "DD", "<sid8>"]
            if len(parts) < 5:
                continue
            date_part = "-".join(parts[1:4])
            if date_part in (today, yesterday):
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — prune is best-effort
        pass


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
