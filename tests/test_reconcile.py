"""Tests for marrow/reconcile.py — milestone md->DB reconcile."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from marrow import reconcile, storage, subpages


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    with conn:
        # pinned=1 = confirmed/subpage-eligible. Reconcile + render both
        # scope themselves to pinned=1 rows now; pinned=0 = candidate.
        conn.execute(
            "INSERT INTO milestones(scope,date,title,description,pinned) "
            "VALUES('us','2026-01-17','First meeting','In the rain',1)"
        )
        conn.execute(
            "INSERT INTO milestones(scope,date,title,pinned) "
            "VALUES('me','2026-03-01','Head of school award',1)"
        )
    conn.close()
    return p


def _render_to_md(db_path: str, folder: Path, state: Path) -> Path:
    conn = storage.connect(db_path)
    try:
        cfg = subpages.SubPageConfig(
            key="milestone",
            render=subpages.render_milestone,
            path=str(folder / "milestone.md"),
            state_dir=str(state),
        )
        subpages.write_subpage(cfg, conn, db=db_path)
    finally:
        conn.close()
    return folder / "milestone.md"


# ── 1. fresh DB -> render -> reconcile no-op ────────────────────────────────

def test_reconcile_noop_on_freshly_rendered(db, tmp_path):
    folder = tmp_path / "ny"
    state = tmp_path / "state"
    md = _render_to_md(db, folder, state)
    conn = storage.connect(db)
    try:
        rpt = reconcile.reconcile_milestones(conn, md)
    finally:
        conn.close()
    assert rpt.inserted == 0
    assert rpt.updated == 0
    assert rpt.deleted == 0
    assert rpt.unchanged == 2


# ── 2. md with new unanchored row -> insert ─────────────────────────────────

def test_reconcile_inserts_unanchored(db, tmp_path):
    folder = tmp_path / "ny"
    state = tmp_path / "state"
    md = _render_to_md(db, folder, state)
    text = md.read_text(encoding="utf-8")
    # Inject a new H5 block under ## Me without an id anchor.
    # Format = H5 paragraph: `##### [date] subject\ndescription`.
    new_block = (
        "##### [2026-05-22] Round 2 milestone\n"
        "added by Lumi\n\n"
    )
    text = text.replace("## Me\n\n", "## Me\n\n" + new_block)
    md.write_text(text, encoding="utf-8")

    conn = storage.connect(db)
    try:
        rpt = reconcile.reconcile_milestones(conn, md)
        rows = conn.execute(
            "SELECT id, title FROM milestones WHERE title='Round 2 milestone'"
        ).fetchall()
    finally:
        conn.close()
    assert rpt.inserted == 1
    assert len(rows) == 1

    # After re-render the new row should have an anchor.
    md2 = _render_to_md(db, folder, state)
    txt2 = md2.read_text()
    rid = rows[0]["id"]
    assert "Round 2 milestone" in txt2
    assert f"<!-- id:{rid} -->" in txt2


# ── 3. md with edited title on anchored row -> update; updated_at advances ─

def test_reconcile_updates_and_advances_updated_at(db, tmp_path):
    folder = tmp_path / "ny"
    state = tmp_path / "state"
    md = _render_to_md(db, folder, state)
    # capture original updated_at
    conn = storage.connect(db)
    try:
        old = conn.execute(
            "SELECT id, updated_at FROM milestones WHERE title='First meeting'"
        ).fetchone()
    finally:
        conn.close()
    assert old is not None
    old_ts = old["updated_at"]

    # Edit the title.
    text = md.read_text().replace("First meeting", "First meeting EDITED")
    md.write_text(text)

    # Sleep just enough so timestamp differs (UTC second granularity).
    time.sleep(1.1)
    conn = storage.connect(db)
    try:
        rpt = reconcile.reconcile_milestones(conn, md)
        row = conn.execute(
            "SELECT title, updated_at, created_at FROM milestones WHERE id=?",
            (old["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert rpt.updated == 1
    assert row["title"] == "First meeting EDITED"
    assert row["updated_at"] > old_ts
    assert row["updated_at"] >= row["created_at"]


# ── 4. md with anchored row removed -> delete (no backup) ──────────────────

def test_reconcile_deletes_when_row_removed(db, tmp_path):
    folder = tmp_path / "ny"
    state = tmp_path / "state"
    md = _render_to_md(db, folder, state)
    text = md.read_text()
    # Strip the full "First meeting" block.
    new_lines = []
    skip_next = False
    for ln in text.splitlines():
        if skip_next:
            skip_next = False
            continue
        if "First meeting" in ln:
            skip_next = True
            continue
        new_lines.append(ln)
    time.sleep(1.1)
    md.write_text("\n".join(new_lines) + "\n")

    conn = storage.connect(db)
    try:
        rpt = reconcile.reconcile_milestones(conn, md)
        remaining = conn.execute(
            "SELECT COUNT(*) c FROM milestones"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert rpt.deleted == 1
    assert remaining == 1
    # No backup file written — DESIGN L62 forbids backups; alert path
    # via rpt.conflicts covers reconcile failure.
    assert not list(Path(md.parent).glob("milestone.*.bak.md"))


# ── 5. unchanged md -> no audit_log ────────────────────────────────────────

# ── candidate buttons (✅ ❌ ✏️) ────────────────────────────────────────────

def test_candidate_pin_promotes(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    with conn:
        cur = conn.execute(
            "INSERT INTO milestones(scope,date,title,pinned) "
            "VALUES('us','2026-05-22','Test cand',0)"
        )
        rid = cur.lastrowid
    dash = tmp_path / "dashboard.md"
    dash.write_text(
        "<!-- marrow:top:start -->\n"
        "## Milestone candidate [1]\n"
        f"- [2026-05-22] Test cand (1h ago)  ✅  <!-- id:{rid} -->\n"
        "## Affect\n"
        "<!-- marrow:top:end -->\n"
    )
    rpt = reconcile.reconcile_milestone_candidates(conn, dash)
    row = conn.execute(
        "SELECT pinned FROM milestones WHERE id=?", (rid,)
    ).fetchone()
    conn.close()
    assert rpt.updated == 1
    assert row["pinned"] == 1


def test_candidate_drop_deletes_and_tombstones(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    with conn:
        cur = conn.execute(
            "INSERT INTO milestones(scope,date,title,pinned,source_hash) "
            "VALUES('us','2026-05-22','Doomed','sh-abc',0)"
        )
        rid = cur.lastrowid
    dash = tmp_path / "dashboard.md"
    dash.write_text(
        "<!-- marrow:top:start -->\n"
        "## Milestone candidate [1]\n"
        f"- [2026-05-22] Doomed (1h ago)  ❌  <!-- id:{rid} -->\n"
        "## Affect\n"
        "<!-- marrow:top:end -->\n"
    )
    rpt = reconcile.reconcile_milestone_candidates(conn, dash)
    row = conn.execute(
        "SELECT id FROM milestones WHERE id=?", (rid,)
    ).fetchone()
    audit = conn.execute(
        "SELECT action, summary FROM audit_log WHERE target_table='milestones'"
        " AND target_id=? ORDER BY id DESC LIMIT 1", (str(rid),)
    ).fetchone()
    conn.close()
    assert rpt.deleted == 1
    assert row is None
    assert audit["action"] == "tombstone"


def test_candidate_row_deleted_drops_and_tombstones(tmp_path):
    """Lumi rm's the bullet line in Obsidian → drop + tombstone, even with
    no emoji vote. Trail marker `<!-- cand:milestone:ids=[N] -->` records
    that the row was rendered last round, so absence == intent to drop.
    """
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    with conn:
        cur = conn.execute(
            "INSERT INTO milestones(scope,date,title,pinned,source_hash) "
            "VALUES('us','2026-05-22','Bye',0,'sh-x')"
        )
        rid = cur.lastrowid
    dash = tmp_path / "dashboard.md"
    # No `- [...] <!-- id:N -->` row in the candidate block — only the trail
    # marker survives, meaning Lumi deleted the bullet line.
    dash.write_text(
        "<!-- marrow:top:start -->\n"
        "## Milestone candidate [0]\n"
        f"<!-- cand:milestone:ids=[{rid}] -->\n"
        "## Affect\n"
        "<!-- marrow:top:end -->\n"
    )
    rpt = reconcile.reconcile_milestone_candidates(conn, dash)
    row = conn.execute(
        "SELECT id FROM milestones WHERE id=?", (rid,)
    ).fetchone()
    audit = conn.execute(
        "SELECT action, summary FROM audit_log WHERE target_table='milestones'"
        " AND target_id=? ORDER BY id DESC LIMIT 1", (str(rid),)
    ).fetchone()
    conn.close()
    assert rpt.deleted == 1
    assert row is None
    assert audit["action"] == "tombstone"
    # Summary carries the natural-key hash for downstream LIKE-match.
    import hashlib
    nh = hashlib.sha256(b"milestones|us|2026-05-22|Bye").hexdigest()
    assert nh in audit["summary"]


def test_candidate_drop_blocks_revive_via_write_milestone_cand(tmp_path):
    """After drop, write_milestone_cand must not re-insert the same row."""
    from marrow import candidates
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    with conn:
        cur = conn.execute(
            "INSERT INTO milestones(scope,date,title,pinned,source_hash) "
            "VALUES('me','2026-05-22','Killed',0,'sh-x')"
        )
        rid = cur.lastrowid
    dash = tmp_path / "dashboard.md"
    dash.write_text(
        "<!-- marrow:top:start -->\n"
        "## Milestone candidate [0]\n"
        f"<!-- cand:milestone:ids=[{rid}] -->\n"
        "## Affect\n"
        "<!-- marrow:top:end -->\n"
    )
    reconcile.reconcile_milestone_candidates(conn, dash)
    raw = (
        "===MILESTONE_CAND===\n"
        '{"scope":"me","date":"2026-05-22","title":"Killed",'
        '"description":"x","conf":0.95}\n'
        "===END===\n"
    )
    n = candidates.write_milestone_cand(conn, raw, "2026-05-22")
    cnt = conn.execute(
        "SELECT COUNT(*) FROM milestones WHERE title='Killed'"
    ).fetchone()[0]
    conn.close()
    assert n == 0
    assert cnt == 0


def test_candidate_no_vote_is_inert(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    with conn:
        cur = conn.execute(
            "INSERT INTO milestones(scope,date,title,pinned) "
            "VALUES('us','2026-05-22','Unchosen',0)"
        )
        rid = cur.lastrowid
    dash = tmp_path / "dashboard.md"
    # All three chars present = no decision yet.
    dash.write_text(
        "<!-- marrow:top:start -->\n"
        "## Milestone candidate [1]\n"
        f"- [2026-05-22] Unchosen (1h ago)  ✅ ❌ ✏️"
        f"  <!-- id:{rid} -->\n"
        "## Affect\n"
        "<!-- marrow:top:end -->\n"
    )
    rpt = reconcile.reconcile_milestone_candidates(conn, dash)
    row = conn.execute(
        "SELECT pinned FROM milestones WHERE id=?", (rid,)
    ).fetchone()
    conn.close()
    assert rpt.updated == 0
    assert rpt.deleted == 0
    assert row["pinned"] == 0


def test_reconcile_edit_description_keeps_id(db, tmp_path):
    """Editing only the description paragraph (anchor untouched) UPDATEs."""
    folder = tmp_path / "ny"
    state = tmp_path / "state"
    md = _render_to_md(db, folder, state)
    text = md.read_text(encoding="utf-8")
    # Replace the description sentence under the Us H5 block.
    text = text.replace("In the rain", "In the rain, soaked but smiling")
    md.write_text(text)
    conn = storage.connect(db)
    try:
        rpt = reconcile.reconcile_milestones(conn, md)
        row = conn.execute(
            "SELECT description FROM milestones WHERE title='First meeting'"
        ).fetchone()
    finally:
        conn.close()
    assert rpt.updated == 1
    assert row["description"] == "In the rain, soaked but smiling"


def test_reconcile_deletes_h5_block(db, tmp_path):
    """Removing the whole H5 block (heading + paragraph + id) DELETEs."""
    folder = tmp_path / "ny"
    state = tmp_path / "state"
    md = _render_to_md(db, folder, state)
    text = md.read_text()
    # Strip every line belonging to the First meeting H5 block.
    lines = text.splitlines()
    out: list[str] = []
    drop = False
    for ln in lines:
        if ln.startswith("##### [") and "First meeting" in ln:
            drop = True
            continue
        if drop and (ln.startswith("##### ") or ln.startswith("## ")
                     or ln.startswith("<!-- marrow:")):
            drop = False
        if drop:
            continue
        out.append(ln)
    time.sleep(1.1)
    md.write_text("\n".join(out) + "\n")
    conn = storage.connect(db)
    try:
        rpt = reconcile.reconcile_milestones(conn, md)
        row = conn.execute(
            "SELECT id FROM milestones WHERE title='First meeting'"
        ).fetchone()
    finally:
        conn.close()
    assert rpt.deleted == 1
    assert row is None


def test_reconcile_ignores_legacy_bullets(db, tmp_path):
    """A stale `- [date] ...` bullet must NOT be absorbed into the
    preceding H5 block's description. Legacy bullets are skipped — the
    reconcile leaves DB rows untouched until Lumi rewrites the block in
    H5 form (or a fresh render rewrites them).
    """
    folder = tmp_path / "ny"
    state = tmp_path / "state"
    md = _render_to_md(db, folder, state)
    # Splice a stale legacy bullet between the H5 block and the next H5.
    text = md.read_text()
    legacy = (
        "- [2026-04-01] legacy bullet that should NOT pollute description "
        "<!-- id:999 -->\n"
    )
    text = text.replace("## Me\n\n", "## Me\n\n" + legacy)
    md.write_text(text)
    conn = storage.connect(db)
    try:
        rpt = reconcile.reconcile_milestones(conn, md)
        # The H5 'Head of school award' row's description must not contain
        # the legacy bullet text.
        row = conn.execute(
            "SELECT description FROM milestones WHERE title='Head of school award'"
        ).fetchone()
    finally:
        conn.close()
    assert row["description"] is None or "legacy bullet" not in (row["description"] or "")
    # The legacy bullet creates no new row.
    conn = storage.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM milestones WHERE title LIKE 'legacy%'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 0


def test_reconcile_age_row_single_bracket(db, tmp_path):
    """Historical Me row (year-only date + `Age ...` title) renders as
    single-bracket `##### [Age 0-10 | Shanghai]`; the raw year is not
    surfaced in md. Round-trip is safe: anchor pulls date from DB.
    """
    folder = tmp_path / "ny"
    state = tmp_path / "state"
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO milestones(scope,date,title,description,pinned)"
            " VALUES('me','1995','Age 0-10 | Shanghai','small apartment',1)"
        )
    conn.close()
    md = _render_to_md(db, folder, state)
    text = md.read_text()
    # New format: title fills the bracket, year is DB-only.
    assert "##### [Age 0-10 | Shanghai]" in text
    assert "##### [1995]" not in text
    # Re-parse: anchor carries id, date inherited from DB → no diff.
    conn = storage.connect(db)
    try:
        rpt = reconcile.reconcile_milestones(conn, md)
    finally:
        conn.close()
    assert not rpt.any_change()


def test_reconcile_unanchored_writes_id_back_and_dedups(db, tmp_path):
    """BUG-1 regression: unanchored md row → INSERT once; the user's heading
    line gets ` <!-- id:N -->` appended in-place so the next reconcile tick
    treats it as anchored (no rapid-fire duplicate inserts).
    """
    folder = tmp_path / "ny"
    state = tmp_path / "state"
    md = _render_to_md(db, folder, state)
    text = md.read_text(encoding="utf-8")
    new_block = (
        "##### [2026-05-22] Round 2 milestone\n"
        "added by Lumi\n\n"
    )
    text = text.replace("## Me\n\n", "## Me\n\n" + new_block)
    md.write_text(text, encoding="utf-8")

    conn = storage.connect(db)
    try:
        rpt1 = reconcile.reconcile_milestones(conn, md)
        rpt2 = reconcile.reconcile_milestones(conn, md)
        rows = conn.execute(
            "SELECT id, title FROM milestones WHERE title='Round 2 milestone'"
        ).fetchall()
    finally:
        conn.close()
    # First pass inserts exactly once; second pass is a no-op (anchor written
    # back means the parser now sees it as an existing row).
    assert rpt1.inserted == 1
    assert rpt2.inserted == 0
    assert len(rows) == 1
    rid = rows[0]["id"]
    # The user's heading line itself now carries the anchor — not appended as
    # a separate block elsewhere in the file.
    txt = md.read_text(encoding="utf-8")
    assert f"##### [2026-05-22] Round 2 milestone <!-- id:{rid} -->" in txt


def test_reconcile_unanchored_dedup_when_db_row_already_exists(db, tmp_path):
    """Safety-net: if an exact (scope,date,title) row already exists in DB
    (e.g. a prior tick inserted but anchor-write failed), reconcile must NOT
    INSERT again — it should reuse the existing id and rewrite the anchor.
    """
    folder = tmp_path / "ny"
    state = tmp_path / "state"
    md = _render_to_md(db, folder, state)
    # Pre-seed a DB row matching the (scope,date,title) we'll add to md.
    conn = storage.connect(db)
    with conn:
        cur = conn.execute(
            "INSERT INTO milestones(scope,date,title,pinned) "
            "VALUES('me','2026-05-23','Dup probe',1)"
        )
        seeded_id = cur.lastrowid
    conn.close()
    # Re-render so the seeded row lands in md with its anchor, then strip
    # the anchor to simulate "anchor-write failed last tick".
    md = _render_to_md(db, folder, state)
    text = md.read_text(encoding="utf-8")
    text = text.replace(f" <!-- id:{seeded_id} -->", "")

    md.write_text(text, encoding="utf-8")

    conn = storage.connect(db)
    try:
        rpt = reconcile.reconcile_milestones(conn, md)
        rows = conn.execute(
            "SELECT id FROM milestones WHERE title='Dup probe'"
        ).fetchall()
    finally:
        conn.close()
    assert rpt.inserted == 0
    assert len(rows) == 1
    assert rows[0]["id"] == seeded_id
    # Anchor restored on the heading line.
    txt = md.read_text(encoding="utf-8")
    assert f"<!-- id:{seeded_id} -->" in txt


def test_reconcile_unchanged_is_inert(db, tmp_path):
    folder = tmp_path / "ny"
    state = tmp_path / "state"
    md = _render_to_md(db, folder, state)
    # Audit rows accumulate from the initial render? No, render doesn't audit.
    conn = storage.connect(db)
    try:
        before = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE target_table='milestones'"
        ).fetchone()["c"]
        rpt = reconcile.reconcile_milestones(conn, md)
        after = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE target_table='milestones'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert not rpt.any_change()
    assert after == before
    # No backup files — unchanged md is fully inert.
    assert not list(Path(md.parent).glob("milestone.*.bak.md"))


# ── memes reconcile ────────────────────────────────────────────────────────────


def _seed_memes(conn) -> list[int]:
    """Insert 3 memes rows, return their ids."""
    ids = []
    for i, (t, k) in enumerate([
        ("paw", "大龙虾"), ("fact", "Plan tier"), ("meme", "rickroll")
    ]):
        cur = conn.execute(
            "INSERT INTO memes(type,key,value) VALUES(?,?,?)", (t, k, f"v{i}")
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def _write_memes_md(md_path: Path, ids: list[int]) -> None:
    """Write a minimal memes.md with anchored rows."""
    lines = ["# Memes\n"]
    for rid in ids:
        lines.append(f"- row {rid} <!-- id:{rid} -->")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_reconcile_memes_noop_when_all_present(tmp_path):
    p = str(tmp_path / "m.db")
    conn = storage.init_db(p)
    ids = _seed_memes(conn)
    md = tmp_path / "memes.md"
    _write_memes_md(md, ids)
    rpt = reconcile.reconcile_memes(conn, md)
    assert rpt.deleted == 0
    remaining = conn.execute("SELECT COUNT(*) c FROM memes").fetchone()["c"]
    assert remaining == 3
    conn.close()


def test_reconcile_memes_deletes_missing_anchor(tmp_path):
    """Write 3 rows, remove one anchor from md — reconcile DELETEs that row."""
    p = str(tmp_path / "m.db")
    conn = storage.init_db(p)
    ids = _seed_memes(conn)
    md = tmp_path / "memes.md"
    # Only include the first two ids in md — third row's anchor is gone.
    _write_memes_md(md, ids[:2])
    rpt = reconcile.reconcile_memes(conn, md)
    assert rpt.deleted == 1
    remaining_ids = {
        r[0] for r in conn.execute("SELECT id FROM memes").fetchall()
    }
    assert ids[2] not in remaining_ids
    assert ids[0] in remaining_ids and ids[1] in remaining_ids
    # Audit log entry written.
    audit = conn.execute(
        "SELECT action FROM audit_log WHERE target_table='memes'"
        " AND target_id=? LIMIT 1", (str(ids[2]),)
    ).fetchone()
    assert audit is not None and audit["action"] == "delete"
    conn.close()


def test_reconcile_memes_empty_md_guard(tmp_path):
    """Empty md must NOT wipe the table (first-render / file-missing guard)."""
    p = str(tmp_path / "m.db")
    conn = storage.init_db(p)
    ids = _seed_memes(conn)
    md = tmp_path / "memes.md"
    md.write_text("", encoding="utf-8")
    rpt = reconcile.reconcile_memes(conn, md)
    assert rpt.deleted == 0
    remaining = conn.execute("SELECT COUNT(*) c FROM memes").fetchone()["c"]
    assert remaining == 3
    conn.close()


def test_reconcile_memes_missing_file_noop(tmp_path):
    """Missing md file is a no-op."""
    p = str(tmp_path / "m.db")
    conn = storage.init_db(p)
    _seed_memes(conn)
    md = tmp_path / "memes.md"  # does not exist
    rpt = reconcile.reconcile_memes(conn, md)
    assert rpt.deleted == 0
    conn.close()


# ── Task 1: tag parsing — no-space CJK ────────────────────────────────────────

def test_parse_unanchored_task_body_cjk_no_space():
    """[appointment]看牙医 → category Appointment, clean title, no prefix."""
    result = reconcile._parse_unanchored_task_body("[appointment]看牙医")
    assert result is not None
    assert result["category"] == "Appointment"
    assert result["title"] == "看牙医"
    assert result["due"] is None


def test_parse_unanchored_task_body_cjk_with_due():
    """[appointment]看牙医 [06-20] → Appointment + title + due peeled."""
    result = reconcile._parse_unanchored_task_body("[appointment]看牙医 [06-20]")
    assert result is not None
    assert result["category"] == "Appointment"
    assert result["title"] == "看牙医"
    assert result["due"] == "06-20"


def test_parse_unanchored_task_body_spaced_unchanged():
    """[Study] existing spaced form still works after the \\s* fix."""
    result = reconcile._parse_unanchored_task_body("[Study] review notes")
    assert result is not None
    assert result["category"] == "Study"
    assert result["title"] == "review notes"


def test_parse_task_row_body_cjk_no_space():
    """_parse_task_row_body strips [appointment] prefix with no space before CJK."""
    result = reconcile._parse_task_row_body("[appointment]看牙医", None, None)
    assert result is not None
    title, ns = result
    assert title == "看牙医"
    assert ns is None


# ── Task 2a: bare-text insert in ## Us / ## Me ────────────────────────────────

_MELB_DATE = reconcile._today_melb()

_BARE_US_MD = f"""\
<!-- marrow:milestone:start -->

