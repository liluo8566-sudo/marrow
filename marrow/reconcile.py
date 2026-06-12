"""md->DB reconcile for sub-pages + dashboard top sections.

Scope today:
- milestone subpage (reconcile_milestones)
- memes subpage (reconcile_memes) — anchor-scan delete
- profile subpage (reconcile_profile) — anchor-scan soft-delete via superseded_by
- dashboard `## Milestone candidate` rows with anchor buttons
  (reconcile_milestone_candidates) — ✅ pin · ❌ delete+tombstone · ✏️ edit
- dashboard `## Tasks` block (reconcile_tasks) — tick/untick/archive
  via `<!-- id:N -->` anchors + `<!-- cand:task:ids=[...] -->` trail.

Vocab/pit plug in later via the same shape (parse(md)->rows, diff
against DB, apply). reconcile_* are the public entries; the dashboard
writer wires the candidate + task passes via write_dashboard.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from ._atomic import atomic_write as _atomic_write


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

    for lineno, line in enumerate(md_text.splitlines()):
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
        # Strip any inline `<!-- id:N -->` anchor off the heading before
        # matching — bidirectional reconcile may have spliced the id onto
        # the heading line itself (BUG-1 fix). Keep the captured id for flush.
        heading_id: int | None = None
        s_for_h5 = s
        m_inline = _ID_RE.search(s)
        if m_inline:
            try:
                heading_id = int(m_inline.group("id"))
            except ValueError:
                heading_id = None
            s_for_h5 = _ID_RE.sub("", s).rstrip()
        m = _H5_RE.match(s_for_h5)
        if m and section is not None:
            flush()
            cur = {
                "scope": section,
                "date": m.group("date"),
                "title": m.group("title").strip(),
                "theme": None,
                "pinned": 1,
                "description": None,
                "id": heading_id,
                "_heading_line": lineno,
            }
            continue
        # Historical Me — single-bracket form `##### [<title>]` (no date in
        # bracket). Only honoured under `## Me`; date is unknown here and
        # must be recovered from DB via the row's id anchor.
        m_age = _H5_AGE_RE.match(s_for_h5)
        if m_age and section == "me":
            flush()
            cur = {
                "scope": section,
                "date": None,
                "title": m_age.group("title").strip(),
                "theme": None,
                "pinned": 1,
                "description": None,
                "id": heading_id,
                "_heading_line": lineno,
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
    md_text = md_path.read_text(encoding="utf-8")
    md_lines = md_text.splitlines()
    md_rows = _parse(md_text)
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
    # (heading_line_index, new_id) pairs to splice into md after INSERT lands.
    line_anchor_writes: list[tuple[int, int]] = []

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
                # Safety net: exact-match dedup. Prevents runaway loop if the
                # md anchor-write fails for any reason (file lock, perm, race).
                existing = conn.execute(
                    "SELECT id FROM milestones "
                    "WHERE scope=? AND date=? AND title=? LIMIT 1",
                    (row["scope"], row["date"], row["title"]),
                ).fetchone()
                if existing is not None:
                    new_id = existing["id"]
                else:
                    h = _hash(row)
                    cur = conn.execute(
                        "INSERT INTO milestones "
                        "(scope, date, title, description, theme, pinned, "
                        " source_hash) VALUES (?, ?, ?, ?, ?, 1, ?)",
                        (row["scope"], row["date"], row["title"],
                         row["description"], row["theme"], h),
                    )
                    new_id = cur.lastrowid
                    _audit(conn, new_id, "insert",
                           f"md-reconcile: {row['title'][:60]}")
                    rpt.inserted += 1
                # Queue heading-line anchor write so the next inserter pass
                # sees the row as present in md (prevents dup canonical block).
                hl = row.get("_heading_line")
                if hl is not None:
                    line_anchor_writes.append((hl, new_id))
                seen.add(new_id)

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

    # Splice new ids back into the user's heading lines (idempotent: skip if
    # the line already carries any `<!-- id:N -->` anchor). Atomic write keeps
    # the watcher from racing on a half-written file.
    if line_anchor_writes:
        changed = False
        for hl, new_id in line_anchor_writes:
            if hl < 0 or hl >= len(md_lines):
                continue
            line = md_lines[hl]
            if _ID_RE.search(line):
                continue
            md_lines[hl] = line.rstrip() + f" <!-- id:{new_id} -->"
            changed = True
        if changed:
            trailing_nl = "\n" if md_text.endswith("\n") else ""
            _atomic_write(str(md_path), "\n".join(md_lines) + trailing_nl)

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
_AFFECT_RENDERED_RE = re.compile(r"<!--\s*aff-rendered:(?P<ids>[0-9,\s]*?)\s*-->")
# Segment parser: `eph<N> <label> | <desc>` or `epl<N> <label> | <desc>`.
_AFFECT_EP_SEG_RE = re.compile(
    r"^ep[hl]\d+\s+(?P<label>.+?)\s*\|\s*(?P<desc>.+?)\s*$"
)
_AFFECT_PENDING_RE = re.compile(
    r"^\s*-\s+\[(?P<box>[ xX])\]\s+(?P<text>.+?)\s*$"
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
                                ) -> tuple[str | None, str | None, bool]:
    """Recover (label, description, resolved) for a Pending row.
    resolved=True when checkbox is `[x]`."""
    body = _AFFECT_ID_RE.sub("", line).rstrip()
    m = _AFFECT_PENDING_RE.match(body)
    if not m:
        return None, None, False
    resolved = m.group("box").lower() == "x"
    text = _sanitize_affect_text(m.group("text").strip()) or ""
    if db_desc and text == db_desc:
        return None, db_desc, resolved
    if db_label and text == db_label and not db_desc:
        return db_label, None, resolved
    return None, text, resolved


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
    rendered_ids: set[int] = set()
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
        m_rendered = _AFFECT_RENDERED_RE.search(stripped)
        if m_rendered:
            for tok in m_rendered.group("ids").split(","):
                tok = tok.strip()
                if tok.isdigit():
                    rendered_ids.add(int(tok))
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

    # md mtime gates the "deleted-from-md → resolved" path so newly-written
    # affect rows (created after the current md snapshot) are not mistaken
    # for user deletions.
    md_mtime_iso: str | None = None
    try:
        md_mtime_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(dashboard_path.stat().st_mtime)
        )
    except OSError:
        pass

    all_ids = set(ep_segs) | set(pending_lines)

    db_rows: dict[int, dict] = {}
    if all_ids:
        placeholders = ",".join("?" for _ in all_ids)
        for row in conn.execute(
            f"SELECT id, label, description FROM affect "
            f"WHERE id IN ({placeholders})",
            list(all_ids),
        ).fetchall():
            db_rows[row["id"]] = dict(row)

    parsed: dict[int, tuple[str | None, str | None]] = dict(ep_segs)
    pending_resolved: set[int] = set()
    for aid, line in pending_lines.items():
        row = db_rows.get(aid)
        if row is None:
            parsed[aid] = (None, None)
            continue
        new_label, new_desc, resolved = _parse_affect_pending_line(
            line, row.get("label"), row.get("description")
        )
        parsed[aid] = (new_label, new_desc)
        if resolved:
            pending_resolved.add(aid)

    # Deleted-from-md: rows that were eligible to render (unresolved=1,
    # superseded_by NULL, in 7d window) and predate the md snapshot but
    # are absent from pending_lines → user removed them.
    # Note: no zero-anchor guard here — affect has rendered anchors since
    # 2026-Q1, so a real legacy-md first-render is no longer plausible,
    # and Lumi's `delete all Pending rows → mass-resolve` IS the intended
    # gesture (test_reconcile_affect_delete_line_marks_resolved).
    deleted_resolved: set[int] = set()
    if md_mtime_iso is not None:
        week_cut = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(dashboard_path.stat().st_mtime - 7 * 86400),
        )
        for row in conn.execute(
            "SELECT id FROM affect WHERE superseded_by IS NULL "
            "AND unresolved=1 AND resolved_at IS NULL "
            "AND created_at>=? AND created_at<=?",
            (week_cut, md_mtime_iso),
        ).fetchall():
            if row["id"] not in pending_lines:
                deleted_resolved.add(row["id"])

    if not all_ids and not deleted_resolved and not rendered_ids:
        return rpt

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
            if updates:
                set_clause = ", ".join(f"{c}=?" for c, _ in updates)
                params = [v for _, v in updates] + [aid]
                conn.execute(
                    f"UPDATE affect SET {set_clause} WHERE id=?", params
                )
                _affect_audit(
                    conn, aid, "retext",
                    "md-reconcile: " + ", ".join(
                        f"{c}={(v or '')[:40]}" for c, v in updates
                    ),
                )
                rpt.updated += 1
            elif aid not in pending_resolved:
                rpt.unchanged += 1

        now_iso = _now()
        for aid in pending_resolved | deleted_resolved:
            conn.execute(
                "UPDATE affect SET unresolved=0, "
                "resolved_at=COALESCE(resolved_at, ?) WHERE id=? "
                "AND unresolved=1",
                (now_iso, aid),
            )
            action = "tick" if aid in pending_resolved else "delete"
            _affect_audit(
                conn, aid, "resolved", f"md-reconcile: {action}"
            )
            rpt.updated += 1

        # aff-anchor deletion: id was in last render's <!-- aff-rendered:... -->
        # but user removed it from the md (deleted ep bullet or edited anchor).
        anchor_deleted: set[int] = set()
        if rendered_ids and md_mtime_iso:
            anchor_deleted = rendered_ids - set(ep_segs) - set(pending_lines)
            for aid in list(anchor_deleted):
                row = conn.execute(
                    "SELECT id FROM affect WHERE id=? "
                    "AND superseded_by IS NULL AND created_at<=?",
                    (aid, md_mtime_iso),
                ).fetchone()
                if not row:
                    anchor_deleted.discard(aid)
        for aid in anchor_deleted:
            conn.execute(
                "UPDATE affect SET superseded_by=id WHERE id=? "
                "AND superseded_by IS NULL",
                (aid,),
            )
            _affect_audit(conn, aid, "superseded",
                          "md-reconcile: anchor-deleted")
            rpt.updated += 1

    return rpt


# ── inserter-subpage anchor-scan reconciles ───────────────────────────────────

_ANCHOR_RE = re.compile(r"<!-- id:(\d+) -->")


def _scan_anchored_ids(md_text: str) -> set[int]:
    """Collect every numeric `<!-- id:N -->` anchor in the file."""
    return {int(m.group(1)) for m in _ANCHOR_RE.finditer(md_text)}


def reconcile_memes(conn: sqlite3.Connection,
                    md_path: Path) -> ReconcileReport:
    """Delete/UPDATE memes rows from md edits. Delegates to reconcile_inserter."""
    from .reconcile_inserter import reconcile_memes as _reconcile_memes
    return _reconcile_memes(conn, md_path)


def reconcile_profile(conn: sqlite3.Connection,
                      md_path: Path) -> ReconcileReport:
    """Soft-delete/UPDATE entity rows from md edits. Delegates to reconcile_inserter."""
    from .reconcile_inserter import reconcile_profile as _reconcile_profile
    return _reconcile_profile(conn, md_path)


# ── alerts reconcile ─────────────────────────────────────────────────────────
_ALERT_H2 = "## Alerts"
_ALERT_ID_RE = re.compile(r"<!-- id:alert\.(?P<id>\d+) -->")


def reconcile_alerts(conn: sqlite3.Connection,
                     dashboard_path: str | Path) -> ReconcileReport:
    """Absorb md-side alert deletions back into the alerts table.

    Each rendered alert bullet carries `<!-- id:alert.N -->`. If Lumi
    removes a bullet from the dashboard md, that row is treated as
    `resolved=1` — the md-side delete IS the resolve gesture. Idempotent;
    no-op when md is absent or the Alerts block is missing.

    md mtime gates the deletion path so alerts created AFTER the md
    snapshot (e.g. by a background hook) are not misread as user deletes.
    """
    rpt = ReconcileReport()
    dashboard_path = Path(dashboard_path)
    if not dashboard_path.exists():
        return rpt
    text = dashboard_path.read_text(encoding="utf-8")
    start = text.find(_ALERT_H2)
    if start == -1:
        return rpt
    after_h2 = text[start + len(_ALERT_H2):]
    next_h2 = re.search(r"\n##\s", after_h2)
    block = after_h2[: next_h2.start()] if next_h2 else after_h2

    md_ids: set[int] = set()
    for raw in block.splitlines():
        m = _ALERT_ID_RE.search(raw)
        if not m:
            continue
        try:
            md_ids.add(int(m.group("id")))
        except ValueError:
            continue

    # Safety: zero anchors in the Alerts block means either first-render
    # against legacy md (no anchors yet) OR user wiped the entire block.
    # These are indistinguishable, so refuse to mass-resolve. Lumi uses
    # `mw resolve <id>` for nuke; this path handles the per-row case.
    if not md_ids:
        return rpt

    md_mtime_iso: str | None = None
    try:
        md_mtime_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(dashboard_path.stat().st_mtime),
        )
    except OSError:
        pass

    sql = "SELECT id FROM alerts WHERE resolved=0"
    params: list = []
    if md_mtime_iso:
        sql += " AND created_at <= ?"
        params.append(md_mtime_iso)
    db_unresolved = {r[0] for r in conn.execute(sql, params).fetchall()}

    deleted = db_unresolved - md_ids
    if not deleted:
        return rpt
    now_iso = _now()
    with conn:
        for aid in deleted:
            conn.execute(
                "UPDATE alerts SET resolved=1, "
                "resolved_at=COALESCE(resolved_at, ?) "
                "WHERE id=? AND resolved=0",
                (now_iso, aid),
            )
            rpt.deleted += 1
    return rpt


# ── timeline reconcile ────────────────────────────────────────────────────────

_TIMELINE_H2 = "## Timeline"
# Matches `<!-- tl:<sid> -->` (session) and `<!-- tl:d:YYYY-MM-DD -->` (diary).
_TL_SID_RE  = re.compile(r"<!--\s*tl:(?!d:)(?P<sid>\S+?)\s*-->")
_TL_DATE_RE = re.compile(r"<!--\s*tl:d:(?P<date>\d{4}-\d{2}-\d{2})\s*-->")
# Strip anchors from a line to get the user-editable text.
_TL_ANCHOR_RE = re.compile(r"\s*<!--\s*tl:[^>]+-->\s*$")
# Strip leading prefixes from timeline lines before extracting editable text.
# \s* (not \s+) so prefix-only stub lines strip to empty → no write-back.
_TL_HHMM_RE   = re.compile(r"^\d{2}:\d{2}\s*")
_TL_TONE_RE   = re.compile(r"^【[^】]*】\s*")
_TL_PERIOD_RE  = re.compile(r"^(?:AM|PM|ND)\s+")
# Diary day-line prefix: "MM-DD Day 【tone】" (day 4-8 zone).
_TL_DAY_RE    = re.compile(r"^\d{2}-\d{2}\s+Day\s+【[^】]*】\s*")


_TL_TRAIL_RE  = re.compile(r"<!--\s*tl-rendered:(?P<payload>[^>]+)\s*-->")
_TL_EVT_RE    = re.compile(r"<!--\s*tl:e:(?P<eid>\d+)\s*-->")
_TL_PLUS_RE   = re.compile(r"^\+\s+(?:(?P<hhmm>\d{2}:\d{2})\s+)?(?P<text>.+)$")
_TZ_MELB      = ZoneInfo("Australia/Melbourne")


def _strip_tl_anchor(line: str) -> str:
    return _TL_ANCHOR_RE.sub("", line).rstrip()


def _extract_tl_text(line: str) -> str:
    """Strip prefixes (HH:MM【tone】 / AM/PM/ND / MM-DD Day 【tone】) and anchor."""
    s = _strip_tl_anchor(line)
    s = _TL_HHMM_RE.sub("", s)
    s = _TL_TONE_RE.sub("", s)
    s = _TL_PERIOD_RE.sub("", s)
    s = _TL_DAY_RE.sub("", s)
    return s.strip()


def reconcile_timeline(conn: sqlite3.Connection,
                       dashboard_path: str | Path) -> ReconcileReport:
    """Absorb tl_line edits from the dashboard ## Timeline block back into DB.

    Anchors:
      `<!-- tl:<sid> -->` → session_digests.tl_line for that sid
      `<!-- tl:d:YYYY-MM-DD -->` → diary.tl_line for that date
      `<!-- tl:e:N -->` → events.content for manual event id N (edit)
      `<!-- tl-rendered:s=...;d=...;e=... -->` → trail marker; absent sid/date/evt = hidden

    Tone/label segments are display-only — only the text portion is written.
    Lines starting with `+ ` → insert as manual events (channel='manual').
    """
    rpt = ReconcileReport()
    dashboard_path = Path(dashboard_path)
    if not dashboard_path.exists():
        return rpt
    text = dashboard_path.read_text(encoding="utf-8")
    start = text.find(_TIMELINE_H2)
    if start == -1:
        return rpt
    after_h2 = text[start + len(_TIMELINE_H2):]
    next_h2 = re.search(r"\n##\s", after_h2)
    block = after_h2[: next_h2.start()] if next_h2 else after_h2

    # Parse trail marker first — tells us what was rendered last time
    trail_sids:  set[str] = set()
    trail_dates: set[str] = set()
    trail_evts:  set[int] = set()
    m_trail = _TL_TRAIL_RE.search(block)
    if m_trail:
        for segment in m_trail.group("payload").split(";"):
            segment = segment.strip()
            if segment.startswith("s="):
                trail_sids.update(x.strip() for x in segment[2:].split(",") if x.strip())
            elif segment.startswith("d="):
                trail_dates.update(x.strip() for x in segment[2:].split(",") if x.strip())
            elif segment.startswith("e="):
                trail_evts.update(int(x.strip()) for x in segment[2:].split(",") if x.strip())

    sid_edits:    dict[str, str] = {}
    date_edits:   dict[str, str] = {}
    present_sids:  set[str] = set()
    present_dates: set[str] = set()
    present_evts:  set[int] = set()
    plus_lines:    list[str] = []
    evt_edits:     dict[int, str] = {}

    for raw in block.splitlines():
        line = raw.rstrip()
        # Skip the trail marker line itself
        if _TL_TRAIL_RE.search(line):
            continue
        # Lines starting with `+ ` are manual add requests
        if _TL_PLUS_RE.match(line):
            plus_lines.append(line)
            continue
        # Manual event anchor
        m_evt = _TL_EVT_RE.search(line)
        if m_evt:
            eid = int(m_evt.group("eid"))
            present_evts.add(eid)
            # Extract editable text: strip the tl:e anchor + leading HH:MM prefix
            text_part = re.sub(r"\s*<!--\s*tl:e:\d+\s*-->\s*$", "", line).rstrip()
            text_part = _TL_HHMM_RE.sub("", text_part).strip()
            if text_part:
                evt_edits[eid] = text_part
            continue
        m_sid = _TL_SID_RE.search(line)
        if m_sid:
            sid = m_sid.group("sid")
            present_sids.add(sid)
            text_part = _extract_tl_text(line)
            if text_part:
                sid_edits[sid] = text_part
            continue
        m_date = _TL_DATE_RE.search(line)
        if m_date:
            date = m_date.group("date")
            present_dates.add(date)
            text_part = _extract_tl_text(line)
            if text_part:
                date_edits[date] = text_part

    if not sid_edits and not date_edits and not m_trail and not plus_lines and not evt_edits:
        return rpt

    now_iso = _now()
    with conn:
        # ── existing: session tl_line edits ──────────────────────────────────
        for sid, new_tl in sid_edits.items():
            row = conn.execute(
                "SELECT tl_line FROM session_digests WHERE sid = ?", (sid,)
            ).fetchone()
            if row is None:
                rpt.conflicts.append(f"tl:sid {sid!r} not in session_digests")
                continue
            db_tl = row["tl_line"] or ""
            if new_tl != db_tl:
                conn.execute(
                    "UPDATE session_digests SET tl_line = ? WHERE sid = ?",
                    (new_tl, sid),
                )
                conn.execute(
                    "INSERT INTO audit_log"
                    " (target_table, target_id, action, summary)"
                    " VALUES ('session_digests', ?, 'tl_edit', ?)",
                    (sid, f"md-reconcile: tl_line={new_tl[:60]!r}"),
                )
                rpt.updated += 1
            else:
                rpt.unchanged += 1

        # ── existing: diary tl_line edits ────────────────────────────────────
        for date, new_tl in date_edits.items():
            row = conn.execute(
                "SELECT tl_line FROM diary WHERE date = ?", (date,)
            ).fetchone()
            if row is None:
                rpt.conflicts.append(f"tl:d:{date} not in diary")
                continue
            db_tl = row["tl_line"] or ""
            if new_tl != db_tl:
                conn.execute(
                    "UPDATE diary SET tl_line = ?, updated_at = ? WHERE date = ?",
                    (new_tl, now_iso, date),
                )
                conn.execute(
                    "INSERT INTO audit_log"
                    " (target_table, target_id, action, summary)"
                    " VALUES ('diary', ?, 'tl_edit', ?)",
                    (date, f"md-reconcile: tl_line={new_tl[:60]!r}"),
                )
                rpt.updated += 1
            else:
                rpt.unchanged += 1

        # ── DELETE: anchors in trail but absent from current block → hidden ──
        if m_trail:
            for sid in trail_sids - present_sids:
                conn.execute(
                    "UPDATE session_digests SET tl_hidden=1 WHERE sid=?", (sid,))
                conn.execute(
                    "INSERT INTO audit_log (target_table, target_id, action, summary)"
                    " VALUES ('session_digests', ?, 'tl_delete', 'user deleted tl line')",
                    (sid,))
                rpt.updated += 1
            for date in trail_dates - present_dates:
                conn.execute(
                    "UPDATE diary SET tl_hidden=1 WHERE date=?", (date,))
                conn.execute(
                    "INSERT INTO audit_log (target_table, target_id, action, summary)"
                    " VALUES ('diary', ?, 'tl_delete', 'user deleted tl line')",
                    (date,))
                rpt.updated += 1
            for eid in trail_evts - present_evts:
                conn.execute(
                    "DELETE FROM events WHERE id=? AND channel='manual'", (eid,))
                try:
                    conn.execute("DELETE FROM events_vec WHERE rowid=?", (eid,))
                    conn.execute("DELETE FROM events_vec_meta WHERE rowid=?", (eid,))
                except sqlite3.OperationalError:
                    pass
                conn.execute(
                    "INSERT INTO audit_log (target_table, target_id, action, summary)"
                    " VALUES ('events', ?, 'tl_manual_delete', 'user deleted manual event')",
                    (str(eid),))
                rpt.updated += 1

        # ── ADD: lines starting with `+ ` → insert manual events ─────────────
        for raw_line in plus_lines:
            m_plus = _TL_PLUS_RE.match(raw_line)
            if not m_plus:
                rpt.conflicts.append(f"tl:+ unparseable: {raw_line[:60]!r}")
                continue
            hhmm_str = m_plus.group("hhmm")
            add_text = (m_plus.group("text") or "").strip()
            if not add_text:
                rpt.conflicts.append(f"tl:+ empty text: {raw_line[:60]!r}")
                continue
            if hhmm_str:
                try:
                    h, mi = int(hhmm_str[:2]), int(hhmm_str[3:5])
                except ValueError:
                    rpt.conflicts.append(f"tl:+ bad time {hhmm_str!r}: {raw_line[:60]!r}")
                    continue
                now_melb = _dt.datetime.now(_TZ_MELB)
                ts_melb = now_melb.replace(hour=h, minute=mi, second=0, microsecond=0)
                ts_utc = ts_melb.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                ts_utc = _now()
            sid_manual = "manual:" + secrets.token_hex(4)
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content, channel)"
                " VALUES (?, ?, 'user', ?, 'manual')",
                (sid_manual, ts_utc, add_text),
            )
            rpt.updated += 1

        # ── EDIT: manual event content ────────────────────────────────────────
        for eid, new_text in evt_edits.items():
            row = conn.execute(
                "SELECT content, channel FROM events WHERE id=?", (eid,)
            ).fetchone()
            if row is None:
                rpt.conflicts.append(f"tl:e:{eid} not in events")
                continue
            if row["channel"] != "manual":
                rpt.conflicts.append(
                    f"tl:e:{eid} not a manual event (channel={row['channel']!r})")
                continue
            if new_text != (row["content"] or ""):
                conn.execute("UPDATE events SET content=? WHERE id=?", (new_text, eid))
                try:
                    conn.execute("DELETE FROM events_vec WHERE rowid=?", (eid,))
                    conn.execute("DELETE FROM events_vec_meta WHERE rowid=?", (eid,))
                except sqlite3.OperationalError:
                    pass
                conn.execute(
                    "INSERT INTO audit_log (target_table, target_id, action, summary)"
                    " VALUES ('events', ?, 'tl_edit', ?)",
                    (str(eid), f"md-reconcile: content={new_text[:60]!r}"))
                rpt.updated += 1
            else:
                rpt.unchanged += 1

    return rpt
