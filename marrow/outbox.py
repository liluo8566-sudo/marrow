"""Outbox send/list logic for the `msg` MCP tool (daemon.py delegates here).

One-way cross-channel drop: a session leaves a note for the user (tg/wx) or
for another session (cli / ct / session:<full-sid>). Delivery is at-most-once,
performed by the channel adapters / hooks (P2+), not here. This module only
resolves the target, enforces permission + daily caps, and inserts the row.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3

from . import config, repo, storage
from .timecue import melb_day_range
from .timeutil import _MELB

# targets that count against the user-facing daily cap (real channels)
_USER_TARGETS = ("tg", "wx")


def _channel() -> str:
    """Sender channel of the calling session. MARROW_CHANNEL='ct' is set on the
    cortex subprocess alongside MARROW_CORTEX; fall back to the cortex env
    marker, else default to 'cli' (a plain terminal session carries neither)."""
    ch = (os.environ.get("MARROW_CHANNEL") or "").strip()
    if ch:
        return ch
    if os.environ.get("MARROW_CORTEX"):
        return "ct"
    return "cli"


def _from_sid() -> str | None:
    """Calling session's sid, if cc exposes it to the MCP subprocess env.
    Best-effort metadata (used by list + future replay exclusion); NULL when
    unavailable. Newer cc sets CLAUDE_CODE_SESSION_ID."""
    for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    return None


def _today_utc_range() -> tuple[str, str]:
    """Local-midnight-to-midnight window as (since_utc, until_utc) ISO."""
    today = _dt.datetime.now(_MELB).strftime("%Y-%m-%d")
    return melb_day_range(today)


def _resolve_target(conn: sqlite3.Connection, to: str) -> tuple[str | None, str | None]:
    """Return (stored_target, error). session:<prefix> resolves against sessions
    at send time — exactly one match required."""
    if to in ("tg", "wx", "cli", "ct"):
        return to, None
    if not to.startswith("session:"):
        return None, f"unknown target {to!r} (expected tg/wx/cli/ct/session:<prefix>)"
    prefix = to[len("session:"):].strip()
    if not prefix:
        return None, "session: target needs a sid prefix"
    rows = conn.execute(
        "SELECT sid FROM sessions WHERE substr(sid,1,?) = ? ORDER BY sid",
        (len(prefix), prefix),
    ).fetchall()
    sids = [r["sid"] for r in rows]
    if len(sids) == 0:
        return None, f"no session matches prefix {prefix!r}"
    if len(sids) > 1:
        return None, f"prefix {prefix!r} matches {len(sids)} sessions — narrow it"
    return f"session:{sids[0]}", None


def send(
    to: str,
    text: str,
    *,
    watch_reply: bool = False,
    watch_timeout_min: int | None = None,
    db: str | None = None,
) -> dict:
    """Insert one outbox row. Returns {ok, id} or {ok: False, error}."""
    if not text or not text.strip():
        return {"ok": False, "error": "text is empty"}
    cfg = config.load().get("outbox", {})
    allowed = cfg.get("user_send_channels", ["ct"])
    cap_user = int(cfg.get("daily_cap_user", 30))
    cap_session = int(cfg.get("daily_cap_session", 100))
    channel = _channel()
    from_sid = _from_sid()

    conn = storage.connect(db)
    try:
        stored_target, err = _resolve_target(conn, to)
        if err:
            return {"ok": False, "error": err}
        base = stored_target.split(":", 1)[0]
        is_user = base in _USER_TARGETS
        if is_user and channel not in allowed:
            return {
                "ok": False,
                "error": f"channel {channel!r} not allowed to send to {base}"
                f" (allowed: {allowed})",
            }
        since, until = _today_utc_range()
        cap = cap_user if is_user else cap_session
        watch_state = "armed" if watch_reply else None
        # Count-today + insert in ONE BEGIN IMMEDIATE txn — per-session daemons
        # race the cap otherwise.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            if is_user:
                count = conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE created_at >= ?"
                    " AND created_at < ? AND target IN ('tg','wx')",
                    (since, until),
                ).fetchone()[0]
            else:
                count = conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE created_at >= ?"
                    " AND created_at < ? AND (target='cli' OR target='ct'"
                    " OR target LIKE 'session:%')",
                    (since, until),
                ).fetchone()[0]
            if count >= cap:
                conn.execute("ROLLBACK")
                repo.add_alert(
                    "warn", "outbox_cap",
                    f"outbox:{'user' if is_user else 'session'}",
                    "outbox.send",
                    message=f"daily {'user' if is_user else 'session'} cap"
                    f" {cap} reached ({count} today)",
                    db=db,
                )
                return {
                    "ok": False,
                    "error": f"daily cap {cap} reached for"
                    f" {'user' if is_user else 'session'} notes",
                }
            cur = conn.execute(
                "INSERT INTO outbox (from_sid, from_channel, target, body,"
                " watch_reply, watch_timeout_min, watch_state)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (from_sid, channel, stored_target, text.strip(),
                 1 if watch_reply else 0, watch_timeout_min, watch_state),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"ok": True, "id": cur.lastrowid, "target": stored_target}
    finally:
        conn.close()


def _render_note(header_tmpl: str, row: sqlite3.Row) -> str:
    """One delivered note: config header line + body verbatim."""
    from_sid = row["from_sid"] or ""
    sid4 = from_sid[:4] if from_sid else "????"
    created = row["created_at"] or ""
    try:
        dt = _dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        hhmm = dt.astimezone(_MELB).strftime("%H:%M")
    except Exception:
        hhmm = ""
    header = header_tmpl.format(
        channel=row["from_channel"] or "?", sid4=sid4, time=hhmm
    )
    return f"{header}\n{row['body']}"


def deliver(
    sid: str | None,
    channel: str,
    *,
    is_cortex: bool = False,
    db: str | None = None,
) -> str | None:
    """Claim + render pending notes targeting the current session, mark them
    sent. At-most-once: each row is claimed via a single atomic UPDATE guarded
    on status='pending' (rowcount decides the winner between racing hooks).

    Targets matched:
      - session:<full-sid>  → exact sid match
      - cli                 → any cli session (broadcast, consume-once)
      - ct                  → cortex session only

    Returns the joined note text (newline-separated) or None when nothing
    delivered. A crash between claim and this return drops the note (status
    stays 'claimed' as a forensic trace) — notes are non-critical.
    """
    header_tmpl = str(
        config.load().get("outbox", {}).get("inject_header", "") or ""
    )
    conds = []
    params: list = []
    if sid:
        conds.append("target = ?")
        params.append(f"session:{sid}")
    if channel == "cli":
        conds.append("target = 'cli'")
    if is_cortex:
        conds.append("target = 'ct'")
    if not conds:
        return None

    conn = storage.connect(db)
    try:
        rows = conn.execute(
            "SELECT id FROM outbox WHERE status = 'pending' AND ("
            + " OR ".join(conds) + ") ORDER BY id",
            params,
        ).fetchall()
        delivered: list[str] = []
        for r in rows:
            rid = r["id"]
            with conn:
                cur = conn.execute(
                    "UPDATE outbox SET status = 'claimed' WHERE id = ?"
                    " AND status = 'pending'",
                    (rid,),
                )
            if cur.rowcount != 1:
                continue  # lost the claim race — another hook took it
            full = conn.execute(
                "SELECT id, created_at, from_sid, from_channel, body"
                " FROM outbox WHERE id = ?", (rid,),
            ).fetchone()
            delivered.append(_render_note(header_tmpl, full))
            with conn:
                conn.execute(
                    "UPDATE outbox SET status = 'sent',"
                    " sent_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE id = ?", (rid,),
                )
        return "\n\n".join(delivered) if delivered else None
    finally:
        conn.close()


def list_recent(limit: int = 20, db: str | None = None) -> list[dict]:
    """Own session's pending + recent rows, newest first (debugging)."""
    from_sid = _from_sid()
    conn = storage.connect(db)
    try:
        if from_sid:
            rows = conn.execute(
                "SELECT id, created_at, target, status, body, watch_reply,"
                " watch_state, sent_at, replied_at, reply_text, receipt_seen"
                " FROM outbox WHERE from_sid = ?"
                " ORDER BY id DESC LIMIT ?",
                (from_sid, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, created_at, target, status, body, watch_reply,"
                " watch_state, sent_at, replied_at, reply_text, receipt_seen"
                " FROM outbox"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def prune(conn: sqlite3.Connection, retention_days: int) -> int:
    """DELETE sent/failed rows older than retention_days. Weekly aging pass."""
    cur = conn.execute(
        "DELETE FROM outbox WHERE status IN ('sent','failed')"
        " AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
        (f"-{int(retention_days)} days",),
    )
    return cur.rowcount or 0