## Us

这是一个新里程碑

## Me

<!-- marrow:milestone:end -->
"""

_BARE_ME_MD = f"""\
<!-- marrow:milestone:start -->

## Us

## Me

新的me里程碑

<!-- marrow:milestone:end -->
"""


def _fresh_conn(tmp_path, name="t.db"):
    p = str(tmp_path / name)
    conn = storage.init_db(p)
    return conn, p


def test_bare_text_us_inserts(tmp_path):
    """Bare text line in ## Us → INSERT with today's date, pinned=1."""
    conn, db = _fresh_conn(tmp_path)
    md = tmp_path / "milestone.md"
    md.write_text(_BARE_US_MD, encoding="utf-8")
    rpt = reconcile.reconcile_milestones(conn, md)
    assert rpt.inserted == 1
    row = conn.execute(
        "SELECT scope, date, title, pinned FROM milestones WHERE title=?",
        ("这是一个新里程碑",),
    ).fetchone()
    assert row is not None
    assert row["scope"] == "us"
    assert row["date"] == _MELB_DATE
    assert row["pinned"] == 1
    conn.close()


def test_bare_text_me_inserts(tmp_path):
    """Bare text line in ## Me → INSERT with today's date, scope=me."""
    conn, db = _fresh_conn(tmp_path)
    md = tmp_path / "milestone.md"
    md.write_text(_BARE_ME_MD, encoding="utf-8")
    rpt = reconcile.reconcile_milestones(conn, md)
    assert rpt.inserted == 1
    row = conn.execute(
        "SELECT scope FROM milestones WHERE title=?", ("新的me里程碑",)
    ).fetchone()
    assert row is not None
    assert row["scope"] == "me"
    conn.close()


