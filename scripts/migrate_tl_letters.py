"""Rewrite historical tl (role='tl') affect-label letters to the configured
tl.user_letter / tl.assistant_letter (see marrow/tl_writer.py:canonicalize_label_letters).

Old hardcoded letters were N (user) / Y (assistant). Rows written before the
config option existed still carry N/Y at label-anchor positions
(start-of-label, right after ♡, right after "| "). This script sweeps them
to the configured letters (default N/Y -> no-op).

Scope: ONLY the leading 【...】 label segment of events.content for rows
WHERE role='tl'. Body text is never touched, even if it happens to contain
"N"/"Y" characters.

Usage:
  python scripts/migrate_tl_letters.py --dry-run
  python scripts/migrate_tl_letters.py --apply
  python scripts/migrate_tl_letters.py --apply --db /path/to/copy.db

Idempotent: rows already carrying the configured letters (or already-default
N/Y config) show no change and are skipped on re-run — safe to re-run later
to sweep stragglers written by still-running old-code windows.

--apply backs up the DB to /tmp first (sqlite3 .backup), then writes in a
single transaction. --dry-run only reads (no writes, safe to run anytime).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sqlite3
import subprocess
import sys
from pathlib import Path

# Allow running as a script from repo root or scripts/.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from marrow import config  # noqa: E402
from marrow.tl_writer import canonicalize_label_letters  # noqa: E402


def _build_plan(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, content FROM events WHERE role='tl' ORDER BY id",
    ).fetchall()
    plan: list[dict] = []
    for r in rows:
        eid, content = r["id"], r["content"] or ""
        new_content = canonicalize_label_letters(content)
        if new_content != content:
            plan.append({"id": eid, "action": "rewrite",
                        "old": content, "new": new_content})
        else:
            plan.append({"id": eid, "action": "skip:unchanged",
                        "old": content, "new": None})
    return plan


def _print_preview(plan: list[dict]) -> None:
    for p in plan:
        if p["action"] == "rewrite":
            print(f"[event_id={p['id']}] {p['old']!r} -> {p['new']!r}")
    counts: dict[str, int] = {}
    for p in plan:
        counts[p["action"]] = counts.get(p["action"], 0) + 1
    touched = counts.get("rewrite", 0)
    print(f"\nSummary: {counts} total={len(plan)} would_touch={touched}")


def _backup_db(db: str) -> str:
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = f"/tmp/marrow_migrate_tl_letters_{ts}.db"
    subprocess.run(["sqlite3", db, f".backup {dest}"], check=True)
    return dest


def _apply(conn: sqlite3.Connection, plan: list[dict]) -> None:
    with conn:
        for p in plan:
            if p["action"] != "rewrite":
                continue
            conn.execute(
                "UPDATE events SET content=? WHERE id=?",
                (p["new"], p["id"]),
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="preview only — no writes")
    g.add_argument("--apply", action="store_true",
                   help="apply changes (backs up DB to /tmp first)")
    ap.add_argument("--db", default=None,
                    help="db path (default: marrow config)")
    args = ap.parse_args(argv)

    db = args.db or config.db_path()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        plan = _build_plan(conn)
        _print_preview(plan)
        if args.apply:
            backup = _backup_db(db)
            print(f"\nBacked up DB to {backup}")
            _apply(conn, plan)
            print("Applied.")
        else:
            print("\nDry-run — no changes written.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
