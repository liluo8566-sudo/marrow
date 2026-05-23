"""md->DB reconcile for sub-pages.

Scope: milestone only. Vocab/pit/tasks plug in later via the same shape
(parse(md)->rows, diff against DB, apply). reconcile_milestones is the
public entry; write_subpage wires it via SubPageConfig.reconcile.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path


MILESTONE_KEY = "milestone"
_M0 = f"<!-- marrow:{MILESTONE_KEY}:start -->"
_M1 = f"<!-- marrow:{MILESTONE_KEY}:end -->"

# Mirrors render_milestone output:
#   - YYYY-MM-DD **Title** [theme] (pinned) — desc <!-- id:N -->
# Theme, (pinned), description, and anchor are optional in capture; the
# title and date are required for a parseable row.
_ROW_RE = re.compile(
    r"^- (?P<date>\d{4}-\d{2}-\d{2}) "
    r"\*\*(?P<title>[^*]+?)\*\*"
    r"(?: \[(?P<theme>[^\]]+)\])?"
    r"(?P<pinned> \(pinned\))?"
    r"(?: — (?P<desc>.*?))?"
    r"(?: <!-- id:(?P<id>\d+) -->)?\s*$"
)


@dataclass
class ReconcileReport:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    conflicts: list[str] = field(default_factory=list)

    def any_change(self) -> bool:
        return self.inserted or self.updated or self.deleted

    def destructive(self) -> bool:
        return self.deleted > 0 or bool(self.conflicts)


def _parse(md_text: str) -> list[dict]:
    """Yield row dicts in section order. Section is set by ## Us / ## Me."""
    rows: list[dict] = []
    section: str | None = None
    in_block = False
    for line in md_text.splitlines():
        s = line.rstrip()
        if _M0 in s:
            in_block = True
            continue
        if _M1 in s:
            in_block = False
            continue
        if not in_block:
            continue
        if s.startswith("## "):
            head = s[3:].strip().lower()
            if head == "us":
                section = "us"
            elif head == "me":
                section = "me"
            else:
                section = None
            continue
        m = _ROW_RE.match(s)
        if not m or section is None:
            continue
        rows.append({
            "scope": section,
            "date": m.group("date"),
            "title": m.group("title").strip(),
            "theme": m.group("theme"),
            "pinned": 1 if m.group("pinned") else 0,
            "description": (m.group("desc") or None) and m.group("desc").strip(),
            "id": int(m.group("id")) if m.group("id") else None,
        })
    return rows


def _hash(row: dict) -> str:
    src = "\x1f".join([
        row["scope"], row["date"], row["title"], row.get("description") or "",
    ])
    return hashlib.sha256(src.encode()).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _audit(conn, mid: int | str, action: str, summary: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (target_table, target_id, action, summary) "
        "VALUES ('milestones', ?, ?, ?)",
        (str(mid), action, summary),
    )


def reconcile_milestones(conn: sqlite3.Connection,
                          md_path: Path) -> ReconcileReport:
    """Apply md edits back to milestones, then return a report.

    Contract:
    - Row with `<!-- id:N -->` -> match on id; update if title/desc/theme/
      pinned/date changed.
    - Row without anchor -> insert (new milestone written by Lumi).
    - DB row whose id is missing from md -> delete.
    - No-op when md == current state: no writes, no audit, no backup.
    """
    rpt = ReconcileReport()
    md_path = Path(md_path)
    if not md_path.exists():
        return rpt
    md_rows = _parse(md_path.read_text(encoding="utf-8"))
    db_rows = {
        r["id"]: dict(r) for r in conn.execute(
            "SELECT id, scope, date, title, description, theme, pinned "
            "FROM milestones"
        ).fetchall()
    }

    seen: set[int] = set()
    backup_taken = False

    def _backup_once() -> None:
        nonlocal backup_taken
        if backup_taken or not md_path.exists():
            return
        state = md_path.parent
        bak = state / f"{md_path.stem}.{int(time.time())}.bak{md_path.suffix}"
        try:
            shutil.copyfile(md_path, bak)
            backup_taken = True
        except OSError as e:
            rpt.conflicts.append(f"backup failed: {e}")

    with conn:
        # inserts + updates
        for row in md_rows:
            rid = row["id"]
            if rid is not None and rid in db_rows:
                cur = db_rows[rid]
                changed = (
                    (cur["scope"] or "") != row["scope"]
                    or (cur["date"] or "") != row["date"]
                    or (cur["title"] or "") != row["title"]
                    or (cur["description"] or None) != row["description"]
                    or (cur["theme"] or None) != row["theme"]
                    or int(cur["pinned"] or 0) != row["pinned"]
                )
                if changed:
                    h = _hash(row)
                    conn.execute(
                        "UPDATE milestones SET "
                        "scope=?, date=?, title=?, description=?, theme=?, "
                        "pinned=?, source_hash=?, updated_at=? WHERE id=?",
                        (row["scope"], row["date"], row["title"],
                         row["description"], row["theme"], row["pinned"],
                         h, _now(), rid),
                    )
                    _audit(conn, rid, "update",
                           f"md-reconcile: {row['title'][:60]}")
                    rpt.updated += 1
                else:
                    rpt.unchanged += 1
                seen.add(rid)
            elif rid is not None and rid not in db_rows:
                # Anchored id not in DB -> Lumi referenced a deleted row.
                # Treat as conflict; do not auto-create with a forced id.
                rpt.conflicts.append(
                    f"anchored id {rid} not in db: {row['title'][:40]}"
                )
            else:
                h = _hash(row)
                cur = conn.execute(
                    "INSERT INTO milestones "
                    "(scope, date, title, description, theme, pinned, "
                    " source_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row["scope"], row["date"], row["title"],
                     row["description"], row["theme"], row["pinned"], h),
                )
                _audit(conn, cur.lastrowid, "insert",
                       f"md-reconcile: {row['title'][:60]}")
                rpt.inserted += 1

        # deletes: db rows whose ids are not present in md
        for rid in list(db_rows.keys()):
            if rid in seen:
                continue
            # only delete if md had any anchored rows OR md is non-empty;
            # otherwise an empty/missing md would wipe the table. We already
            # returned early on missing file; require at least one parsed row.
            if not md_rows:
                continue
            if not backup_taken:
                _backup_once()
            conn.execute("DELETE FROM milestones WHERE id=?", (rid,))
            _audit(conn, rid, "delete", "md-reconcile: removed from md")
            rpt.deleted += 1

        if rpt.destructive() and not backup_taken:
            _backup_once()

    return rpt
