"""Weekly maintenance: memes decay, task auto-archive, milestone auto-confirm.

No LLM. Triggered by deploy/mw-aging.plist (Sun 12:00 local).

Memes aging under v2 type enum (Lumi 2026-05-25):
- Entry gate already enforces ≥3/7d for meme/news/event (candidates.py).
- Anything in memes is by definition active — no promote/dormant pass.
- paw / fact land pinned=1 → never aged.
- meme / news / event / others land pinned=0 → 90d after last_seen → DELETE.

Passes (single txn):
1. retire_memes — last_seen > 90d AND pinned=0 → DELETE.
2. archive_tasks — status='active' AND 0 mentions in events over last 30d
   → status = 'archived'.
3. confirm_milestone_alerts — alerts.type='milestone_added' AND created_at
   > 7d ago AND resolved=0 → set resolved=1, resolved_at=now.
"""
from __future__ import annotations

import sqlite3
import sys

from . import storage


def _fts_phrase(q: str) -> str:
    # Mirror repo._fts_query: phrase match, FTS5-safe (trigram tokenizer).
    return '"' + q.replace('"', '""').strip() + '"'


def retire_memes(conn: sqlite3.Connection) -> int:
    """last_seen > 90d AND pinned=0 → DELETE.

    Rows with NULL last_seen are skipped (never seen → not yet decayable).
    Pinned rows (paw / fact) are skipped — they never age.
    """
    cur = conn.execute(
        "DELETE FROM memes "
        "WHERE pinned = 0 "
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
    """Single entrypoint: run all three passes inside one txn, log summary."""
    conn = storage.init_db()
    try:
        with conn:
            retired = retire_memes(conn)
            archived = archive_tasks(conn)
            confirmed = confirm_milestone_alerts(conn)
            conn.execute(
                "INSERT INTO audit_log "
                "(target_table, target_id, action, summary) "
                "VALUES ('aging', NULL, 'weekly', ?)",
                (f"retired={retired} archived={archived} "
                 f"confirmed={confirmed}",),
            )
        sys.stderr.write(
            f"[aging] retired={retired} archived={archived} "
            f"confirmed={confirmed}\n"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
