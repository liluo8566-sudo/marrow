"""Per-session hook state files: recall dedup, sticker nudge,
ct cursor, outbound cursor, ct_activity, recall logs."""
from __future__ import annotations

import json
import re as _re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .. import config, replay, storage, transcript

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


# ── own-channel outbound-note cursor (turn_inject, F6) ───────────────────────
# One file per sid holding the last-rendered outbox.sent_at, same dir pattern as
# replay. Absent = first sight → seed to MAX(sent_at), future-only. Advance is
# monotonic forward-only (cutoff = max sent_at of the rendered subset), so a
# note surfaced once is never re-injected.

def _outbound_cursor_path(sid: str) -> Path:
    return config.DATA_DIR / "state" / "outbound" / f"{sid}"


def _load_outbound_cursor(sid: str) -> str | None:
    """Cursor value, or None only when never seeded (file absent). A seeded but
    empty baseline (first sight with no notes yet) returns "" — distinct from
    None so it is not re-seeded and past notes surface once."""
    if not sid:
        return None
    try:
        return _outbound_cursor_path(sid).read_text().strip()
    except Exception:
        return None


def _save_outbound_cursor(sid: str, sent_at: str) -> None:
    """Persist the cursor. An empty string is a valid seed (file present but no
    baseline yet) — write it so the sid counts as seeded."""
    if not sid:
        return
    p = _outbound_cursor_path(sid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(sent_at or ""))
    except Exception:
        pass


def _outbound_notes(sid: str, channel: str) -> str:
    """F6 own-channel note visibility. A bridge sends an outbound note on a
    channel (e.g. cortex→tg) straight to the wire, bypassing that channel's
    resident session — so she later replies to a note it never saw. This surfaces
    the notes bridge-delivered on THIS channel since the per-sid cursor, so the
    resident session sees them before her reply.

    Reads outbox rows with target=channel, status='sent', sent_at past the cursor
    (already-delivered outbound notes only — never her replies). First sight seeds
    the cursor to MAX(sent_at) (future-only, no backfill). Advance is monotonic
    forward-only to the max rendered sent_at, so a note is surfaced exactly once.
    Data already in outbox — no writes to the DB, no transcript writes."""
    if not sid or not channel:
        return ""
    cfg = (config.load().get("outbox", {}) or {})
    if channel not in (cfg.get("wire_channels", ["tg", "wx"]) or []):
        return ""  # cli/ct/session are delivered inline by outbox.deliver
    header = cfg.get("own_note_header", "📤 Sent on this channel")
    per_chars = int(
        (config.load().get("replay", {}) or {}).get("per_msg_chars", 150)
    )
    conn = storage.connect(config.db_path())
    try:
        since = _load_outbound_cursor(sid)
        if since is None:
            row = conn.execute(
                "SELECT MAX(sent_at) AS m FROM outbox"
                " WHERE target = ? AND status = 'sent'",
                (channel,),
            ).fetchone()
            seed = (row["m"] if row else None) or ""
            _save_outbound_cursor(sid, seed or "")
            return ""  # first sight — future-only, never backfill
        where_since = " AND sent_at > ?" if since else ""
        params = (channel,) + ((since,) if since else ())
        rows = conn.execute(
            "SELECT id, body, sent_at FROM outbox"
            " WHERE target = ? AND status = 'sent' AND sent_at IS NOT NULL"
            + where_since + " ORDER BY sent_at ASC, id ASC",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return ""
    finally:
        conn.close()

    if not rows:
        return ""

    tz = config.get_tz()
    lines = [header]
    cutoff = since
    for r in rows:
        sent_at = r["sent_at"]
        if sent_at and (not cutoff or str(sent_at) > str(cutoff)):
            cutoff = sent_at
        hm = replay.local_hm(sent_at, tz)
        body = replay.truncate(transcript.strip_media_markers(r["body"]) or "", per_chars)
        lines.append(f"[{hm}] {body}")

    # Monotonic forward-only advance to the max rendered sent_at (F8 semantics).
    if cutoff and (not since or str(cutoff) > str(since)):
        _save_outbound_cursor(sid, str(cutoff))

    return "\n".join(lines)


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
