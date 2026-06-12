"""Tests for marrow/subpages.py — config-driven sub-page render.

Contract:
- Each sub-page rendered from one table, same render contract (markers +
  atomic write) differing only by table + view.
- Structured views: row-id anchor `<!-- id:{id} -->` at line end.
- Narrative views (diary): `## YYYY-MM-DD` heading is the row
  boundary; no extra inline anchor.
- Cheatsheet: read-only, always overwrite.
- Anchored md edits flow back to DB via reconcile; free-form hand-edits
  inside the rendered block are silently overwritten on next render.
- New sub-page = new SubPageConfig entry, not a base rewrite (goal 7).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from marrow import storage, subpages


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    with conn:
        conn.execute("INSERT INTO diary(date,content,mood) "
                     "VALUES('2026-05-20','Today was a good day.','calm')")
        conn.execute("INSERT INTO diary(date,content) "
                     "VALUES('2026-04-15','Spring entry.')")
        conn.execute("INSERT INTO milestones(scope,date,title,description,pinned) "
                     "VALUES('us','2026-01-17','First meeting','In the rain',1)")
        conn.execute("INSERT INTO milestones(scope,date,title,pinned) "
                     "VALUES('me','2026-03-01','Head of school award',1)")
        conn.execute("INSERT INTO memes(type,key,value,context,use_count) "
                     "VALUES('cipher','大龙虾','Openclaw','popular AI agent',5)")
        conn.execute("INSERT INTO tasks(category,title,status,due,next_step) "
                     "VALUES('study','Biochem:Unit 3','active','2026-06-01','read ch5')")
        conn.execute("INSERT INTO tasks(category,title,status,next_step) "
                     "VALUES('project','Marrow','active','build subpages')")
        conn.execute("INSERT INTO pit(title,description,status) "
                     "VALUES('old feature','dropped idea','idea')")
    conn.close()
    return p


# ---------------------------------------------------------------------------
# Diary
# ---------------------------------------------------------------------------

def test_render_diary_contains_dates_and_content(db):
    conn = storage.connect(db)
    try:
        block = subpages.render_diary(conn)
    finally:
        conn.close()
    assert "#### 2026-05-20" in block
    assert "Today was a good day." in block
    assert "### April" in block  # month-name heading
    assert "### May" in block
    assert "## 2026" in block    # year heading
    assert "Spring entry." in block
    assert "<!-- marrow:diary:start -->" in block
    assert "<!-- marrow:diary:end -->" in block


def test_render_diary_no_structured_anchor(db):
    conn = storage.connect(db)
    try:
        block = subpages.render_diary(conn)
    finally:
        conn.close()
    # Narrative view: NO row-id anchor, boundary is the date heading only
    assert "<!-- id:" not in block


def test_render_diary_month_grouped(db):
    conn = storage.connect(db)
    try:
        block = subpages.render_diary(conn)
    finally:
        conn.close()
    # Both months appear as H3 month-name headings under a shared year.
    assert "## 2026" in block
    assert "### April" in block
    assert "### May" in block
    # ASC order: oldest first — April appears before May.
    assert block.index("### April") < block.index("### May")


# ---------------------------------------------------------------------------
# Milestone
# ---------------------------------------------------------------------------

def test_render_milestone_sections(db):
    conn = storage.connect(db)
    try:
        block = subpages.render_milestone(conn)
    finally:
        conn.close()
    assert "## Us" in block
    assert "## Me" in block
    assert "First meeting" in block
    assert "Head of school award" in block
    assert "<!-- marrow:milestone:start -->" in block


def test_render_milestone_structured_anchor(db):
    conn = storage.connect(db)
    try:
        block = subpages.render_milestone(conn)
        row = conn.execute("SELECT id FROM milestones LIMIT 1").fetchone()
    finally:
        conn.close()
    assert f"<!-- id:{row['id']} -->" in block


# ---------------------------------------------------------------------------
# Memes
# ---------------------------------------------------------------------------

def test_render_memes_personal_and_public(db):
    conn = storage.connect(db)
    try:
        block = subpages.render_memes(conn)
    finally:
        conn.close()
    assert "## Personal" in block
    assert "## Public" in block
    assert "大龙虾" in block
    assert "<!-- marrow:memes:start -->" in block
    # Stickers section gone — sticker render lives on its own subpage.
    assert "## Stickers" not in block
    assert "## Phrases" not in block


def test_render_memes_structured_anchor(db):
    conn = storage.connect(db)
    try:
        block = subpages.render_memes(conn)
        row = conn.execute("SELECT id FROM memes LIMIT 1").fetchone()
    finally:
        conn.close()
    assert f"<!-- id:{row['id']} -->" in block


# ---------------------------------------------------------------------------
# Study
# ---------------------------------------------------------------------------

def test_study_index_and_unit(db, tmp_path):
    folder = str(tmp_path / "ny")
    state = str(tmp_path / "state")
    conn = storage.connect(db)
    try:
        cfg = subpages.build_study_configs(conn, folder, state)
    finally:
        conn.close()
    assert cfg.key == "study"
    assert len(cfg.subpages) == 1
    unit_cfg = cfg.subpages[0]
    assert "Biochem" in unit_cfg.key
    conn = storage.connect(db)
    try:
        block = cfg.render(conn)
        unit_block = unit_cfg.render(conn)
    finally:
        conn.close()
    assert "Biochem" in block
    assert "read ch5" in unit_block
    assert "<!-- id:" in unit_block  # study is structured


def test_study_write_creates_files(db, tmp_path):
    folder = str(tmp_path / "ny")
    state = str(tmp_path / "state")
    conn = storage.connect(db)
    try:
        cfg = subpages.build_study_configs(conn, folder, state)
        subpages.write_subpage(cfg, conn, db=db)
    finally:
        conn.close()
    index = Path(folder) / "study.md"
    assert index.exists()
    unit = Path(folder) / "study" / "Biochem.md"
    assert unit.exists()


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def test_projects_index_active_and_done(db):
    conn = storage.connect(db)
    try:
        block = subpages.render_projects_index(conn)
    finally:
        conn.close()
    assert "## Active" in block
    assert "Marrow" in block
    assert "## Done" in block
    assert "<!-- marrow:projects:start -->" in block


def test_pit_render(db):
    # render_pit is no longer imported through subpages; call via subpages_render.
    from marrow.subpages_render import render_pit
    conn = storage.connect(db)
    try:
        block = render_pit(conn)
    finally:
        conn.close()
    assert "old feature" in block
    assert "<!-- id:" in block
    assert "<!-- marrow:pit:start -->" in block


def test_projects_build_no_pit_child(db, tmp_path):
    """pit child removed from build_projects_configs — mw export-pit handles it."""
    folder = str(tmp_path / "ny")
    state = str(tmp_path / "state")
    conn = storage.connect(db)
    try:
        cfg = subpages.build_projects_configs(conn, folder, state)
    finally:
        conn.close()
    keys = [c.key for c in cfg.subpages]
    assert "pit" not in keys
    assert any("Marrow" in k for k in keys)


# ---------------------------------------------------------------------------
# Cheatsheet (read-only / disk-rendered)
# ---------------------------------------------------------------------------

def test_cheatsheet_render_no_anchor(db):
    conn = storage.connect(db)
    try:
        block = subpages.render_cheatsheet(conn)
    finally:
        conn.close()
    # Internal H1 stripped — Obsidian shows filename as title. H2+ structure
    # (Directory map, Skills, Hooks, Aliases) is the visible spine.
    assert "## Directory map" in block
    assert "<!-- id:" not in block


def test_cheatsheet_always_overwrites(db, tmp_path):
    folder = str(tmp_path / "ny")
    state = str(tmp_path / "state")
    path = str(Path(folder) / "cheatsheet.md")
    conn = storage.connect(db)
    try:
        cfg = subpages.SubPageConfig(
            key="cheatsheet",
            render=subpages.render_cheatsheet,
            path=path,
            state_dir=state,
            read_only=True,
        )
        subpages.write_subpage(cfg, conn, db=db)
        # Manually edit the file (simulating hand-edit)
        Path(path).write_text(Path(path).read_text() + "\nHAND EDIT")
        subpages.write_subpage(cfg, conn, db=db)
        result = Path(path).read_text()
    finally:
        conn.close()
    # Hand edit is overwritten; no backup or alert for read_only
    assert "HAND EDIT" not in result
    # No backup file created
    assert not list(Path(state).glob("cheatsheet*.bak"))


# ---------------------------------------------------------------------------
# Free-form hand-edit inside the rendered block is silently overwritten
# (anchored edits flow back via reconcile; free-form is not preserved).
# ---------------------------------------------------------------------------

def test_freeform_hand_edit_silently_overwritten(db, tmp_path):
    folder = str(tmp_path / "ny")
    state = str(tmp_path / "state")
    path = str(Path(folder) / "milestone.md")
    conn = storage.connect(db)
    try:
        cfg = subpages.SubPageConfig(
            key="milestone",
            render=subpages.render_milestone,
            path=path,
            state_dir=state,
        )
        subpages.write_subpage(cfg, conn, db=db)
        # Free-form addition inside the marker block (not an anchored row).
        end_marker = "<!-- marrow:milestone:end -->"
        t = Path(path).read_text().replace(
            end_marker, "FREE FORM NOTE\n" + end_marker
        )
        Path(path).write_text(t)
        subpages.write_subpage(cfg, conn, db=db)
        result = Path(path).read_text()
        alerts = [a["message"] for a in
                  __import__("marrow.repo", fromlist=["x"]).open_alerts(conn)]
    finally:
        conn.close()
    # Overwritten silently: free-form line gone, no alert, no .bak.
    assert "FREE FORM NOTE" not in result
    assert not any("hand-edited" in m.lower() for m in alerts)
    assert not list(Path(state).glob("milestone*.bak"))


# ---------------------------------------------------------------------------
# build_all_configs / write_all_subpages
# ---------------------------------------------------------------------------

def test_build_all_configs_returns_expected_keys(db, tmp_path):
    folder = str(tmp_path / "ny")
    state = str(tmp_path / "state")
    conn = storage.connect(db)
    try:
        cfgs = subpages.build_all_configs(
            conn, folder=folder, state_dir=state
        )
    finally:
        conn.close()
    keys = {c.key for c in cfgs}
    assert "diary" in keys
    assert "milestone" in keys
    assert "memes" in keys
    assert "goose" not in keys
    assert "cheatsheet" in keys
    assert "study" in keys
    assert "projects" in keys


def test_write_all_subpages_creates_files(db, tmp_path):
    folder = str(tmp_path / "ny")
    state = str(tmp_path / "state")
    conn = storage.connect(db)
    try:
        subpages.write_all_subpages(
            conn, folder=folder, state_dir=state, db=db
        )
    finally:
        conn.close()
    for name in ("diary.md", "milestone.md", "memes.md",
                  "cheatsheet.md", "study.md", "projects.md",
                  "profile.md", "stickers.md", "wallet.md"):
        assert (Path(folder) / name).exists(), f"Missing {name}"
    # pit.md no longer auto-created; mw export-pit writes it on demand.
    assert not (Path(folder) / "projects" / "pit.md").exists()


# ---------------------------------------------------------------------------
# Config-driven build (DESIGN L43-65)
# ---------------------------------------------------------------------------

def test_build_all_configs_respects_subpages_config(db, tmp_path, monkeypatch):
    folder = str(tmp_path / "ny")
    state = str(tmp_path / "state")

    def fake_load():
        return {"subpages": {"top": ["milestone", "diary"],
                              "bottom": ["projects"], "hidden": []}}

    monkeypatch.setattr(subpages._config, "load", fake_load)
    conn = storage.connect(db)
    try:
        cfgs = subpages.build_all_configs(conn, folder=folder, state_dir=state)
    finally:
        conn.close()
    keys = [c.key for c in cfgs]
    # Order honoured; only listed keys built.
    assert keys == ["milestone", "diary", "projects"]


def test_build_all_configs_warns_on_unknown_key(db, tmp_path, monkeypatch):
    folder = str(tmp_path / "ny")
    state = str(tmp_path / "state")

    def fake_load():
        return {"subpages": {"top": ["milestone", "bogus_key"],
                              "bottom": [], "hidden": []}}

    monkeypatch.setattr(subpages._config, "load", fake_load)
    conn = storage.connect(db)
    try:
        cfgs = subpages.build_all_configs(conn, folder=folder,
                                          state_dir=state, db=db)
        from marrow import repo as _repo
        msgs = [a["message"] for a in _repo.open_alerts(conn)]
    finally:
        conn.close()
    keys = [c.key for c in cfgs]
    assert keys == ["milestone"]
    assert any("bogus_key" in m for m in msgs)


def test_content_list_excludes_hidden(db, tmp_path, monkeypatch):
    folder = str(tmp_path / "ny")

    def fake_load():
        return {"subpages": {"top": ["profile", "milestone"],
                              "bottom": ["cheatsheet"],
                              "hidden": ["milestone"]}}

    monkeypatch.setattr(subpages._config, "load", fake_load)
    out = subpages.content_list(folder=folder)
    top_keys = [Path(p).stem for _, p in out["top"]]
    assert "profile" in top_keys
    assert "milestone" not in top_keys
    assert [Path(p).stem for _, p in out["bottom"]] == ["cheatsheet"]


# ---------------------------------------------------------------------------
# Milestone format (H5 + paragraph, 2026-05-24)
# ---------------------------------------------------------------------------

def test_milestone_h5_paragraph_format(db):
    conn = storage.connect(db)
    try:
        block = subpages.render_milestone(conn)
    finally:
        conn.close()
    # H5 heading carries `[date] subject`; description sits on the next
    # line with the inline-tail anchor.
    assert "##### [2026-01-17] First meeting" in block
    assert "In the rain <!-- id:" in block
    # Me row has no description — anchor lands on its own line.
    assert "##### [2026-03-01] Head of school award" in block
    # Old bullet format is gone.
    assert "- [2026-01-17]" not in block
    assert "Head of school award:" not in block
    # No `Nh ago` timestamp on confirmed rows.
    assert "ago)" not in block
    # No candidate buttons on confirmed rows.
    assert "✅" not in block and "❌" not in block and "✏️" not in block


def test_milestone_me_age_row_uses_single_bracket(tmp_path):
    """Historical Me row (year-only date + `Age ` title) renders as
    `##### [<title>]`; dated Me rows keep the `[YYYY-MM-DD] subject` form.
    """
    from marrow import storage as _storage
    p = str(tmp_path / "age.db")
    conn = _storage.init_db(p)
    with conn:
        conn.execute("INSERT INTO milestones(scope,date,title,description,pinned)"
                     " VALUES('me','1995','Age 0-10 | Shanghai','flat',1)")
        conn.execute("INSERT INTO milestones(scope,date,title,description,pinned)"
                     " VALUES('me','2026-05-15','Marrow rebuild','starting',1)")
    try:
        block = subpages.render_milestone(conn)
    finally:
        conn.close()
    # Age row: title fills the bracket; raw year not surfaced.
    assert "##### [Age 0-10 | Shanghai]" in block
    assert "##### [1995]" not in block
    # Dated Me row keeps standard form.
    assert "##### [2026-05-15] Marrow rebuild" in block
