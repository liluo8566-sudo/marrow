"""One-time repair of md_index tombstones that block LIVE db rows from rendering.

Background: subpage inserters refuse to render any (path, block_id) whose
md_index row has tombstone_at set. Combined with plain-INTEGER-PK id reuse, a
deleted row freed an id + tombstoned its block; the next created row reused that
id and was then silently blocked from ever rendering to its subpage. The id
reuse is fixed at the root by storage v36 (AUTOINCREMENT) and the path-key split
by the MdIndex choke point + v37 merge; this script heals any pre-existing
casualty by clearing tombstones whose block_id maps to a currently-live db row.

Casualty = a tombstoned md_index block whose block_id equals the id (or date)
of a live row in that page's source table. Pages that ignore tombstones (atlas)
or are id-less/empty (wallet) are skipped.

Usage:
  python scripts/repair_md_index.py            # dry-run (default)
  python scripts/repair_md_index.py --apply    # requires a fresh DB backup
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from marrow import config, storage  # noqa: E402
from marrow.aging import _BACKUP_STALE_DAYS, _newest_backup  # noqa: E402

# md filename -> SQL producing the set of live block_ids for that page.
# Mirrors the fetch() of each InserterSpec in marrow/subpage_specs.py.
_LIVE_SQL: dict[str, str] = {
    "memes.md": "SELECT id FROM memes",
    "milestone.md": "SELECT id FROM milestones WHERE pinned=1",
    "profile.md": (
        "SELECT id FROM entities WHERE superseded_by IS NULL"
        " AND kind IN ('person','pref','place')"
    ),
    "stickers.md": "SELECT id FROM stickers",
    "projects.md": "SELECT id FROM tasks WHERE category='project'",
    "study.md": "SELECT id FROM tasks WHERE category='study'",
    "diary.md": "SELECT date FROM diary",
}


def _build_plan(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    live: dict[str, set[str]] = {}
    for base, sql in _LIVE_SQL.items():
        try:
            live[base] = {str(r[0]) for r in conn.execute(sql).fetchall()}
        except sqlite3.Error:
            live[base] = set()
    plan: list[tuple[str, str]] = []
    rows = conn.execute(
        "SELECT path, block_id FROM md_index WHERE tombstone_at IS NOT NULL"
    ).fetchall()
    for r in rows:
        base = os.path.basename(r["path"])
        if str(r["block_id"]) in live.get(base, set()):
            plan.append((r["path"], str(r["block_id"])))
    return plan


def _apply(conn: sqlite3.Connection, plan: list[tuple[str, str]]) -> None:
    with conn:
        for path, bid in plan:
            conn.execute(
                "UPDATE md_index SET tombstone_at=NULL"
                " WHERE path=? AND block_id=?",
                (path, bid),
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="preview only — no writes (default)")
    g.add_argument("--apply", action="store_true", help="clear the tombstones")
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
            print(f"REFUSING --apply: backup {age} (need <={_BACKUP_STALE_DAYS}d "
                  f"in {backup_dir}).\nRun `python -m marrow.backup --apply` first.")
            return 2

    conn = storage.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        plan = _build_plan(conn)
        print("-- repair_md_index plan --")
        print(f"live-row tombstone casualties = {len(plan)}")
        per_page: dict[str, int] = defaultdict(int)
        for path, _bid in plan:
            per_page[os.path.basename(path)] += 1
        for base, n in sorted(per_page.items()):
            print(f"  {base}: {n}")
        for path, bid in plan[:12]:
            print(f"    clear  {os.path.basename(path)}  id={bid}")
        if args.apply:
            _apply(conn, plan)
            print("\nApplied. Next subpage render will emit the freed blocks.")
        else:
            print("\nDry-run — no changes written. Re-run with --apply.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