def test_bare_text_writeback_canonical_line(tmp_path):
    """After bare-text INSERT, the md line is rewritten to canonical H5 form."""
    conn, db = _fresh_conn(tmp_path)
    md = tmp_path / "milestone.md"
    md.write_text(_BARE_US_MD, encoding="utf-8")
    reconcile.reconcile_milestones(conn, md)
    txt = md.read_text(encoding="utf-8")
    rid = conn.execute(
        "SELECT id FROM milestones WHERE title=?", ("这是一个新里程碑",)
    ).fetchone()["id"]
    assert f"##### [{_MELB_DATE}] 这是一个新里程碑 <!-- id:{rid} -->" in txt
    conn.close()


def test_bare_text_second_pass_noop(tmp_path):
    """Second reconcile after bare-text write-back is a no-op (idempotent)."""
    conn, db = _fresh_conn(tmp_path)
    md = tmp_path / "milestone.md"
    md.write_text(_BARE_US_MD, encoding="utf-8")
    reconcile.reconcile_milestones(conn, md)
    rpt2 = reconcile.reconcile_milestones(conn, md)
    assert rpt2.inserted == 0
    assert rpt2.updated == 0
    count = conn.execute("SELECT COUNT(*) c FROM milestones").fetchone()["c"]
    assert count == 1
    conn.close()


