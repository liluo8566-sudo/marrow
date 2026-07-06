"""Weekly maintenance: task auto-archive, milestone auto-confirm, vec eviction.

No LLM. Triggered by deploy/mw-aging.plist (Sun 12:00 local).

Memes are permanent — no aging/DELETE pass (Lumi 2026-07-06).

Passes (single txn):
1. archive_tasks — status='active' AND 0 mentions in events over last 30d
   → status = 'archived'.
2. confirm_milestone_alerts — alerts.type='milestone_added' AND created_at
   > 7d ago AND resolved=0 → set resolved=1, resolved_at=now.
3. prune_md_index_tombstones — DELETE md_index rows whose tombstone_at is
   older than 30 days. Stops the table accumulating dead rows from blocks
   the user permanently removed.
4. prune_projects_worktrees — delete every ~/.claude/projects/<slug>
   directory whose name contains "worktrees". cc auto-cleans jsonl 30d+
   but leaves the slug shells; worktree sessions are task-isolated and
   not part of the user's continuous memory, so the whole shell goes.
5. evict_vec_window — DELETE events_vec + events_vec_meta rows whose
   events.timestamp is older than vec_window_days (config). Exempt rows:
   affect-linked (importance>=3) or recall_count>0. Safety caps abort the
   pass if eviction would exceed 25% of vec rows or 10000 rows. Backup
   gate: skip if newest marrow-YYYY-MM-DD.db backup is missing or >7d old.
   vec_window_days=0 disables the pass entirely.
"""
from __future__ import annotations

import argparse
import glob
import re
import shutil
import sqlite3
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from . import config, repo, storage


def _fts_phrase(q: str) -> str:
    # Mirror repo._fts_query: phrase match, FTS5-safe (trigram tokenizer).
    return '"' + q.replace('"', '""').strip() + '"'


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
                "AND e.timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-30 days') "
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
        "AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', '-7 days')"
    )
    return cur.rowcount or 0


def prune_md_index_tombstones(conn: sqlite3.Connection) -> int:
    """DELETE md_index rows whose tombstone_at is older than 30 days.

    Keeps the table from accumulating dead rows. Live rows (tombstone_at
    IS NULL) and recently-tombstoned rows (≤30d) are preserved so the
    inserter's anti-resurrection guard still fires for blocks the user
    deleted recently.
    """
    cur = conn.execute(
        "DELETE FROM md_index "
        "WHERE tombstone_at IS NOT NULL "
        "AND tombstone_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', '-30 days')"
    )
    return cur.rowcount or 0


def prune_projects_worktrees(projects_dir: Path | None = None) -> int:
    """Delete every ~/.claude/projects/<slug>/ whose name contains "worktrees".

    cc spawns one slug per cwd; worktree sessions (task-isolated runs in
    non-primary git worktrees) leave behind shell directories after their
    jsonls age out via cc's native 30d cleanup. These shells are never
    revisited — purge unconditionally regardless of remaining content.

    Returns the number of slug directories removed.
    """
    if projects_dir is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return 0
    d = projects_dir or (Path.home() / ".claude" / "projects")
    if not d.is_dir():
        return 0
    purged = 0
    for child in sorted(d.iterdir()):
        if not child.is_dir():
            continue
        if "worktrees" not in child.name:
            continue
        try:
            shutil.rmtree(child)
            purged += 1
        except OSError:
            continue
    return purged


_VEC_EVICT_CAP_PCT = 0.25   # abort if eviction > 25% of vec rows
_VEC_EVICT_CAP_MIN = 100    # pct cap inert below this count — small/new DBs
                            # would trip 25% on a handful of rows
_VEC_EVICT_CAP_ABS = 10000  # abort if eviction > 10000 rows
_BACKUP_STALE_DAYS = 7
_BACKUP_NAME_RE = re.compile(r"^marrow-\d{4}-\d{2}-\d{2}\.db$")


