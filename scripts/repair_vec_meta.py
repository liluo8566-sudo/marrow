"""One-time repair of events vector-lane poisoning + bridge-marker junk.

Background: the no-filter event_clear branch used to drop all events triggers,
DELETE FROM events, and manually clear events_vec but NEVER events_vec_meta.
Because events.id is a plain INTEGER PK, freed ids get reused and newborn events
inherit an orphan meta row ("embedded" bookkeeping with no vector). The recall
dedup then skips them forever, so the vec lane covers a tiny fraction of the
corpus. The gate is fixed in daemon.py; this script repairs the existing damage.

Four categories (counts printed in both modes):
  1. orphan vec      — events_vec rows with no matching live event.
  2. orphan meta     — events_vec_meta rows with no matching live event.
  3. poisoned meta   — live event, meta row present, but NO real vector.
  4. junk rows       — events whose ENTIRE content is CC-harness junk, decided
                       by transcript._is_harness_row (the same predicate the
                       ingest gate uses) — including bare [time:]/[sticker:]
                       bridge headers with no message body. Deleted whole +
                       tombstoned so archive_events cannot resurrect them.

Rows that mix a [time:]/[sticker:] header with REAL dialogue are LEFT ALONE:
the [time:] prefix is intentionally retained in stored content and stripped
only at each consumption point (hooks._WX_TIME_PREFIX_RE, recall needle build),
so mutating it here would break that design.

After --apply, run `/embed` (embed_pending) to re-embed the freed rows.

WARNING — one-shot. Do NOT re-run after vec eviction has begun keeping meta
tombstones (aging.evict_vec_window now deletes only the vector and KEEPS the
meta row): categories 2/3 cannot distinguish an eviction tombstone from poison,
so a later run would delete legitimate tombstones and trigger re-embedding.

Usage:
  python scripts/repair_vec_meta.py            # dry-run (default)
  python scripts/repair_vec_meta.py --dry-run
  python scripts/repair_vec_meta.py --apply    # requires a fresh DB backup
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from marrow import config, storage  # noqa: E402
from marrow.aging import _BACKUP_STALE_DAYS, _newest_backup  # noqa: E402
from marrow.transcript import _is_harness_row  # noqa: E402

# Cheap prefilter for whole-row junk; the authoritative decision is made in
# Python by _is_harness_row so the ingest gate and repair never diverge.
_JUNK_CANDIDATE_SQL = """
    SELECT id, content, source_hash
    FROM events
    WHERE content LIKE '[time:%'
       OR content LIKE '[sticker:%'
       OR content LIKE '<task-notification>%'
       OR content = '[Request interrupted by user]'
       OR content = '[Request interrupted by user for tool use]'
       OR content LIKE '%<image path=%'
       OR content LIKE '%<gif path=%'
"""


def _build_plan(conn: sqlite3.Connection) -> dict:
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    orphan_vec = conn.execute(
        "SELECT COUNT(*) FROM events_vec "
        "WHERE rowid NOT IN (SELECT id FROM events)"
    ).fetchone()[0]
    orphan_meta = conn.execute(
        "SELECT COUNT(*) FROM events_vec_meta "
        "WHERE rowid NOT IN (SELECT id FROM events)"
    ).fetchone()[0]
    orphan_meta_gt_max = conn.execute(
        "SELECT COUNT(*) FROM events_vec_meta WHERE rowid > ?", (max_id,)
    ).fetchone()[0]
    poisoned_meta = conn.execute(
        "SELECT COUNT(*) FROM events_vec_meta m "
        "WHERE m.rowid IN (SELECT id FROM events) "
        "  AND NOT EXISTS (SELECT 1 FROM events_vec v WHERE v.rowid=m.rowid)"
    ).fetchone()[0]

    junk: list[dict] = []
    for r in conn.execute(_JUNK_CANDIDATE_SQL).fetchall():
        content = r["content"] or ""
        if _is_harness_row(content):
            junk.append({"id": r["id"], "content": content,
                         "source_hash": r["source_hash"]})
    return {
        "max_id": max_id,
        "orphan_vec": orphan_vec,
        "orphan_meta": orphan_meta,
        "orphan_meta_gt_max": orphan_meta_gt_max,
        "poisoned_meta": poisoned_meta,
        "junk": junk,
    }


def _print_plan(plan: dict) -> None:
    print(f"max(events.id)          = {plan['max_id']}")
    print(f"orphan vec  (no event)  = {plan['orphan_vec']}")
    print(f"orphan meta (no event)  = {plan['orphan_meta']}"
          f"  (of which rowid>max = {plan['orphan_meta_gt_max']})")
    print(f"poisoned meta (no vec)  = {plan['poisoned_meta']}")
    print(f"junk rows (whole-row)   = {len(plan['junk'])}")

    if plan["junk"]:
        print("\n  junk sample (up to 8):")
        for j in plan["junk"][:8]:
            print(f"    id={j['id']:>5}  {j['content'][:70]!r}")


def _apply(conn: sqlite3.Connection, plan: dict) -> None:
    with conn:
        # 1. Whole-row junk: DELETE cascades events_fts/vec/meta via triggers.
        for j in plan["junk"]:
            conn.execute("DELETE FROM events WHERE id=?", (j["id"],))
            if j["source_hash"]:
                conn.execute(
                    "INSERT OR IGNORE INTO event_tombstones (source_hash, reason) "
                    "VALUES (?, ?)",
                    (j["source_hash"], "repair_vec_meta: whole-row bridge junk"),
                )
        # 2-4. Vec/meta cleanup for rows with no matching event or no vector.
        conn.execute(
            "DELETE FROM events_vec WHERE rowid NOT IN (SELECT id FROM events)")
        conn.execute(
            "DELETE FROM events_vec_meta WHERE rowid NOT IN (SELECT id FROM events)")
        conn.execute(
            "DELETE FROM events_vec_meta WHERE rowid IN (SELECT id FROM events) "
            "AND NOT EXISTS (SELECT 1 FROM events_vec v WHERE v.rowid=events_vec_meta.rowid)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="preview only — no writes (default)")
    g.add_argument("--apply", action="store_true", help="apply the repair")
    ap.add_argument("--db", default=None, help="db path (default: marrow config)")
    args = ap.parse_args(argv)

    cfg = config.load()
    db = args.db or cfg["paths"]["db"]

    if args.apply:
        backup_dir = cfg["paths"]["backup_dir"]
        newest = _newest_backup(backup_dir)
        today = date.today()
        if newest is None or (today - newest).days > _BACKUP_STALE_DAYS:
            age = f"{(today - newest).days}d" if newest else "missing"
            print(f"REFUSING --apply: backup {age} (need ≤{_BACKUP_STALE_DAYS}d "
                  f"in {backup_dir}).\nRun `python -m marrow.backup --apply` first.")
            return 2

    conn = storage.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        plan = _build_plan(conn)
        print("── repair_vec_meta plan ──")
        _print_plan(plan)
        if args.apply:
            _apply(conn, plan)
            print("\nApplied. Run `/embed` (embed_pending) to re-embed freed rows.")
        else:
            print("\nDry-run — no changes written. Re-run with --apply.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
