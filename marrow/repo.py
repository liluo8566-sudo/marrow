"""Query + write layer over the SQLite store. Daemon tools and hooks call
here; schema/connection stay in storage.py. Deterministic, no LLM.
"""
from __future__ import annotations

import hashlib
import sqlite3

from . import storage
from . import recall as _recall_mod
from . import top_sections as _top_sections
from . import entity_recall as _entity_recall
from . import candidates as _candidates


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
        "ELSE 2 END, created_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def archived_today(conn: sqlite3.Connection) -> list[dict]:
    """Tasks done since today's 6AM cutoff, sorted by updated_at ASC."""
    cutoff = _top_sections._day_cutoff_utc()
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT id, category, title, updated_at FROM tasks "
        "WHERE status = 'done' AND updated_at >= ? "
        "ORDER BY updated_at ASC",
        (cutoff_iso,),
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
    # Idempotent: if an unresolved alert with the same (severity, type,
    # message, source) already exists, return its id without inserting —
    # stops legacy full-render etc. from breeding hundreds of dupes.
    conn = storage.connect(db)
    try:
        existing = conn.execute(
            "SELECT id FROM alerts"
            " WHERE severity=? AND type=? AND message=?"
            " AND COALESCE(source,'')=COALESCE(?,'') AND resolved=0"
            " LIMIT 1",
            (severity, atype, message, source),
        ).fetchone()
        if existing is not None:
            return existing["id"]
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
    inserted: list[dict] = []
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
            inserted.append(r)
            n += 1
        # Bump entities.mention_count for entities whose name/alias appears
        # in newly-inserted events. Same transaction = atomic with inserts;
        # dedup-aware (only `inserted`, not `rows`) so re-runs don't double-count.
        if inserted:
            _entity_recall.bump_mention_counts(conn, inserted)
            # Same pattern for memes.use_count — meme key substring match,
            # one bump per event per meme. recall_fusion._memes_candidates
            # reads use_count as the heat score for the meme lane.
            _candidates.bump_use_counts(conn, inserted)
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
