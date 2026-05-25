"""Tests for marrow/dashboard.py — inserter-mode dashboard top render.

Phase 3 contract (md=SoT):
- deterministic 5-section block between `<!-- marrow:top:* -->` markers
  with per-block `<!-- id:dashboard.* -->` ids
- hand-written zone outside markers never touched
- Reconciled blocks (tasks + milestone_cand) ALWAYS overwrite — the
  reconcile pass absorbed any user edit into the DB first
- Pure-display blocks (alerts + affect + content) honour hash-skip:
  user hand-edit preserved when md_index hash diverges from md body
- Tombstoned blocks (watcher saw user delete the block) are not re-emitted
"""
from __future__ import annotations

from pathlib import Path

import pytest

from marrow import dashboard, storage, top_sections
from marrow.md_index import MdIndex

M0 = "<!-- marrow:top:start -->"
M1 = "<!-- marrow:top:end -->"


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    conn.execute("INSERT INTO tasks(category,title,status,due,next_step) "
                 "VALUES('study','Essay 370','active','2026-05-20','write intro')")
    conn.execute("INSERT INTO alerts(severity,type,message) "
                 "VALUES('warn','bug','recall returned 0')")
    conn.commit()
    conn.close()
    return p


def test_render_top_has_alerts_and_tasks(db):
    conn = storage.connect(db)
    try:
        block = dashboard.render_top(conn)
    finally:
        conn.close()
    assert "Essay 370" in block
    assert "recall returned 0" in block
    assert M0 in block and M1 in block


def test_alert_rendered_with_severity(db):
    # Format changed in 2.5b: severity: message (no id prefix, per template spec).
    conn = storage.connect(db)
    try:
        block = dashboard.render_top(conn)
    finally:
        conn.close()
    line = next(ln for ln in block.splitlines() if "recall returned 0" in ln)
    assert line == "- warn: recall returned 0"


def test_write_creates_file_with_block(db, tmp_path):
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    conn = storage.connect(db)
    try:
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
    finally:
        conn.close()
    txt = dash.read_text()
    assert M0 in txt and "Essay 370" in txt


def test_write_preserves_hand_zone(db, tmp_path):
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    dash.write_text(f"{M0}\nOLD BLOCK\n{M1}\n\n## My notes\nkeep me\n")
    conn = storage.connect(db)
    try:
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
    finally:
        conn.close()
    txt = dash.read_text()
    assert "## My notes\nkeep me" in txt
    assert "OLD BLOCK" not in txt
    assert "Essay 370" in txt


def test_render_top_includes_content_section(db, tmp_path, monkeypatch):
    # Content section follows Affect and lists subpages with md links.
    from marrow import subpages

    def fake_load():
        return {"subpages": {"top": ["milestone", "diary"],
                              "bottom": ["cheatsheet"], "hidden": []}}

    monkeypatch.setattr(subpages._config, "load", fake_load)
    dash = tmp_path / "dashboard.md"
    conn = storage.connect(db)
    try:
        block = dashboard.render_top(conn, dashboard_path=str(dash))
    finally:
        conn.close()
    assert "## Content" in block
    # Both top and bottom render as dot bullets; `---` separates them.
    assert "- [Milestone](" in block and "milestone.md" in block
    assert "- [Diary](" in block
    assert "- [Cheatsheet](" in block
    assert "---" in block
    # Numbered list form is gone.
    assert "1. [" not in block and "2. [" not in block
    # Affect section precedes Content
    assert block.index("## Affect") < block.index("## Content")


def test_iter_top_blocks_canonical_ids(db):
    conn = storage.connect(db)
    try:
        pairs = top_sections.iter_top_blocks(conn)
    finally:
        conn.close()
    ids = [bid for bid, _ in pairs]
    assert ids == list(top_sections.DASHBOARD_BLOCK_IDS)
    # Every body carries its id marker.
    for bid, body in pairs:
        assert f"<!-- id:{bid} -->" in body


def test_iter_top_blocks_round_trip_through_dashboard_parser(db):
    # iter_top_blocks output joined into a top region must parse back into
    # exactly the same canonical ids in the same order via the dashboard
    # block parser (which scopes boundaries to `dashboard.<key>` markers and
    # ignores per-row `<!-- id:N -->` anchors inside the body).
    conn = storage.connect(db)
    try:
        pairs = top_sections.iter_top_blocks(conn)
    finally:
        conn.close()
    joined = "\n\n".join(body for _, body in pairs)
    parsed = dashboard._parse_top_blocks(joined)
    assert list(parsed.keys()) == list(top_sections.DASHBOARD_BLOCK_IDS)