def test_bare_text_after_h5_merges_into_description(tmp_path):
    """Regression: bare line AFTER an H5 heading (cur is not None) still
    becomes part of that block's description, not a new milestone."""
    md_text = """\
<!-- marrow:milestone:start -->

## Us

##### [2026-06-01] Our trip
This is the description line

<!-- marrow:milestone:end -->
"""
    conn, db = _fresh_conn(tmp_path)
    md = tmp_path / "milestone.md"
    md.write_text(md_text, encoding="utf-8")
    rpt = reconcile.reconcile_milestones(conn, md)
    # Should be 1 insert (the H5 row), NOT 2 (description not treated as milestone)
    assert rpt.inserted == 1
    row = conn.execute(
        "SELECT description FROM milestones WHERE title='Our trip'"
    ).fetchone()
    assert row is not None
    assert row["description"] == "This is the description line"
    conn.close()


# ── Task 2b: unanchored single-bracket Me row → insert with today ─────────────

_SINGLE_BRACKET_MD = """\
<!-- marrow:milestone:start -->

## Us

## Me

##### [My childhood label]

<!-- marrow:milestone:end -->
"""


def test_single_bracket_unanchored_me_inserts(tmp_path):
    """Unanchored ##### [label] under ## Me → INSERT with today's date."""
    conn, db = _fresh_conn(tmp_path)
    md = tmp_path / "milestone.md"
    md.write_text(_SINGLE_BRACKET_MD, encoding="utf-8")
    rpt = reconcile.reconcile_milestones(conn, md)
    assert rpt.inserted == 1
    row = conn.execute(
        "SELECT scope, date, title FROM milestones WHERE title='My childhood label'"
    ).fetchone()
    assert row is not None
    assert row["scope"] == "me"
    assert row["date"] == _MELB_DATE
    conn.close()


