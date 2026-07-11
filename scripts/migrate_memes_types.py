"""Migrate legacy memes rows to the v2 enum {paw,meme,news,event,fact,others}.

Usage:
  python scripts/migrate_memes_types.py --dry-run
  python scripts/migrate_memes_types.py --apply

Rules (decided 2026-05-24):
- ids 1..5: type → fact, pinned=1 (the user's own protocol/setup notes)
- all other ids: delete (legacy memes table was full of junk —
  rhetorical quotes, mis-classified entities, one-off phrases)

Dry-run prints a preview table; --apply writes in a single transaction.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Allow running as a script from repo root or scripts/.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from marrow import config  # noqa: E402

_FACT_IDS = {1, 2, 3, 4, 5}


def _plan_row(rid: int, rtype: str, key: str) -> tuple[str, str | None,
                                                       int | None]:
    """Return (action, new_type, new_pinned).
    action ∈ {keep, delete, update}.
    """
    if rid in _FACT_IDS:
        return ("update", "fact", 1)
    return ("delete", None, None)


def _build_plan(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, type, key, pinned FROM memes ORDER BY id"
    ).fetchall()
    out = []
    for r in rows:
        action, new_type, new_pinned = _plan_row(
            r["id"], r["type"], r["key"] or "")
        out.append({
            "id": r["id"],
            "key": r["key"] or "",
            "old_type": r["type"],
            "old_pinned": r["pinned"],
            "action": action,
            "new_type": new_type,
            "new_pinned": new_pinned,
        })
    return out


def _print_preview(plan: list[dict]) -> None:
    print(f"{'id':>3}  {'action':<7}  {'old_type':<9}  {'new_type':<9}  "
          f"{'old_pin':>7}  {'new_pin':>7}  key")
    print("-" * 80)
    for p in plan:
        new_t = p["new_type"] or ""
        new_p = "" if p["new_pinned"] is None else str(p["new_pinned"])
        print(f"{p['id']:>3}  {p['action']:<7}  {p['old_type']:<9}  "
              f"{new_t:<9}  {p['old_pinned']:>7}  {new_p:>7}  {p['key']}")
    counts = {"keep": 0, "update": 0, "delete": 0}
    for p in plan:
        counts[p["action"]] += 1
    print(f"\nSummary: keep={counts['keep']} update={counts['update']} "
          f"delete={counts['delete']} total={len(plan)}")


def _apply(conn: sqlite3.Connection, plan: list[dict]) -> None:
    with conn:
        for p in plan:
            if p["action"] == "delete":
                conn.execute("DELETE FROM memes WHERE id=?", (p["id"],))
            elif p["action"] == "update":
                if p["new_pinned"] is None:
                    conn.execute(
                        "UPDATE memes SET type=? WHERE id=?",
                        (p["new_type"], p["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE memes SET type=?, pinned=? WHERE id=?",
                        (p["new_type"], p["new_pinned"], p["id"]),
                    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="preview only — no writes")
    g.add_argument("--apply", action="store_true",
                   help="apply changes")
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
            _apply(conn, plan)
            print("\nApplied.")
        else:
            print("\nDry-run — no changes written.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