def test_tasks_title_edit_absorbed_into_db(db, tmp_path):
    # Tasks block is RECONCILED: reconcile_tasks absorbs Lumi's title edit
    # back into the DB before render, so the next render reproduces her
    # edited title rather than clobbering it.
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    conn = storage.connect(db)
    try:
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        # Lumi free-form edits the task title (not a tick/untick/delete)
        t = dash.read_text().replace("Essay 370", "Essay 370 EDITED BY LUMI")
        dash.write_text(t)
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        result = dash.read_text()
        db_title = conn.execute(
            "SELECT title FROM tasks WHERE id=1"
        ).fetchone()[0]
        alerts = [a["message"] for a in
                  __import__("marrow.repo", fromlist=["x"]).open_alerts(conn)]
    finally:
        conn.close()
    assert "EDITED BY LUMI" in result, \
        "title edit must survive re-render via DB absorption"
    assert db_title == "Essay 370 EDITED BY LUMI"
    assert not any("dashboard" in m.lower() and "hand-edited" in m.lower()
                   for m in alerts)
    assert not list(Path(state).glob("dashboard*.bak"))


def test_alerts_hand_edit_preserved_when_hash_diverges(db, tmp_path):
    # Alerts is a pure-display block — hash-skip preserves Lumi's edit.
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    conn = storage.connect(db)
    try:
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        text = dash.read_text()
        edited = text.replace(
            "- warn: recall returned 0",
            "- warn: recall returned 0 (lumi noted: investigating)",
        )
        assert edited != text
        dash.write_text(edited)
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        result = dash.read_text()
    finally:
        conn.close()
    assert "lumi noted: investigating" in result


def test_content_block_carries_id_marker(db, tmp_path):
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    conn = storage.connect(db)
    try:
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        text = dash.read_text()
    finally:
        conn.close()
    for bid in (
        "dashboard.alerts", "dashboard.tasks", "dashboard.milestone_cand",
        "dashboard.affect", "dashboard.content",
    ):
        assert f"<!-- id:{bid} -->" in text, bid


def test_tombstoned_block_not_reemitted(db, tmp_path):
    # Simulates watcher tombstone of the Alerts block — inserter must skip it.
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    conn = storage.connect(db)
    try:
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        # Watcher would tombstone after user deletes the block from md;
        # we record the tombstone directly here.
        MdIndex(conn).tombstone(str(dash), "dashboard.alerts")
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        result = dash.read_text()
    finally:
        conn.close()
    assert "<!-- id:dashboard.alerts -->" not in result
    assert "## Tasks" in result  # other blocks still rendered


def test_new_task_appears_on_next_render(db, tmp_path):
    # Adding a task to the DB shows up in the Tasks block on the next render
    # even though the block already exists (reconciled block always overwrite).
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    conn = storage.connect(db)
    try:
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        first = dash.read_text()
        assert "New task brand new" not in first
        conn.execute(
            "INSERT INTO tasks(category,title,status,next_step) "
            "VALUES('study','New task brand new','active','x')"
        )
        conn.commit()
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        result = dash.read_text()
    finally:
        conn.close()
    assert "New task brand new" in result


def test_tick_in_md_moves_row_to_completed(db, tmp_path):
    # End-to-end: user ticks `[ ]` to `[x]` in dashboard.md, write_dashboard
    # runs reconcile then re-renders. Row should land in Completed.
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    conn = storage.connect(db)
    try:
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        text = dash.read_text()
        ticked = text.replace(
            "- [ ] [study] Essay 370",
            "- [x] [study] Essay 370",
        )
        assert ticked != text, "expected initial render to contain `[ ] [study]`"
        dash.write_text(ticked)
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        result = dash.read_text()
        status = conn.execute(
            "SELECT status FROM tasks WHERE title='Essay 370'").fetchone()[0]
    finally:
        conn.close()
    # DB flipped to done by reconcile, canonical rewrite shows the row in
    # `### Completed` with `[x]`.
    assert status == "done"
    assert "### Completed [1]" in result
    assert "- [x] [study] Essay 370" in result
