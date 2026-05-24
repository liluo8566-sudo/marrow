"""Weekly maintenance: memes decay, task auto-archive, milestone auto-confirm.

No LLM. Triggered by deploy/mw-aging.plist (Sun 12:00 local). Rules locked
in DECISIONS.md:46 — pinned=1 rows are never aged (full bypass for identity
anchors); hardcoded anchor list is force-pinned every pass (idempotent).

Passes (single txn):
1. promote_memes — ≥3 distinct event hits over last 7d → use_count += hits,
   last_seen = now, status = 'active' (revives dormant rows).
2. demote_memes — last_seen > 90d AND pinned=0 → status = 'dormant'.
3. archive_tasks — status='active' AND 0 mentions in events over last 30d
   → status = 'archived'.
4. confirm_milestone_alerts — alerts.type='milestone_added' AND created_at
   > 7d ago AND resolved=0 → set resolved=1, resolved_at=now.
"""
from __future__ import annotations

import sqlite3
import sys

from . import storage
from .candidates import MEMES_ANCHOR_KEYS as _IDENTITY_ANCHORS


def _fts_phrase(q: str) -> str:
    # Mirror repo._fts_query: phrase match, FTS5-safe (trigram tokenizer).
    return '"' + q.replace('"', '""').strip() + '"'


def enforce_anchor_pins(conn: sqlite3.Connection) -> int:
    """Force pinned=1 on every memes row whose key is in the anchor list.
    Idempotent: returns rows newly flipped (was pinned=0)."""
    if not _IDENTITY_ANCHORS:
        return 0
    placeholders = ",".join("?" * len(_IDENTITY_ANCHORS))
    cur = conn.execute(
        f"UPDATE memes SET pinned = 1 "
        f"WHERE key IN ({placeholders}) AND pinned = 0",
        tuple(_IDENTITY_ANCHORS),
    )
    return cur.rowcount or 0


def promote_memes(conn: sqlite3.Connection) -> int:
    """≥3 distinct event hits over last 7d → bump + revive.

    For each non-pinned memes row (pinned=0 — pinned rows skip aging
    entirely), FTS5-match its key against events from the last 7d. Count
    distinct event_id hits. ≥3 → use_count += hits, last_seen = now,
    status = 'active'. Returns rows promoted.

    Pinned rows are skipped — they never age (DECISIONS:46).
    """
    rows = conn.execute(
        "SELECT id, key FROM memes WHERE pinned = 0"
    ).fetchall()
    promoted = 0
    for r in rows:
        key = (r["key"] or "").strip()
        if not key:
            continue
        try:
            hits = conn.execute(
                "SELECT COUNT(DISTINCT f.rowid) FROM events_fts f "
                "JOIN events e ON e.id = f.rowid "
                "WHERE events_fts MATCH ? "
                "AND e.timestamp >= datetime('now', '-7 days')",
                (_fts_phrase(key),),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            # Malformed FTS expression (rare for CJK trigrams) — skip silently.
            continue
        if hits >= 3:
            conn.execute(
                "UPDATE memes SET use_count = use_count + ?, "
                "last_seen = strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
                "status = 'active' WHERE id = ?",
                (hits, r["id"]),
            )
            promoted += 1
    return promoted


def demote_memes(conn: sqlite3.Connection) -> int:
    """last_seen > 90d ago AND pinned=0 AND status != 'dormant' → dormant.

    Rows with NULL last_seen are skipped — never auto-demoted until they
    have at least been seen once. Pinned rows are skipped (DECISIONS:46).
    """
    cur = conn.execute(
        "UPDATE memes SET status = 'dormant' "
        "WHERE pinned = 0 "
        "AND status != 'dormant' "
        "AND last_seen IS NOT NULL "
        "AND last_seen < datetime('now', '-90 days')"
    )
    return cur.rowcount or 0


def archive_tasks(conn: sqlite3.Connection) -> int:
    """status='active' tasks with 0 event mentions in last 30d → archived.

    Mention = FTS5 phrase match of tasks.title against events.content from
    the last 30d. Empty/whitespace titles are skipped (cannot mention).
    """
    rows = conn.execute(
        "SELECT id, title FROM tasks WHERE status = 'active'"
    ).fetchall()
    archived = 0
    for r in rows:
        title = (r["title"] or "").strip()
        if not title:
            continue
        try:
            hits = conn.execute(
                "SELECT COUNT(*) FROM events_fts f "
                "JOIN events e ON e.id = f.rowid "
                "WHERE events_fts MATCH ? "
                "AND e.timestamp >= datetime('now', '-30 days') "
                "LIMIT 1",
                (_fts_phrase(title),),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if hits == 0:
            conn.execute(
                "UPDATE tasks SET status = 'archived', "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE id = ?",
                (r["id"],),
            )
            archived += 1
    return archived


def confirm_milestone_alerts(conn: sqlite3.Connection) -> int:
    """milestone_added alerts older than 7d AND unresolved → confirmed.

    The alerts table has no `status` / `dismissed_at`; resolved=1 +
    resolved_at=now is the canonical confirmation per existing semantics
    (storage.py:104-113, repo.open_alerts filters resolved=0).
    """
    cur = conn.execute(
        "UPDATE alerts SET resolved = 1, "
        "resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
        "WHERE type = 'milestone_added' "
        "AND resolved = 0 "
        "AND created_at < datetime('now', '-7 days')"
    )
    return cur.rowcount or 0


def main() -> None:
    """Single entrypoint: run all four passes inside one txn, log summary."""
    conn = storage.init_db()
    try:
        with conn:
            enforce_anchor_pins(conn)
            promoted = promote_memes(conn)
            demoted = demote_memes(conn)
            archived = archive_tasks(conn)
            confirmed = confirm_milestone_alerts(conn)
            conn.execute(
                "INSERT INTO audit_log "
                "(target_table, target_id, action, summary) "
                "VALUES ('aging', NULL, 'weekly', ?)",
                (f"promoted={promoted} demoted={demoted} "
                 f"archived={archived} confirmed={confirmed}",),
            )
        sys.stderr.write(
            f"[aging] promoted={promoted} demoted={demoted} "
            f"archived={archived} confirmed={confirmed}\n"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