def test_single_bracket_anchored_date_inherited(tmp_path):
    """Anchored single-bracket Me row (with id) keeps DB date unchanged."""
    conn, db = _fresh_conn(tmp_path)
    with conn:
        cur = conn.execute(
            "INSERT INTO milestones(scope,date,title,pinned) "
            "VALUES('me','1995','Age 0-10 | Shanghai',1)"
        )
        rid = cur.lastrowid
    md_text = f"""\
<!-- marrow:milestone:start -->

## Us

## Me

##### [Age 0-10 | Shanghai] <!-- id:{rid} -->

<!-- marrow:milestone:end -->
"""
    md = tmp_path / "milestone.md"
    md.write_text(md_text, encoding="utf-8")
    rpt = reconcile.reconcile_milestones(conn, md)
    assert rpt.inserted == 0
    row = conn.execute(
        "SELECT date FROM milestones WHERE id=?", (rid,)
    ).fetchone()
    assert row["date"] == "1995"
    conn.close()


# ── Task 3: reconcile conflicts surfaced as alerts ────────────────────────────

def test_conflict_produces_alert(tmp_path):
    """Anchored id not in DB → one alert row created."""
    conn, db = _fresh_conn(tmp_path)
    md = tmp_path / "milestone.md"
    md.write_text(
        "<!-- marrow:milestone:start -->\n"
        "## Us\n\n"
        "##### [2026-01-01] Ghost row <!-- id:9999 -->\n\n"
        "<!-- marrow:milestone:end -->\n",
        encoding="utf-8",
    )
    rpt = reconcile.reconcile_milestones(conn, md)
    assert rpt.conflicts
    # emit_conflict_alerts needs a db path — call directly
    reconcile.emit_conflict_alerts(rpt, "test:milestone", db=db)
    rows = conn.execute(
        "SELECT type, fingerprint, hit_count FROM alerts WHERE resolved=0"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["type"] == "reconcile_conflict"
    assert "9999" in rows[0]["fingerprint"]
    conn.close()


def test_conflict_repeat_bumps_hit_count(tmp_path):
    """Repeated reconcile + emit bumps hit_count, not row count."""
    conn, db = _fresh_conn(tmp_path)
    md = tmp_path / "milestone.md"
    md.write_text(
        "<!-- marrow:milestone:start -->\n"
        "## Us\n\n"
        "##### [2026-01-01] Ghost row <!-- id:9999 -->\n\n"
        "<!-- marrow:milestone:end -->\n",
        encoding="utf-8",
    )
    for _ in range(3):
        rpt = reconcile.reconcile_milestones(conn, md)
        reconcile.emit_conflict_alerts(rpt, "test:milestone", db=db)
    rows = conn.execute(
        "SELECT hit_count FROM alerts WHERE type='reconcile_conflict' AND resolved=0"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["hit_count"] == 3
    conn.close()
