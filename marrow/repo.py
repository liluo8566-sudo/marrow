"""Query + write layer over the SQLite store. Daemon tools and hooks call
here; schema/connection stay in storage.py. Deterministic, no LLM.
"""
from __future__ import annotations

import hashlib
import sqlite3

from . import storage
from . import recall as _recall_mod


def _fts_query(q: str) -> str:
    # Phase 1: phrase match, FTS5-safe. Multi-term ranking is Pending.
    return '"' + q.replace('"', '""').strip() + '"'


def recall(conn: sqlite3.Connection, query: str, limit: int = 10,
           budget_chars: int = 4000) -> list[dict]:
    """Recall past events. Uses fusion (vec+bm25+recency+affect) when
    bge-m3 is loaded; falls back to FTS5-only when embedder is absent."""
    return _recall_mod.recall_fusion(
        conn, query, limit=limit, budget_chars=budget_chars
    )


def open_tasks(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, category, title, due, next_step, last_session_summary "
        "FROM tasks WHERE status = 'active' "
        "ORDER BY (due IS NULL), due, created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def open_alerts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, severity, type, message, source "
        "FROM alerts WHERE resolved = 0 "
        "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 "
        "ELSE 2 END, created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def handoff(conn: sqlite3.Connection) -> dict:
    # Phase 1 session-start payload: open tasks + open alerts only.
    # No who-i-am/persona (static CLAUDE.md layer); emotion is Phase 2.
    return {"tasks": open_tasks(conn), "alerts": open_alerts(conn)}


def add_alert(severity: str, atype: str, message: str,
              source: str | None = None, *, db: str | None = None) -> int:
    # on_alert sink for LLMClient: self-contained connection so it works
    # from any context (pipeline, hook, daemon). Mirrors to audit_log.
    conn = storage.connect(db)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO alerts (severity, type, message, source) "
                "VALUES (?, ?, ?, ?)",
                (severity, atype, message, source),
            )
            aid = cur.lastrowid
            conn.execute(
                "INSERT INTO audit_log "
                "(target_table, target_id, action, summary) "
                "VALUES ('alerts', ?, 'insert', ?)",
                (str(aid), f"{severity}/{atype}: {message[:120]}"),
            )
        return aid
    finally:
        conn.close()


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def archive_events(conn: sqlite3.Connection, rows: list[dict]) -> int:
    # Write path for #7 SessionEnd. Idempotent by source_hash; re-run skips.
    n = 0
    sessions: set[str] = set()
    with conn:
        for r in rows:
            h = _hash(r["session_id"], r["timestamp"], r["role"],
                      r["content"])
            if conn.execute(
                "SELECT 1 FROM events WHERE source_hash = ?", (h,)
            ).fetchone():
                continue
            conn.execute(
                "INSERT INTO events "
                "(session_id, timestamp, role, content, channel, source_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (r["session_id"], r["timestamp"], r["role"], r["content"],
                 r.get("channel"), h),
            )
            sessions.add(r["session_id"])
            n += 1
        # One batch audit row per call (Monitor Zone), atomic with the inserts.
        # Skip when n == 0 so a fully-deduped re-run shows no phantom archive.
        if n:
            target = next(iter(sessions)) if len(sessions) == 1 else str(len(sessions))
            conn.execute(
                "INSERT INTO audit_log "
                "(target_table, target_id, action, summary) "
                "VALUES ('events', ?, 'insert', ?)",
                (target, f"archived {n} events ({len(sessions)} sessions)"),
            )
    return n
