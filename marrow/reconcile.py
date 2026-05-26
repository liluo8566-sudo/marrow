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
    r"^- \[(?P<check>[ x])\] (?P<body>.*?)\s*<!-- id:(?P<id>\d+) -->\s*$"
)
# Same row shape minus the `<!-- id:N -->` anchor — used to detect rows Lumi
# typed into the dashboard by hand. INSERTed by reconcile_tasks so the next
# render replaces them with the canonical anchored body.
_TASK_ROW_NOID_RE = re.compile(
    r"^- \[(?P<check>[ x])\] (?P<body>.+?)\s*$"
)
# Category whitelist matches top_sections._TAG_ORDER — anything outside falls
# back to `Project` (mirrors the renderer's None-fallback intent for typed rows).
_TASK_CATEGORIES = ("Study", "Project", "Appointment", "Daily", "Others")
# Allow any non-ws text in tag — render emits Title/None-fallback Others so it
# may carry spaces in user-renamed categories. The first `]` ends the tag.
_TAG_PREFIX_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s+(?P<rest>.*)$")
_TRAILING_DATE_RE = re.compile(r"^(?P<rest>.*?)\s+\[(?P<date>[^\]]+)\]\s*$")
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


def _parse_task_row_body(body: str, db_title: str | None,
                         db_next_step: str | None) -> tuple[str, str | None] | None:
    """Recover (title, next_step) from an edited task row body.

    Render shape: `[<tag>] <title>{: <next_step>}{ [<date>]}`. Title and
    next_step both may contain `: ` internally so we can't naively split —
    we anchor against the DB values to identify which field was edited:

    - body endswith `: <db_next_step>`  → title-only edit
    - body startswith `<db_title>: `    → next_step-only edit
    - db_next_step is None              → whole body is title
    - none of the above                 → return None (ambiguous; caller logs)
    """
    text = body.strip()
    m = _TAG_PREFIX_RE.match(text)
    if m:
        text = m.group("rest").strip()
    dm = _TRAILING_DATE_RE.match(text)
    if dm:
        text = dm.group("rest").rstrip()
    if not text:
        return None
    db_title_v = (db_title or "").strip()
    if db_next_step:
        suffix = f": {db_next_step}"
        if text.endswith(suffix):
            return (text[: -len(suffix)].rstrip(), db_next_step)
        prefix = f"{db_title_v}: "
        if db_title_v and text.startswith(prefix):
            new_ns = text[len(prefix):].strip()
            return (db_title_v, new_ns or None)
        # next_step deleted entirely, title kept — body equals db_title.
        if db_title_v and text == db_title_v:
            return (db_title_v, None)
        return None
    # db had no next_step. Title may legitimately contain `: ` (e.g. Lumi's
    # task 148 title = "mw-phase 3: Almost done") — never split a body that
    # already matches db_title.
    if db_title_v and text == db_title_v:
        return (db_title_v, None)
    if ": " in text:
        head, _, tail = text.partition(": ")
        head = head.rstrip()
        tail = tail.strip()
        if head and tail:
            return (head, tail)
    return (text, None)


def _parse_unanchored_task_body(body: str) -> dict | None:
    """Pull (category, title, next_step, due) from a hand-typed row body.

    Shape mirrors render_tasks output minus the anchor:
        [<tag>] <title>{: <next_step>}{ [<date>]}

    Returns None when the body lacks a non-empty title (malformed). The tag
    prefix is optional; missing or unrecognised → 'Project'. The trailing
    `[<date>]` and `: <next_step>` suffixes are optional and peel in that
    order to avoid swallowing a colon inside the title.
    """
    text = body.strip()
    if not text:
        return None
    cat = "Project"
    m = _TAG_PREFIX_RE.match(text)
    if m:
        raw_tag = m.group("tag").strip()
        cap = raw_tag.capitalize()
        cat = cap if cap in _TASK_CATEGORIES else "Project"
        text = m.group("rest").strip()
    due: str | None = None
    dm = _TRAILING_DATE_RE.match(text)
    if dm:
        due = dm.group("date").strip() or None
        text = dm.group("rest").rstrip()
    next_step: str | None = None
    if ":" in text:
        head, _, tail = text.rpartition(":")
        head = head.rstrip()
        tail = tail.strip()
        if head and tail:
            text = head
            next_step = tail
    title = text.strip()
    if not title:
        return None
    return {
        "category": cat,
        "title": title,
        "next_step": next_step,
        "due": due,
    }


