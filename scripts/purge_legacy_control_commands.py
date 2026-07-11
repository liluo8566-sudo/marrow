"""One-off purge of legacy CC control-command rows written before c40e41e.

c40e41e added a write-side filter (transcript._is_control_command_row) that
drops CC harness slash commands with empty args (/clear /model /compact /mcp
/effort ...) before archive. Rows written before that commit predate the
filter and still sit in the DB. This script targets the STRIPPED stored form
(the write-side filter runs on raw JSONL where <command-name>/<command-args>
tags are still intact; by the time content lands in `events` those tags are
already collapsed by strip_harness_markers). A legacy junk row is stored
content that is EXACTLY one of the bare control-command names below, with no
residual args text — matching the same "empty args" semantics as
_is_control_command_row. Custom commands (/goal /diagnose /teach) and control
commands submitted WITH args (e.g. "/model\\n \\n opusplan", "/effort\\n \\n max")
carry real text after stripping and are NOT touched.

Deleted rows are tombstoned via their existing events.source_hash (mirrors
daemon._do_event_clear), so archive_events cannot resurrect them. events_vec /
events_vec_meta / events_fts cleanup is handled by the events_ad / events_ad_vec
AFTER DELETE triggers — no manual touch needed (verified in schema).

Usage:
  python scripts/purge_legacy_control_commands.py --dry-run
  python scripts/purge_legacy_control_commands.py --apply
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from marrow import config, storage  # noqa: E402

# Bare control-command names — exact stored content match only. A row with
# any residual args text (e.g. a picked model name) is a different string
# and will not match these exact equalities, so it is left alone.
_BARE_COMMANDS = ("/clear", "/model", "/compact", "/mcp", "/effort")

_SELECT_SQL = (
    "SELECT id, session_id, timestamp, role, channel, content, source_hash "
    "FROM events WHERE content IN ({})"
).format(",".join("?" * len(_BARE_COMMANDS)))


def _build_plan(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(_SELECT_SQL, _BARE_COMMANDS).fetchall()
    return [dict(r) for r in rows]


def _print_preview(plan: list[dict]) -> None:
    print(f"{'id':>6}  {'role':<6}  {'channel':<6}  {'content':<10}  {'session':<10}  timestamp")
    print("-" * 80)
    for p in plan:
        sid = (p["session_id"] or "")[:8]
        print(f"{p['id']:>6}  {p['role']:<6}  {p['channel']:<6}  {p['content']:<10}  {sid:<10}  {p['timestamp']}")
    from collections import Counter
    print(f"\nBy content: {dict(Counter(p['content'] for p in plan))}")
    print(f"By channel: {dict(Counter(p['channel'] for p in plan))}")
    print(f"Total: {len(plan)}")
    null_hash = [p["id"] for p in plan if not p["source_hash"]]
    if null_hash:
        print(f"WARNING: {len(null_hash)} rows have NULL source_hash, will delete without tombstone: {null_hash}")


def _apply(conn: sqlite3.Connection, plan: list[dict]) -> dict:
    ids = [p["id"] for p in plan]
    sids = sorted({p["session_id"] for p in plan if p["session_id"]})
    tombstoned = 0
    with conn:
        for p in plan:
            conn.execute("DELETE FROM events WHERE id=?", (p["id"],))
            if p["source_hash"]:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO event_tombstones (source_hash, reason) VALUES (?, ?)",
                    (p["source_hash"], "purge_legacy_control_commands: pre-c40e41e control-command row"),
                )
                tombstoned += cur.rowcount
        if sids:
            conn.executemany(
                "DELETE FROM audit_log WHERE action='sessionend_extract' AND target_id=?",
                [(s,) for s in sids],
            )
    return {"deleted_events": len(ids), "tombstoned": tombstoned, "sessions_touched": len(sids)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="preview only — no writes")
    g.add_argument("--apply", action="store_true", help="apply changes")
    ap.add_argument("--db", default=None, help="db path (default: marrow config)")
    args = ap.parse_args(argv)

    db = args.db or config.db_path()

    if args.apply:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = Path(db).parent / f"marrow.db.bak-{ts}-junkpurge"
        shutil.copy2(db, backup)
        print(f"Backup written: {backup}")

    conn = storage.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        plan = _build_plan(conn)
        _print_preview(plan)
        if args.apply:
            result = _apply(conn, plan)
            print(f"\nApplied: {result}")
        else:
            print("\nDry-run — no changes written.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