def _newest_backup(backup_dir: str) -> date | None:
    """Return the date of the newest marrow-YYYY-MM-DD.db in backup_dir, or None."""
    d = Path(backup_dir)
    if not d.is_dir():
        return None
    candidates = sorted(
        p.name for p in d.iterdir() if _BACKUP_NAME_RE.match(p.name)
    )
    if not candidates:
        return None
    try:
        return date.fromisoformat(candidates[-1][len("marrow-"):-len(".db")])
    except ValueError:
        return None


def evict_vec_window(
    conn: sqlite3.Connection,
    *,
    window_days: int,
    backup_dir: str,
    dry_run: bool = False,
    alert_db: str | None = None,
) -> dict:
    """Delete out-of-window events_vec + events_vec_meta rows.

    Returns dict with keys: evicted, exempted, skipped (bool), aborted (bool),
    pending_alerts (list of dicts). Callers must flush pending_alerts via
    repo.add_alert AFTER their transaction closes to avoid db-lock conflicts.
    alert_db is accepted for back-compat but ignored here; pass it when
    calling repo.add_alert on the returned pending_alerts.
    """
    result: dict = {
        "evicted": 0, "exempted": 0,
        "skipped": False, "aborted": False,
        "pending_alerts": [],
    }

    if window_days == 0:
        return result

    # Backup gate: skip destructive pass if backup missing or stale.
    newest = _newest_backup(backup_dir)
    today = date.today()
    if newest is None or (today - newest).days > _BACKUP_STALE_DAYS:
        age_str = str((today - newest).days) + "d" if newest else "missing"
        result["pending_alerts"].append({
            "severity": "warn", "atype": "aging",
            "fingerprint": "vec_evict_backup_stale",
            "source": "aging.py",
            "message": (
                f"vec_evict skipped: backup {age_str} (need ≤{_BACKUP_STALE_DAYS}d). "
                f"Run `python -m marrow.backup --apply` then retry."
            ),
        })
        result["skipped"] = True
        return result

    cutoff_sql = f"strftime('%Y-%m-%dT%H:%M:%SZ','now', '-{window_days} days')"

    # Candidate rowids: out-of-window and not exempt.
    # Exempt: affect.event_id link with importance>=3 OR recall_count>0.
    # Source = events_vec (real vectors), NOT events_vec_meta: eviction keeps
    # the meta row as a tombstone, so a meta-based scan would re-select the same
    # already-evicted rows every run.
    candidate_rows = conn.execute(
        f"""
        SELECT ev.rowid
        FROM events_vec ev
        JOIN events e ON e.id = ev.rowid
        WHERE e.timestamp < {cutoff_sql}
          AND e.recall_count = 0
          AND NOT EXISTS (
            SELECT 1 FROM affect a
            WHERE a.event_id = e.id AND a.importance >= 3
          )
        """
    ).fetchall()
    candidate_ids = [r[0] for r in candidate_rows]

    exempt_rows = conn.execute(
        f"""
        SELECT ev.rowid
        FROM events_vec ev
        JOIN events e ON e.id = ev.rowid
        WHERE e.timestamp < {cutoff_sql}
          AND (
            e.recall_count > 0
            OR EXISTS (
              SELECT 1 FROM affect a
              WHERE a.event_id = e.id AND a.importance >= 3
            )
          )
        """
    ).fetchall()
    exempt_count = len(exempt_rows)

    evict_count = len(candidate_ids)
    result["exempted"] = exempt_count

    if evict_count == 0:
        return result

    # Safety caps: abort if too many rows would be evicted.
    total_vec = conn.execute(
        "SELECT COUNT(*) FROM events_vec"
    ).fetchone()[0]
    if (evict_count > _VEC_EVICT_CAP_MIN
            and total_vec > 0
            and evict_count > total_vec * _VEC_EVICT_CAP_PCT):
        result["pending_alerts"].append({
            "severity": "critical", "atype": "aging",
            "fingerprint": "vec_evict_cap_pct",
            "source": "aging.py",
            "message": (
                f"vec_evict aborted: would evict {evict_count}/{total_vec} rows "
                f"({evict_count/total_vec:.0%} > {_VEC_EVICT_CAP_PCT:.0%} cap). "
                "Manual investigation required."
            ),
        })
        result["aborted"] = True
        return result
    if evict_count > _VEC_EVICT_CAP_ABS:
        result["pending_alerts"].append({
            "severity": "critical", "atype": "aging",
            "fingerprint": "vec_evict_cap_abs",
            "source": "aging.py",
            "message": (
                f"vec_evict aborted: would evict {evict_count} rows "
                f"(> {_VEC_EVICT_CAP_ABS} abs cap). Manual investigation required."
            ),
        })
        result["aborted"] = True
        return result

    if dry_run:
        result["evicted"] = evict_count
        return result

    # DELETE in same transaction as caller; conn is already inside `with conn:`.
    # Drop only the vector — KEEP events_vec_meta as an eviction tombstone so the
    # recall dedup (NOT EXISTS vec AND NOT EXISTS meta) does not re-embed the row.
    placeholders = ",".join("?" * len(candidate_ids))
    conn.execute(
        f"DELETE FROM events_vec WHERE rowid IN ({placeholders})",
        candidate_ids,
    )
    result["evicted"] = evict_count
    return result


