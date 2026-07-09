"""Clean CC harness markers from event bodies in the database.

Two independent passes, both dry-run/apply gated:

1. Substring strip — rows whose content contains CC harness tags
   (<command-message>, <command-name>, <command-args>, [Image #N],
   [Image: source: ...], <local-command-stdout>) are either updated
   (non-empty cleaned content) or deleted (empty after stripping).
2. Whole-row junk drop — rows that ARE entirely one of four harness-junk
   classes (task-notification receipts, interrupt markers, bare sticker-tag
   bubbles) via transcript._is_harness_row — the same predicate the ingest
   gate in marrow/transcript.py uses, so retroactive cleanup and future
   ingest never diverge. These rows are always deleted whole (never
   updated), since by definition nothing real remains.

Deleted rows are tombstoned so archive_events cannot resurrect them.
events_vec / events_vec_meta / events_fts rows are removed via the
events_ad / events_ad_vec AFTER DELETE triggers (marrow/storage.py) — no
manual cleanup needed, but events_vec/_meta deletes are kept explicit below
for the UPDATE case, which the triggers do not cover.

Usage:
  python scripts/clean_harness_events.py --dry-run
  python scripts/clean_harness_events.py --apply
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from marrow import config, storage  # noqa: E402
from marrow.transcript import _is_harness_row, strip_harness_markers  # noqa: E402


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


_DIRTY_SQL = """
    SELECT id, session_id, timestamp, role, content
    FROM events
    WHERE content LIKE '%<command-message>%'
       OR content LIKE '%<command-name>%'
       OR content LIKE '%<command-args>%'
       OR content LIKE '%[Image #%'
       OR content LIKE '%[Image: source:%'
       OR content LIKE '%<local-command-stdout>%'
"""


def _build_plan(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(_DIRTY_SQL).fetchall()
    plan = []
    for r in rows:
        content = r["content"] or ""
        new_content = strip_harness_markers(content)
        if new_content == content:
            continue  # no harness markers matched — skip
        action = "DELETE" if not new_content else "UPDATE"
        plan.append({
            "id": r["id"],
            "session_id": r["session_id"] or "",
            "timestamp": r["timestamp"] or "",
            "role": r["role"] or "",
            "old_content": content,
            "new_content": new_content,
            "action": action,
        })
    return plan


def _print_preview(plan: list[dict]) -> None:
    print(f"{'id':>6}  {'action':<7}  {'before':>7}  {'after':>7}  preview")
    print("-" * 80)
    for p in plan:
        after = "EMPTY" if p["action"] == "DELETE" else str(len(p["new_content"]))
        preview = p["new_content"][:60].replace("\n", " ") if p["new_content"] else "(deleted)"
        print(f"{p['id']:>6}  {p['action']:<7}  {len(p['old_content']):>7}  {after:>7}  {preview}")
    updates = sum(1 for p in plan if p["action"] == "UPDATE")
    deletes = sum(1 for p in plan if p["action"] == "DELETE")
    print(f"\nSummary: update={updates} delete={deletes} total={len(plan)}")


def _apply(conn: sqlite3.Connection, plan: list[dict]) -> None:
    with conn:
        for p in plan:
            eid = p["id"]
            if p["action"] == "UPDATE":
                new_hash = _hash(p["session_id"], p["timestamp"], p["role"], p["new_content"])
                conn.execute(
                    "UPDATE events SET content=?, source_hash=? WHERE id=?",
                    (p["new_content"], new_hash, eid),
                )
                conn.execute("DELETE FROM events_vec WHERE rowid=?", (eid,))
                conn.execute("DELETE FROM events_vec_meta WHERE rowid=?", (eid,))
            else:
                old_hash = _hash(p["session_id"], p["timestamp"], p["role"], p["old_content"])
                conn.execute("DELETE FROM events WHERE id=?", (eid,))
                conn.execute("DELETE FROM events_vec WHERE rowid=?", (eid,))
                conn.execute("DELETE FROM events_vec_meta WHERE rowid=?", (eid,))
                conn.execute(
                    "INSERT OR IGNORE INTO event_tombstones (source_hash, reason) VALUES (?, ?)",
                    (old_hash, "clean_harness_events: empty after strip"),
                )


_JUNK_CANDIDATE_SQL = """
    SELECT id, session_id, timestamp, role, content
    FROM events
    WHERE content LIKE '<task-notification>%'
       OR content = '[Request interrupted by user]'
       OR content = '[Request interrupted by user for tool use]'
       OR content LIKE '%<image path=%'
       OR content LIKE '%<gif path=%'
"""


def _classify_junk(content: str) -> str:
    if content.startswith("<task-notification>"):
        return "task-notification"
    if content.startswith("[Request interrupted by user"):
        return "interrupted"
    return "sticker-tag"


def _build_junk_plan(conn: sqlite3.Connection) -> list[dict]:
    """Whole-row junk candidates, decided by the shared _is_harness_row
    predicate (SQL above is only a cheap prefilter)."""
    rows = conn.execute(_JUNK_CANDIDATE_SQL).fetchall()
    plan = []
    for r in rows:
        content = r["content"] or ""
        if not _is_harness_row(content):
            continue  # e.g. a row that quotes the tag mid-dialogue — keep
        plan.append({
            "id": r["id"],
            "session_id": r["session_id"] or "",
            "timestamp": r["timestamp"] or "",
            "role": r["role"] or "",
            "content": content,
            "class": _classify_junk(content),
        })
    return plan


def _print_junk_preview(plan: list[dict]) -> None:
    print(f"{'id':>6}  {'role':<10}  {'class':<17}  {'len':>6}  preview")
    print("-" * 80)
    for p in plan:
        preview = p["content"][:60].replace("\n", " ")
        print(f"{p['id']:>6}  {p['role']:<10}  {p['class']:<17}  {len(p['content']):>6}  {preview}")
    from collections import Counter
    by_class = Counter(p["class"] for p in plan)
    print(f"\nSummary: {dict(by_class)} total={len(plan)}")


def _apply_junk(conn: sqlite3.Connection, plan: list[dict]) -> None:
    with conn:
        for p in plan:
            eid = p["id"]
            old_hash = _hash(p["session_id"], p["timestamp"], p["role"], p["content"])
            conn.execute("DELETE FROM events WHERE id=?", (eid,))
            conn.execute(
                "INSERT OR IGNORE INTO event_tombstones (source_hash, reason) VALUES (?, ?)",
                (old_hash, f"clean_harness_events: whole-row junk ({p['class']})"),
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="preview only — no writes")
    g.add_argument("--apply", action="store_true", help="apply changes")
    ap.add_argument("--db", default=None, help="db path (default: marrow config)")
    args = ap.parse_args(argv)

    db = args.db or config.db_path()
    conn = storage.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        plan = _build_plan(conn)
        print("── substring-strip pass ──")
        _print_preview(plan)

        junk_plan = _build_junk_plan(conn)
        print("\n── whole-row junk-drop pass ──")
        _print_junk_preview(junk_plan)

        if args.apply:
            _apply(conn, plan)
            _apply_junk(conn, junk_plan)
            print("\nApplied.")
        else:
            print("\nDry-run — no changes written.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
