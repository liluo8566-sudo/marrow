"""Per-subpage InserterSpec smoke tests (Plan M Phase B).

Verifies each builder returns a usable spec and that bootstrap from a
populated DB produces the expected blocks. The fine-grained inserter
behaviour (user-edit-wins / tombstone) is covered by test_inserter.py;
here we focus on the wiring per subpage.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from marrow import storage, subpage_specs
from marrow.inserter import write_subpage_inserter
from marrow.md_index import MdIndex


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    with conn:
        conn.execute("INSERT INTO diary(date,content,mood)"
                     " VALUES('2026-05-20','Today was a good day.','calm')")
        conn.execute("INSERT INTO milestones(scope,date,title,description,pinned)"
                     " VALUES('us','2026-01-17','First meeting','In the rain',1)")
        conn.execute("INSERT INTO milestones(scope,date,title,pinned)"
                     " VALUES('me','2026-03-01','Head of school award',1)")
        conn.execute("INSERT INTO memes(type,key,value,use_count)"
                     " VALUES('paw','大龙虾','Openclaw',5)")
        conn.execute("INSERT INTO memes(type,key,value)"
                     " VALUES('meme','rickroll','GG')")
        conn.execute("INSERT INTO tasks(category,title,status,next_step)"
                     " VALUES('project','Marrow','active','build subpages')")
        conn.execute("INSERT INTO tasks(category,title,status)"
                     " VALUES('study','Biochem:Unit 3','active')")
        conn.execute("INSERT INTO tasks(category,title,status)"
                     " VALUES('study','Biochem:Unit 4','active')")
        conn.execute("INSERT INTO tasks(category,title,status)"
                     " VALUES('study','Physiology:Week 1','active')")
    conn.close()
    return p


def _run(spec, db_path):
    conn = storage.connect(db_path)
    try:
        store = MdIndex(conn)
        return write_subpage_inserter(spec, conn, store)
    finally:
        conn.close()


# ── milestone ──────────────────────────────────────────────────────────────


def test_milestone_bootstrap_emits_us_and_me_sections(db, tmp_path):
    spec = subpage_specs.build_milestone_spec(str(tmp_path / "ny"))
    counts = _run(spec, db)
    text = Path(spec.path).read_text()
    assert "## Us" in text
    assert "## Me" in text
    assert "##### [2026-01-17] First meeting" in text
    assert "##### [2026-03-01] Head of school award" in text
    assert "<!-- id:1 -->" in text and "<!-- id:2 -->" in text
    assert counts["bootstrapped"] == 2


# ── diary ──────────────────────────────────────────────────────────────────


def test_diary_bootstrap_uses_date_block_ids(db, tmp_path):
    spec = subpage_specs.build_diary_spec(str(tmp_path / "ny"))
    counts = _run(spec, db)
    text = Path(spec.path).read_text()
    assert "## 2026" in text
    assert "#### 2026-05-20" in text
    assert "Today was a good day." in text
    assert "<!-- id:2026-05-20 -->" in text
    assert counts["bootstrapped"] == 1


# ── memes ──────────────────────────────────────────────────────────────────


def test_memes_bootstrap_personal_public_sections(db, tmp_path):
    spec = subpage_specs.build_memes_spec(str(tmp_path / "ny"))
    counts = _run(spec, db)
    text = Path(spec.path).read_text()
    # Two top-level sections: Personal (fact/paw) and Public (meme/news/...).
    assert "## Personal" in text
    assert "## Public" in text
    assert "大龙虾" in text
    assert "rickroll" in text
    assert counts["bootstrapped"] == 2
    # Personal precedes Public.
    assert text.index("## Personal") < text.index("## Public")
    # No type-name subheaders (fact/paw act as dividers only).
    assert "## paw" not in text
    assert "## meme" not in text
    assert "### paw" not in text


def test_memes_bootstrap_divider_between_types(db, tmp_path):
    """fact→paw transition inside Personal emits a `---` divider; the
    section's first type does NOT render a header."""
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db)
    conn.row_factory = _sqlite3.Row
    with conn:
        conn.execute(
            "INSERT INTO memes (type, key, value, use_count, last_seen,"
            " pinned, source_hash) VALUES ('fact', 'Plan', '$100/mo', 1,"
            " '2026-06-04T00:00:00Z', 1, 't')"
        )
    spec = subpage_specs.build_memes_spec(str(tmp_path / "ny"))
    _run(spec, db)
    text = Path(spec.path).read_text()
    # fact row precedes paw row, separated by `---`.
    personal_block = text[text.index("## Personal"):text.index("## Public")]
    fact_idx = personal_block.index("Plan")
    div_idx = personal_block.index("---")
    paw_idx = personal_block.index("大龙虾")
    assert fact_idx < div_idx < paw_idx
    # No `---` before the first row of Personal (only at fact→paw transition).
    assert personal_block.count("---") == 1