def main(argv: list[str] | None = None) -> None:
    """Single entrypoint: run all passes, log summary."""
    ap = argparse.ArgumentParser(
        prog="marrow.aging",
        description="Weekly DB maintenance: memes, tasks, milestones, vec window.",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true",
                   help="execute destructive passes (default: dry-run)")
    g.add_argument("--dry-run", action="store_true",
                   help="print eviction plan only, write nothing (default)")
    args = ap.parse_args(argv)
    dry_run = not args.apply

    cfg = config.load()
    backup_dir = cfg["paths"]["backup_dir"]
    window_days = int(cfg.get("recall", {}).get("vec_window_days", 90))

    conn = storage.init_db()
    # Alerts must land in the same DB main() operates on (init_db may be
    # routed elsewhere in tests), not whatever config.db_path() resolves to.
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    # Initialise before try so finally can always flush, even on early raise.
    pending_alerts: list[dict] = []
    vec: dict = {}
    try:
        with conn:
            archived = archive_tasks(conn)
            confirmed = confirm_milestone_alerts(conn)
            tombs = prune_md_index_tombstones(conn)
            wtshells = prune_projects_worktrees()
            vec = evict_vec_window(
                conn,
                window_days=window_days,
                backup_dir=backup_dir,
                dry_run=dry_run,
                alert_db=db_file,
            )
            pending_alerts = vec.get("pending_alerts", [])
            conn.execute(
                "INSERT INTO audit_log "
                "(target_table, target_id, action, summary) "
                "VALUES ('aging', NULL, 'weekly', ?)",
                (f"archived={archived} "
                 f"confirmed={confirmed} "
                 f"tombs={tombs} wtshells={wtshells} "
                 f"vec_evicted={vec['evicted']} vec_exempted={vec['exempted']} "
                 f"vec_skipped={vec['skipped']} vec_aborted={vec['aborted']}",),
            )
            conn.execute(
                "INSERT INTO audit_log "
                "(target_table, target_id, action, summary) "
                "VALUES ('events_vec', NULL, 'vec_evict', ?)",
                (f"evicted={vec['evicted']} exempted={vec['exempted']} "
                 f"skipped={vec['skipped']} aborted={vec['aborted']} "
                 f"window_days={window_days} dry_run={dry_run}",),
            )
        sys.stderr.write(
            f"[aging] archived={archived} "
            f"confirmed={confirmed} "
            f"tombs={tombs} wtshells={wtshells} "
            f"vec_evicted={vec['evicted']} vec_exempted={vec['exempted']} "
            f"vec_skipped={vec['skipped']} vec_aborted={vec['aborted']}"
            f"{' (dry-run)' if dry_run else ''}\n"
        )
    finally:
        # Flush deferred vec alerts even when the audit INSERT raised.
        for a in pending_alerts:
            repo.add_alert(
                a["severity"], a["atype"], a["fingerprint"],
                source=a.get("source"),
                message=a.get("message"),
                db=db_file,
            )
        conn.close()


if __name__ == "__main__":
    main()