def reconcile_tasks(conn: sqlite3.Connection,
                    dashboard_path: str | Path) -> ReconcileReport:
    """Apply md edits from the dashboard Tasks block back to DB.

    Absorbed edits — each becomes a DB UPDATE before re-render so the
    reconciled block can safely overwrite the body:
    - Tick `[ ] -> [x]` → status='done'.
    - Untick `[x] -> [ ]` → status='active'.
    - Title text edit → tasks.title UPDATE (peels tag/date/next_step
      suffixes; whatever remains between them is the title).
    - id in trail but missing from rendered rows → status='archived'.

    No-op when trail marker is absent (legacy dashboard).
    Anchored id not found in DB → conflict (logged, not fatal).
    Malformed row → conflict + skip; never crash refresh.
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

    # Parse rows with anchors. body retained for title diff. Unanchored
    # task-shaped rows are collected separately — they're hand-typed inserts.
    anchored: dict[int, tuple[str, str]] = {}  # id -> (check, body)
    unanchored: list[tuple[str, str]] = []  # (check, body)
    for line in block.splitlines():
        m = _TASK_ROW_RE.match(line)
        if m:
            anchored[int(m.group("id"))] = (m.group("check"), m.group("body"))
            continue
        m2 = _TASK_ROW_NOID_RE.match(line)
        if m2:
            unanchored.append((m2.group("check"), m2.group("body")))

    if not trail_ids and not anchored and not unanchored:
        return rpt

    # Load all tasks referenced by anchored ids in one query.
    all_ids = trail_ids | set(anchored.keys())
    placeholders = ",".join("?" for _ in all_ids)
    db_rows: dict[int, dict] = {}
    if all_ids:
        for row in conn.execute(
            f"SELECT id, status, title, next_step FROM tasks "
            f"WHERE id IN ({placeholders})",
            list(all_ids),
        ).fetchall():
            db_rows[row["id"]] = dict(row)

    with conn:
        # tick / untick / title-edit anchored rows
        for tid, (check, body) in anchored.items():
            if tid not in db_rows:
                rpt.conflicts.append(f"anchored id {tid} not in db")
                continue
            row = db_rows[tid]
            current = row["status"]
            status_changed = False
            if check == "x" and current == "active":
                conn.execute(
                    "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
                    (_now(), tid),
                )
                _task_audit(conn, tid, "tick", "md-reconcile: ticked done")
                rpt.updated += 1
                status_changed = True
            elif check == " " and current == "done":
                conn.execute(
                    "UPDATE tasks SET status='active', updated_at=? WHERE id=?",
                    (_now(), tid),
                )
                _task_audit(conn, tid, "untick", "md-reconcile: unticked active")
                rpt.updated += 1
                status_changed = True
            # Title / next_step edit absorption — parse the body and UPDATE
            # whichever field changed. Done independently of tick/untick so
            # both can land in one pass.
            try:
                parsed = _parse_task_row_body(
                    body, row.get("title"), row.get("next_step")
                )
            except Exception as e:  # noqa: BLE001 — parse must never crash
                rpt.conflicts.append(f"task row {tid} malformed: {e}")
                if not status_changed:
                    rpt.unchanged += 1
                continue
            if parsed is None:
                rpt.conflicts.append(
                    f"task row {tid} ambiguous edit; keeping DB"
                )
                if not status_changed:
                    rpt.unchanged += 1
                continue
            md_title, md_next_step = parsed
            db_title = row.get("title") or ""
            db_next_step = row.get("next_step")
            title_changed = bool(md_title) and md_title != db_title
            ns_changed = md_next_step != db_next_step
            if title_changed or ns_changed:
                sets = []
                params: list = []
                if title_changed:
                    sets.append("title=?")
                    params.append(md_title)
                if ns_changed:
                    sets.append("next_step=?")
                    params.append(md_next_step)
                sets.append("updated_at=?")
                params.append(_now())
                params.append(tid)
                conn.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE id=?",
                    params,
                )
                bits = []
                if title_changed:
                    bits.append(f"title={md_title[:60]}")
                if ns_changed:
                    bits.append(f"next_step={(md_next_step or '<null>')[:60]}")
                _task_audit(conn, tid, "retitle",
                            "md-reconcile: " + " ".join(bits))
                rpt.updated += 1
            elif not status_changed:
                rpt.unchanged += 1

        # archive: ids in trail but not present as anchored rows in this block.
        # done rows count too — render keeps them on dashboard for the 6AM
        # cutoff window, so deleting them mid-day must stick (status='done'
        # → 'archived' makes render skip them).
        for tid in trail_ids:
            if tid in anchored:
                continue  # still rendered
            row = db_rows.get(tid)
            if row is None:
                continue  # already gone
            current = row["status"]
            if current == "archived":
                continue  # already terminal
            conn.execute(
                "UPDATE tasks SET status='archived', updated_at=? WHERE id=?",
                (_now(), tid),
            )
            _task_audit(conn, tid, "archive",
                        f"md-reconcile: removed from dashboard (was {current})")
            rpt.deleted += 1

        # insert: hand-typed unanchored rows → new tasks. Next render emits
        # them with `<!-- id:N -->` anchors so subsequent passes treat them
        # as ordinary anchored rows.
        _insert_unanchored_tasks(conn, unanchored, rpt)

    return rpt


def _insert_unanchored_tasks(conn: sqlite3.Connection,
                              rows: list[tuple[str, str]],
                              rpt: ReconcileReport) -> None:
    """INSERT new tasks for each unanchored row in the Tasks block.

    Status follows the checkbox (`[x]` → done, otherwise active). Dedup:
    if an active task with the same `(category, title)` already exists,
    skip the insert and log via add_alert(info) — see DECISIONS.md.
    Malformed body (missing title) → warn alert, skip.
    """
    if not rows:
        return
    now = _now()
    for check, body in rows:
        try:
            parsed = _parse_unanchored_task_body(body)
        except Exception as e:  # noqa: BLE001 — never crash refresh on a typo
            rpt.conflicts.append(f"unanchored task malformed: {e}")
            continue
        if parsed is None:
            try:
                from . import repo as _repo  # local import to dodge cycles
                _repo.add_alert(
                    "warn", "tasks",
                    f"unanchored task row missing title: {body[:80]}",
                    source="reconcile",
                )
            except Exception:
                pass
            continue
        status = "done" if check == "x" else "active"
        # Dedup: refuse to insert another active task with the same
        # (category, title) — keeps repeated mw refresh idempotent if Lumi
        # forgot to delete her hand-typed line. Silent — no alert; the next
        # render rewrites the hand-typed line as the canonical anchored row.
        dup = conn.execute(
            "SELECT id FROM tasks "
            "WHERE status='active' AND category=? AND title=? LIMIT 1",
            (parsed["category"], parsed["title"]),
        ).fetchone()
        if dup is not None:
            continue
        # Cosine dedup across all active tasks (cross-category — Lumi may
        # have hand-typed the category wrong, so don't trust it as a
        # partitioning key here).
        from . import semantic_dedup as _sd
        cos_targets = [
            r["title"] for r in conn.execute(
                "SELECT title FROM tasks WHERE status='active'"
            ).fetchall()
        ]
        cos = _sd.cosine_max(conn, parsed["title"], cos_targets)
        if cos is None:
            _sd.warn_embedder_missing(
                conn, "tasks_dedup_no_embedder",
                "reconcile._insert_unanchored_tasks",
            )
        elif cos >= _sd.threshold_for("tasks"):
            continue
        cur = conn.execute(
            "INSERT INTO tasks "
            "(category, title, due, next_step, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (parsed["category"], parsed["title"], parsed["due"],
             parsed["next_step"], status, now, now),
        )
        new_id = cur.lastrowid
        _task_audit(conn, new_id, "insert",
                     f"md-reconcile: typed row title={parsed['title'][:60]}")
        rpt.inserted += 1


# ── affect reconcile ──────────────────────────────────────────────────────────

# Affect rows in the dashboard carry their ids inline at end-of-line, parity
# with task `<!-- id:N -->`. Bullet shapes:
#   - 【tone】 · eph<N> label | desc · epl<N> label | desc [<ago>|24h|7d] <!-- aff:<id1>,<id2> -->
#   - [ ] <desc> <!-- id:affect.N -->                                       (Pending — per-row anchor)
_AFFECT_ID_RE = re.compile(r"<!-- id:affect\.(?P<id>\d+) -->")
_AFFECT_TRAIL_RE = re.compile(r"<!--\s*aff:(?P<ids>[0-9,\s]*?)\s*-->")
# Segment parser: `eph<N> <label> | <desc>` or `epl<N> <label> | <desc>`.
_AFFECT_EP_SEG_RE = re.compile(
    r"^ep[hl]\d+\s+(?P<label>.+?)\s*\|\s*(?P<desc>.+?)\s*$"
)
_AFFECT_PENDING_RE = re.compile(
    r"^\s*-\s+\[[ x]\]\s+(?P<text>.+?)\s*$"
)
# Trailing ` [<token>]` suffix on Today/24h/7d lines (e.g. ` [1m ago]`, ` [24h]`).
_AFFECT_AGO_SUFFIX_RE = re.compile(r"\s+\[[^\]]+\]\s*$")
# Stand-alone trailing `[N(m|h|d|hr|min|hour|day)s? ago]` / `[24h]` / `[7d]`
# suffix used by the sanitizer to strip render-leakage from free text.
_AFFECT_TAG_TAIL_RE = re.compile(
    r"\s*\[\d+\s*(?:m|min|h|hr|hour|d|day)s?\s+ago\]\s*$",
    re.IGNORECASE,
)
_AFFECT_WINDOW_TAIL_RE = re.compile(r"\s*\[(?:24h|7d)\]\s*$", re.IGNORECASE)
# Middle-dot separator used to join tone header with ep segments and segments
# with each other. Literal: space + U+00B7 + space.
_AFFECT_SEG_SEP = " · "
_AFFECT_H2 = "## Affect"


def _affect_audit(conn, aid: int, action: str, summary: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (target_table, target_id, action, summary) "
        "VALUES ('affect', ?, ?, ?)",
        (str(aid), action, summary),
    )


def _parse_affect_trail_ids(trail_line: str) -> list[int]:
    """Pull the comma-separated affect ids out of a `<!-- aff:1,2,3 -->` token."""
    m = _AFFECT_TRAIL_RE.search(trail_line)
    if not m:
        return []
    ids: list[int] = []
    for tok in m.group("ids").split(","):
        tok = tok.strip()
        if tok.isdigit():
            ids.append(int(tok))
    return ids


def _sanitize_affect_text(text: str | None) -> str | None:
    """Strip render leakage (anchor + time-tag suffix) from free-text fields.
    Root-cause fix: parser used to capture `<!-- aff:N -->` and `[Nm ago]`
    suffixes into label/description before the inline-anchor format. Always
    strip them on the way back into the DB so the description stays clean.
    Returns the same shape (None stays None, '' stays '').
    """
    if text is None:
        return None
    out = text
    # Strip any inline aff/id-affect anchors anywhere in the text.
    out = _AFFECT_TRAIL_RE.sub("", out)
    out = _AFFECT_ID_RE.sub("", out)
    # Peel trailing time tags repeatedly (cover `[24h] <!-- aff:N -->` chains).
    for _ in range(4):
        prev = out
        out = _AFFECT_TAG_TAIL_RE.sub("", out)
        out = _AFFECT_WINDOW_TAIL_RE.sub("", out)
        if out == prev:
            break
    return out.strip()


def _scrub_affect_pollution(conn: sqlite3.Connection) -> int:
    """One-shot idempotent cleanup of affect.description / .label rows whose
    text was polluted by the prior trail-marker render bug. Cheap when
    nothing matches (no-op UPDATE skipped via WHERE), runs every reconcile.
    Returns rows touched.
    """
    rows = conn.execute(
        "SELECT id, label, description FROM affect "
        "WHERE description LIKE '%<!-- aff:%' "
        "   OR description LIKE '%<!-- id:affect.%' "
        "   OR label LIKE '%<!-- aff:%' "
        "   OR label LIKE '%<!-- id:affect.%'"
    ).fetchall()
    touched = 0
    with conn:
        for r in rows:
            new_desc = _sanitize_affect_text(r["description"])
            new_label = _sanitize_affect_text(r["label"])
            if (new_desc != r["description"]) or (new_label != r["label"]):
                conn.execute(
                    "UPDATE affect SET label=?, description=? WHERE id=?",
                    (new_label, new_desc, r["id"]),
                )
                _affect_audit(conn, r["id"], "scrub",
                              "stripped render-leakage anchor/time-tag")
                touched += 1
    return touched


def _parse_affect_segments(line: str, ids: list[int]
                           ) -> list[tuple[int, str | None, str | None]]:
    """Extract (id, label, description) tuples from a Today/Week affect bullet.

    Bullet shape:
      - 【tone】 · ep{h|l}N <label> | <desc> · ep{h|l}N <label> | <desc> [<ago>] <!-- aff:... -->

    `ids` comes from the inline `<!-- aff:... -->` anchor at end-of-line
    (left-to-right segment order). Strategy: strip the trailing anchor +
    ` [<...>]` suffix, split by middle-dot separator ` · `, drop the first
    segment (tone header), then pair each remaining segment with the next
    id from `ids`. Segments that don't match the ep shape contribute
    (id, None, None) so caller can mark them unchanged without crashing.
    Description/label are run through the sanitizer before return so any
    accidental anchor/tag leftover never reaches the DB.
    """
    body = line.rstrip()
    body = _AFFECT_TRAIL_RE.sub("", body).rstrip()
    body = _AFFECT_AGO_SUFFIX_RE.sub("", body)
    parts = body.split(_AFFECT_SEG_SEP)
    # parts[0] is the tone-header segment (`- 【tone】`) — skip.
    segments = parts[1:] if len(parts) > 1 else []
    out: list[tuple[int, str | None, str | None]] = []
    for seg, aid in zip(segments, ids):
        inner = seg.strip()
        m = _AFFECT_EP_SEG_RE.match(inner)
        if m:
            out.append((
                aid,
                _sanitize_affect_text(m.group("label").strip()),
                _sanitize_affect_text(m.group("desc").strip()),
            ))
        else:
            out.append((aid, None, None))
    return out


def _parse_affect_pending_line(line: str, db_label: str | None,
                                db_desc: str | None
                                ) -> tuple[str | None, str | None]:
    """Recover (label, description) for a Pending row `- [ ] <text> <anchor>`."""
    body = _AFFECT_ID_RE.sub("", line).rstrip()
    m = _AFFECT_PENDING_RE.match(body)
    if not m:
        return None, None
    text = _sanitize_affect_text(m.group("text").strip()) or ""
    if db_desc and text == db_desc:
        return None, db_desc  # unchanged
    if db_label and text == db_label and not db_desc:
        return db_label, None
    return None, text


def reconcile_affect(conn: sqlite3.Connection,
                     dashboard_path: str | Path) -> ReconcileReport:
    """Absorb dashboard `## Affect` description/label edits back into the
    affect table. Today/Week bullets carry an inline end-of-line anchor
    `<!-- aff:id1,id2[,id3,id4] -->` paired left-to-right with each ep
    segment. Pending rows are one `<!-- id:affect.N -->` per line. Aggregate
    stats text outside anchored segments is left alone.

    Always scrubs known affect-row pollution (anchor/time-tag leakage) up
    front — cheap when nothing matches.

    No-op when the affect block has no anchored segments (cold-start / empty).
    """
    rpt = ReconcileReport()
    # Idempotent cleanup of historic render leakage (free-text fields holding
    # `<!-- aff:N -->` or `[Nm ago]` suffixes). Runs every reconcile — guard
    # via WHERE so the no-op path is one cheap LIKE scan.
    try:
        _scrub_affect_pollution(conn)
    except Exception as e:  # noqa: BLE001 — never block refresh
        rpt.conflicts.append(f"affect scrub failed: {e}")

    dashboard_path = Path(dashboard_path)
    if not dashboard_path.exists():
        return rpt
    text = dashboard_path.read_text(encoding="utf-8")
    start = text.find(_AFFECT_H2)
    if start == -1:
        return rpt
    after_h2 = text[start + len(_AFFECT_H2):]
    next_h2 = re.search(r"\n##\s", after_h2)
    block = after_h2[: next_h2.start()] if next_h2 else after_h2

    # Anchored bullets and pending rows live in the same block; the inline
    # `<!-- aff:... -->` anchor identifies Today/Week ep bullets while the
    # per-row `<!-- id:affect.N -->` identifies Pending rows.
    ep_segs: dict[int, tuple[str | None, str | None]] = {}
    pending_lines: dict[int, str] = {}
    in_pending = False
    for raw in block.splitlines():
        s = raw.rstrip()
        stripped = s.lstrip()
        if stripped.startswith("### Pending"):
            in_pending = True
            continue
        if stripped.startswith("### "):
            in_pending = False
            continue
        if in_pending:
            m_id = _AFFECT_ID_RE.search(s)
            if m_id:
                try:
                    aid = int(m_id.group("id"))
                except ValueError:
                    continue
                pending_lines[aid] = s
            continue
        # Today / This Week bullet — inline anchor at end-of-line.
        if not stripped.startswith("- 【"):
            continue
        ids = _parse_affect_trail_ids(s)
        if not ids:
            continue
        try:
            for aid, lbl, desc in _parse_affect_segments(s, ids):
                ep_segs[aid] = (lbl, desc)
        except Exception as e:  # noqa: BLE001 — never crash refresh
            rpt.conflicts.append(f"affect line malformed: {e}")

    all_ids = set(ep_segs) | set(pending_lines)
    if not all_ids:
        return rpt

    placeholders = ",".join("?" for _ in all_ids)
    db_rows: dict[int, dict] = {}
    for row in conn.execute(
        f"SELECT id, label, description FROM affect "
        f"WHERE id IN ({placeholders})",
        list(all_ids),
    ).fetchall():
        db_rows[row["id"]] = dict(row)

    parsed: dict[int, tuple[str | None, str | None]] = dict(ep_segs)
    for aid, line in pending_lines.items():
        row = db_rows.get(aid)
        if row is None:
            parsed[aid] = (None, None)
            continue
        parsed[aid] = _parse_affect_pending_line(
            line, row.get("label"), row.get("description")
        )

    with conn:
        for aid, (new_label, new_desc) in parsed.items():
            row = db_rows.get(aid)
            if row is None:
                rpt.conflicts.append(f"anchored affect id {aid} not in db")
                continue
            updates: list[tuple[str, str | None]] = []
            db_label = row.get("label")
            db_desc = row.get("description")
            if new_label is not None and new_label != (db_label or ""):
                updates.append(("label", new_label))
            if new_desc is not None and new_desc != (db_desc or ""):
                updates.append(("description", new_desc))
            if not updates:
                rpt.unchanged += 1
                continue
            set_clause = ", ".join(f"{c}=?" for c, _ in updates)
            params = [v for _, v in updates] + [aid]
            conn.execute(f"UPDATE affect SET {set_clause} WHERE id=?", params)
            _affect_audit(
                conn, aid, "retext",
                f"md-reconcile: " + ", ".join(
                    f"{c}={(v or '')[:40]}" for c, v in updates
                ),
            )
            rpt.updated += 1
    return rpt