# ── projects index ─────────────────────────────────────────────────────────


def test_projects_index_bootstrap_active_section(db, tmp_path):
    spec = subpage_specs.build_projects_index_spec(str(tmp_path / "ny"))
    counts = _run(spec, db)
    text = Path(spec.path).read_text()
    assert "## Active" in text
    assert "Marrow" in text
    assert counts["bootstrapped"] == 1


# ── study index ────────────────────────────────────────────────────────────


def test_study_index_bootstrap_emits_unit_links(db, tmp_path):
    """study inserter: two units from Biochem tasks + one Physiology unit."""
    spec = subpage_specs.build_study_index_spec(str(tmp_path / "ny"))
    counts = _run(spec, db)
    text = Path(spec.path).read_text()
    # Two distinct units: Biochem and Physiology
    assert "[[study/Biochem|Biochem]]" in text
    assert "[[study/Physiology|Physiology]]" in text
    # Deduplication: Biochem:Unit 3 and Unit 4 collapse to one Biochem row
    assert text.count("[[study/Biochem|Biochem]]") == 1
    assert counts["bootstrapped"] == 2


# ── stub subpages (profile / stickers / wallet) ────────────────────────────


def test_profile_bootstrap_emits_empty_placeholder(db, tmp_path):
    spec = subpage_specs.build_profile_spec(str(tmp_path / "ny"))
    counts = _run(spec, db)
    text = Path(spec.path).read_text()
    assert "Profile entries land here" in text
    assert counts["bootstrapped"] == 0


def test_stickers_bootstrap_emits_empty_placeholder(db, tmp_path):
    spec = subpage_specs.build_stickers_spec(str(tmp_path / "ny"))
    counts = _run(spec, db)
    text = Path(spec.path).read_text()
    assert "No stickers yet" in text
    assert counts["bootstrapped"] == 0


def test_wallet_bootstrap_emits_empty_placeholder(db, tmp_path):
    spec = subpage_specs.build_wallet_spec(str(tmp_path / "ny"))
    counts = _run(spec, db)
    text = Path(spec.path).read_text()
    assert "Phase 5" in text
    assert counts["bootstrapped"] == 0


# ── registry ───────────────────────────────────────────────────────────────


def test_registry_covers_expected_subpages():
    keys = set(subpage_specs.SPEC_BUILDERS)
    expected = {"profile", "milestone", "diary", "memes",
                "stickers", "wallet", "projects", "study"}
    assert expected.issubset(keys)
    assert "goose" not in keys
    # Cheatsheet stays disk-driven; not in the registry.
    assert "cheatsheet" not in keys


# ── append flow: rerun after row added ─────────────────────────────────────


def test_milestone_new_row_appended_on_rerun(db, tmp_path):
    spec = subpage_specs.build_milestone_spec(str(tmp_path / "ny"))
    _run(spec, db)
    # Add another milestone.
    conn = storage.connect(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO milestones(scope,date,title,pinned)"
                " VALUES('us','2026-04-10','Anniversary',1)"
            )
    finally:
        conn.close()
    counts = _run(spec, db)
    text = Path(spec.path).read_text()
    assert "Anniversary" in text
    assert "First meeting" in text  # preserved
    assert counts["appended"] == 1
    assert counts["preserved"] == 2
