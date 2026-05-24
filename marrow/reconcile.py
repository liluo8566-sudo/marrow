"""md->DB reconcile for sub-pages + dashboard candidate rows.

Scope today:
- milestone subpage (reconcile_milestones)
- dashboard `## Milestone candidate` rows with anchor buttons
  (reconcile_milestone_candidates) — ✅ pin · ❌ delete+tombstone · ✏️ edit

Vocab/pit/tasks plug in later via the same shape (parse(md)->rows,
diff against DB, apply). reconcile_* are the public entries; the
dashboard writer wires the candidate pass via write_dashboard.
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

# Mirrors render_milestone output (milestone_format_unify, 2026-05-24):
#   - [YYYY-MM-DD] subject: description  <!-- id:N -->
# Description and anchor are optional; subject and date are required.
# Subject = `title` column (text before the colon). Theme column kept
# nullable in DB but no longer parsed from md.
_ROW_RE = re.compile(
    r"^- \[(?P<date>\d{4}-\d{2}-\d{2})\] "
    r"(?P<title>[^:<]+?)"
    r"(?:: (?P<desc>.*?))?"
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
            "theme": None,  # render dropped theme; DB col stays nullable
            "pinned": 1,    # md rows on subpage are confirmed by being there
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
    # Reconcile operates on pinned=1 only — the confirmed subpage set.
    # pinned=0 candidates live outside the md ↔ db sync loop; daily.py
    # writes them, dashboard renders them, Lumi promotes via pinned=1.
    db_rows = {
        r["id"]: dict(r) for r in conn.execute(
            "SELECT id, scope, date, title, description, theme, pinned "
            "FROM milestones WHERE pinned=1"
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
                )
                if changed:
                    h = _hash(row)
                    conn.execute(
                        "UPDATE milestones SET "
                        "scope=?, date=?, title=?, description=?, theme=?, "
                        "pinned=1, source_hash=?, updated_at=? WHERE id=?",
                        (row["scope"], row["date"], row["title"],
                         row["description"], row["theme"],
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
                    " source_hash) VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (row["scope"], row["date"], row["title"],
                     row["description"], row["theme"], h),
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


# ── dashboard candidate-row reconcile (✅ ❌ ✏️) ───────────────────────────

_PIN_CHAR = "✅"      # ✅
_DROP_CHAR = "❌"     # ❌
_EDIT_CHAR = "✏️"  # ✏️ (pencil + emoji selector)

# Candidate row produced by top_sections.render_milestone_candidate:
#   - [YYYY-MM-DD] <title> (Nh ago)  ✅ ❌ ✏️  <!-- id:N -->
# We tolerate the user leaving a single char (the "vote"). The button block
# may be edited/reordered; we look for the presence of a single decision char
# (✅ or ❌) — pencil edits are not destructive, treated as no-op for now
# (md reflects DB on next render either way).
_CAND_ID_RE = re.compile(r"<!-- id:(\d+) -->")


def _parse_dashboard_candidates(text: str) -> list[dict]:
    """Scan dashboard md for milestone-candidate rows with vote chars.

    Returns a list of {id, vote} where vote ∈ {"pin", "drop", "edit"}.
    Rows missing a decision char are skipped. Multiple chars on one row
    resolve in priority: drop > pin > edit (destructive wins so a Lumi
    "❌ + leftover ✅" still drops).
    """
    found: list[dict] = []
    in_block = False
    for raw in text.splitlines():
        s = raw.rstrip()
        if "## Milestone candidate" in s:
            in_block = True
            continue
        if in_block and s.startswith("## "):
            in_block = False
            continue
        if not in_block:
            continue
        m = _CAND_ID_RE.search(s)
        if not m:
            continue
        try:
            rid = int(m.group(1))
        except ValueError:
            continue
        # All three chars are template defaults; require the user to remove
        # the other two for a vote to register. If all three remain, no vote.
        has_pin = _PIN_CHAR in s
        has_drop = _DROP_CHAR in s
        has_edit = _EDIT_CHAR in s
        present = sum([has_pin, has_drop, has_edit])
        if present != 1:
            continue
        if has_drop:
            vote = "drop"
        elif has_pin:
            vote = "pin"
        else:
            vote = "edit"
        found.append({"id": rid, "vote": vote})
    return found


def reconcile_milestone_candidates(conn: sqlite3.Connection,
                                    dashboard_path: Path) -> ReconcileReport:
    """Apply ✅/❌ votes on dashboard milestone-candidate rows.

    ✅ → pinned=1 (row moves to subpage on next render — scope already on row).
    ❌ → DELETE + audit_log tombstone (anti-revive on next extraction pass).
    ✏️ → no-op for now (md re-rendered to DB state; HTML layer realises edits).
    """
    rpt = ReconcileReport()
    dashboard_path = Path(dashboard_path)
    if not dashboard_path.exists():
        return rpt
    text = dashboard_path.read_text(encoding="utf-8")
    votes = _parse_dashboard_candidates(text)
    if not votes:
        return rpt
    with conn:
        for v in votes:
            rid = v["id"]
            row = conn.execute(
                "SELECT id, pinned, source_hash, title FROM milestones"
                " WHERE id=?", (rid,)
            ).fetchone()
            if row is None:
                rpt.conflicts.append(f"candidate id {rid} not in db")
                continue
            if v["vote"] == "pin":
                if row["pinned"]:
                    rpt.unchanged += 1
                    continue
                conn.execute(
                    "UPDATE milestones SET pinned=1, updated_at=? WHERE id=?",
                    (_now(), rid),
                )
                _audit(conn, rid, "pin",
                       f"dashboard ✅: {(row['title'] or '')[:60]}")
                rpt.updated += 1
            elif v["vote"] == "drop":
                conn.execute("DELETE FROM milestones WHERE id=?", (rid,))
                # Tombstone in audit_log keyed on source_hash so future
                # candidate extraction can skip the same upstream row.
                tomb = f"sha={row['source_hash'] or ''}|" \
                       f"title={(row['title'] or '')[:80]}"
                _audit(conn, rid, "tombstone", f"dashboard ❌: {tomb}")
                rpt.deleted += 1
            else:  # edit — no-op until HTML layer realises in-place edits
                _audit(conn, rid, "edit_noop", "dashboard ✏️ (no md edit path)")
                rpt.unchanged += 1
    return rpt
