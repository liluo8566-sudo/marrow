"""md->DB reconcile for sub-pages + dashboard top sections.

Scope today:
- milestone subpage (reconcile_milestones)
- dashboard `## Milestone candidate` rows with anchor buttons
  (reconcile_milestone_candidates) — ✅ pin · ❌ delete+tombstone · ✏️ edit
- dashboard `## Tasks` block (reconcile_tasks) — tick/untick/archive
  via `<!-- id:N -->` anchors + `<!-- cand:task:ids=[...] -->` trail.

Vocab/pit plug in later via the same shape (parse(md)->rows, diff
against DB, apply). reconcile_* are the public entries; the dashboard
writer wires the candidate + task passes via write_dashboard.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path


MILESTONE_KEY = "milestone"
_M0 = f"<!-- marrow:{MILESTONE_KEY}:start -->"
_M1 = f"<!-- marrow:{MILESTONE_KEY}:end -->"

# Mirrors render_milestone output (H5 + paragraph, 2026-05-24):
#   ##### [YYYY-MM-DD] subject       (Us / dated Me — full date in bracket)
#   ##### [YYYY] subject             (Me — year-only date in bracket, legacy)
#   ##### [<title>]                  (Me historical — title fills bracket,
#                                     date stays in DB; only valid when row
#                                     carries `<!-- id:N -->` anchor so we
#                                     can pull date from the existing row)
#   description paragraph. <!-- id:N -->
_H5_RE = re.compile(
    r"^##### \[(?P<date>\d{4}(?:-\d{2}-\d{2})?)\] (?P<title>.+?)\s*$"
)
_H5_AGE_RE = re.compile(r"^##### \[(?P<title>.+?)\]\s*$")
_ID_RE = re.compile(r"<!-- id:(?P<id>\d+) -->")


@dataclass
class ReconcileReport:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    conflicts: list[str] = field(default_factory=list)

    def any_change(self) -> bool:
        return self.inserted or self.updated or self.deleted


def _parse(md_text: str) -> list[dict]:
    """Yield row dicts in section order.

    Block boundary = H5 heading `##### [date] subject`. Body lines below
    the heading (until the next H5 / `## ` section / end-of-marker) form
    the description paragraph; the `<!-- id:N -->` anchor may live
    anywhere in the body (inline-tail by render convention).
    """
    rows: list[dict] = []
    section: str | None = None
    in_block = False
    cur: dict | None = None
    body: list[str] = []

    def flush():
        nonlocal cur, body
        if cur is None:
            return
        text = "\n".join(body).strip()
        m_id = _ID_RE.search(text)
        if m_id:
            cur["id"] = int(m_id.group("id"))
            text = _ID_RE.sub("", text).strip()
        cur["description"] = text or None
        rows.append(cur)
        cur = None
        body = []

    for line in md_text.splitlines():
        s = line.rstrip()
        if _M0 in s:
            in_block = True
            continue
        if _M1 in s:
            flush()
            in_block = False
            continue
        if not in_block:
            continue
        if s.startswith("## ") and not s.startswith("##### "):
            flush()
            head = s[3:].strip().lower()
            if head == "us":
                section = "us"
            elif head == "me":
                section = "me"
            else:
                section = None
            continue
        m = _H5_RE.match(s)
        if m and section is not None:
            flush()
            cur = {
                "scope": section,
                "date": m.group("date"),
                "title": m.group("title").strip(),
                "theme": None,
                "pinned": 1,
                "description": None,
                "id": None,
            }
            continue
        # Historical Me — single-bracket form `##### [<title>]` (no date in
        # bracket). Only honoured under `## Me`; date is unknown here and
        # must be recovered from DB via the row's id anchor.
        m_age = _H5_AGE_RE.match(s)
        if m_age and section == "me":
            flush()
            cur = {
                "scope": section,
                "date": None,
                "title": m_age.group("title").strip(),
                "theme": None,
                "pinned": 1,
                "description": None,
                "id": None,
            }
            continue
        # Stale legacy `- [date] ...` bullet rows from a pre-H5 file are
        # NOT body — skip them so they don't pollute the H5 description.
        if s.startswith("- "):
            continue
        if cur is not None and s:
            body.append(s)
    flush()
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
    - No-op when md == current state: no writes, no audit.
    - Failures surface via rpt.conflicts -> alert in caller. No md backup
      taken; render is the SoT and atomic write keeps the previous md
      intact until the new one lands.
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

    with conn:
        # inserts + updates
        for row in md_rows:
            rid = row["id"]
            # Single-bracket historical Me rows have no date in the md;
            # inherit the DB's existing date so diff doesn't flap.
            if rid is not None and rid in db_rows and row["date"] is None:
                row["date"] = db_rows[rid]["date"] or ""
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
                if row["date"] is None:
                    # Unanchored single-bracket Me row carries no date.
                    # We can't synthesise one; report conflict so Lumi can
                    # rewrite the heading with a date or restore the anchor.
                    rpt.conflicts.append(
                        f"single-bracket row needs date or id anchor: "
                        f"{row['title'][:40]}"
                    )
                    continue
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
            conn.execute("DELETE FROM milestones WHERE id=?", (rid,))
            _audit(conn, rid, "delete", "md-reconcile: removed from md")
            rpt.deleted += 1

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
# Trail marker emitted by render_milestone_candidate so reconcile can tell
# "row deleted in Obsidian" apart from "row never rendered this round".
_CAND_TRAIL_RE = re.compile(r"<!-- cand:milestone:ids=\[([0-9,\s]*)\] -->")


def _milestone_natkey_hash(scope: str, date: str, title: str) -> str:
    """Identity hash for anti-revive tombstones — `milestones|scope|date|title`.
    Mirrors migrate._milestone_natural_key so the generic backfill path and
    sonnet-candidate writes both skip rows Lumi has dropped.
    """
    nk = f"milestones|{scope}|{date}|{title}"
    return hashlib.sha256(nk.encode()).hexdigest()


def _parse_dashboard_candidates(text: str) -> tuple[list[dict], list[int] | None]:
    """Scan dashboard md for milestone-candidate rows.

    Returns (votes, trail_ids):
    - votes: [{id, vote}] where vote ∈ {"pin", "drop", "edit"}. Rows missing
      a decision char are skipped. Multiple chars on one row resolve drop >
      pin > edit (destructive wins so leftover ✅ alongside ❌ still drops).
    - trail_ids: list of ids from the trail marker `<!-- cand:milestone:
      ids=[...] -->` — i.e. ids the renderer wrote last round. None when no
      marker present (legacy / first-render dashboard; skip drop-by-absence).
    """
    found: list[dict] = []
    trail: list[int] | None = None
    in_block = False
    for raw in text.splitlines():
        s = raw.rstrip()
        if "## Milestone candidate" in s:
            in_block = True
            continue
        if in_block and s.startswith("## "):
            in_block = False
        if in_block:
            tm = _CAND_TRAIL_RE.search(s)
            if tm:
                inside = tm.group(1).strip()
                trail = []
                if inside:
                    for tok in inside.split(","):
                        tok = tok.strip()
                        if tok.isdigit():
                            trail.append(int(tok))
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
    return found, trail


def _scan_candidate_ids(text: str) -> set[int]:
    """Set of milestone-candidate ids surviving in dashboard md (anchor scan,
    independent of emoji votes). Used by reconcile to detect "row deleted".
    """
    ids: set[int] = set()
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
        if _CAND_TRAIL_RE.search(s):
            continue
        m = _CAND_ID_RE.search(s)
        if m and m.group(1).isdigit():
            ids.add(int(m.group(1)))
    return ids


def _drop_milestone_candidate(conn, rid: int, source: str) -> None:
    """DELETE + write anti-revive tombstone keyed on natural_key hash.
    Summary stays `sha=<hash>|title=<title>` so migrate._insert and
    candidates.write_milestone_cand can LIKE-match the same key.
    """
    row = conn.execute(
        "SELECT scope, date, title FROM milestones WHERE id=?", (rid,)
    ).fetchone()
    if row is None:
        return
    h = _milestone_natkey_hash(
        row["scope"] or "", row["date"] or "", row["title"] or "",
    )
    conn.execute("DELETE FROM milestones WHERE id=?", (rid,))
    tomb = f"sha={h}|title={(row['title'] or '')[:80]}"
    _audit(conn, rid, "tombstone", f"{source}: {tomb}")


def reconcile_milestone_candidates(conn: sqlite3.Connection,
                                    dashboard_path: Path) -> ReconcileReport:
    """Reconcile dashboard `## Milestone candidate` rows back to DB.

    Three drop paths, one outcome — DELETE row + write anti-revive tombstone:
    - ❌ vote (legacy emoji-vote workflow)
    - Row deleted in Obsidian (id present in last-round trail marker but
      missing from md this round) — natural "delete the bullet to drop"
    Pin:
    - ✅ vote → pinned=1 (row appears on milestone subpage next render).
      (Or: copy the row's `<!-- id:N -->` into the milestone subpage —
      reconcile_milestones picks it up and flips pinned=1 there.)
    ✏️ → no-op (md re-rendered to DB state; HTML layer realises edits).
    """
    rpt = ReconcileReport()
    dashboard_path = Path(dashboard_path)
    if not dashboard_path.exists():
        return rpt
    text = dashboard_path.read_text(encoding="utf-8")
    votes, trail = _parse_dashboard_candidates(text)
    # Collect every anchored id surviving in the candidate block — votes
    # alone miss bare rows (no emoji edit), so re-scan the block for raw
    # `<!-- id:N -->` anchors. Trail marker presence = candidate block
    # rendered cleanly this round; intersect against it to spot deletions.
    md_ids = _scan_candidate_ids(text)
    missing: list[int] = []
    if trail is not None:
        missing = [i for i in trail if i not in md_ids]
    if not votes and not missing:
        return rpt
    with conn:
        for v in votes:
            rid = v["id"]
            row = conn.execute(
                "SELECT id, pinned, title FROM milestones WHERE id=?", (rid,)
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
                _drop_milestone_candidate(conn, rid, "dashboard ❌")
                rpt.deleted += 1
            else:  # edit — no-op until HTML layer realises in-place edits
                _audit(conn, rid, "edit_noop", "dashboard ✏️ (no md edit path)")
                rpt.unchanged += 1
        for rid in missing:
            row = conn.execute(
                "SELECT id, pinned FROM milestones WHERE id=?", (rid,)
            ).fetchone()
            if row is None:
                continue
            if row["pinned"]:
                # Lumi may have promoted the row by copying its anchor into
                # the milestone subpage — the candidate-block row vanishing
                # is then expected, not a drop. Skip.
                continue
            _drop_milestone_candidate(conn, rid, "dashboard row deleted")
            rpt.deleted += 1
    return rpt


# ── task reconcile ────────────────────────────────────────────────────────────

_TASK_ROW_RE = re.compile(
    r"^- \[(?P<check>[ x])\] .*<!-- id:(?P<id>\d+) -->\s*$"
)
_TASK_TRAIL_RE = re.compile(
    r"<!-- cand:task:ids=\[(?P<ids>[^\]]*)\] -->"
)
_TASKS_H2 = "## Tasks"


def _task_audit(conn, tid: int, action: str, summary: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (target_table, target_id, action, summary) "
        "VALUES ('tasks', ?, ?, ?)",
        (str(tid), action, summary),
    )


def reconcile_tasks(conn: sqlite3.Connection,
                    dashboard_path: str | Path) -> ReconcileReport:
    """Apply tick/untick edits from the dashboard Tasks block back to DB.

    Contract:
    - Row with <!-- id:N -->: [x] + active in DB -> UPDATE status='done'.
    - Row with <!-- id:N -->: [ ] + done in DB -> UPDATE status='active'.
    - id in trail marker but absent from rendered rows this pass -> archived.
    - No-op when trail marker is absent (legacy dashboard, no anchors yet).
    - Anchored id not found in DB -> conflict (logged, not fatal).
    """
    rpt = ReconcileReport()
    dashboard_path = Path(dashboard_path)
    if not dashboard_path.exists():
        return rpt

    text = dashboard_path.read_text(encoding="utf-8")

    # Locate ## Tasks block: from the heading to the next ## heading (or EOF).
    start = text.find(_TASKS_H2)
    if start == -1:
        return rpt
    after_h2 = text[start + len(_TASKS_H2):]
    next_h2 = re.search(r"\n##\s", after_h2)
    block = after_h2[: next_h2.start()] if next_h2 else after_h2

    # Require trail marker — no-op on legacy dashboards.
    trail_m = _TASK_TRAIL_RE.search(block)
    if not trail_m:
        return rpt

    trail_ids: set[int] = set()
    raw_ids = trail_m.group("ids").strip()
    if raw_ids:
        for part in raw_ids.split(","):
            part = part.strip()
            if part.isdigit():
                trail_ids.add(int(part))

    # Parse rows with anchors.
    anchored: dict[int, str] = {}  # id -> "x" or " "
    for line in block.splitlines():
        m = _TASK_ROW_RE.match(line)
        if m:
            anchored[int(m.group("id"))] = m.group("check")

    if not trail_ids and not anchored:
        return rpt

    # Load all tasks referenced by anchored ids in one query.
    all_ids = trail_ids | set(anchored.keys())
    placeholders = ",".join("?" for _ in all_ids)
    db_rows: dict[int, str] = {}
    if all_ids:
        for row in conn.execute(
            f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
            list(all_ids),
        ).fetchall():
            db_rows[row["id"]] = row["status"]

    with conn:
        # tick / untick anchored rows
        for tid, check in anchored.items():
            if tid not in db_rows:
                rpt.conflicts.append(f"anchored id {tid} not in db")
                continue
            current = db_rows[tid]
            if check == "x" and current == "active":
                conn.execute(
                    "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
                    (_now(), tid),
                )
                _task_audit(conn, tid, "tick", "md-reconcile: ticked done")
                rpt.updated += 1
            elif check == " " and current == "done":
                conn.execute(
                    "UPDATE tasks SET status='active', updated_at=? WHERE id=?",
                    (_now(), tid),
                )
                _task_audit(conn, tid, "untick", "md-reconcile: unticked active")
                rpt.updated += 1
            else:
                rpt.unchanged += 1

        # archive: ids in trail but not present as anchored rows in this block
        for tid in trail_ids:
            if tid in anchored:
                continue  # still rendered
            current = db_rows.get(tid)
            if current is None:
                continue  # already gone
            if current in ("done", "archived"):
                continue  # already terminal
            conn.execute(
                "UPDATE tasks SET status='archived', updated_at=? WHERE id=?",
                (_now(), tid),
            )
            _task_audit(conn, tid, "archive",
                        "md-reconcile: removed from dashboard")
            rpt.deleted += 1

    return rpt
