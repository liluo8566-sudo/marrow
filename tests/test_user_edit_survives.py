"""End-to-end regression tests for "user edits get clobbered on refresh".

Pre-fix bugs (now closed):
- mw refresh called MdIndex.sync_file which overwrote the content_hash
  baseline. The dashboard inserter then saw `stored == cur_hash` and
  walked into the "no edit since last auto-write" branch → fresh DB
  body overwrote Lumi's edit. Fixed by sync_file_observe.
- dashboard.tasks block was RECONCILED but reconcile_tasks ignored
  title text edits. Fixed by extending reconcile_tasks.
- dashboard.affect had no append mechanism: hash-skip preserved the
  whole block, blocking new sessionend eps. Fixed by per-row anchors
  + RECONCILED-mode + reconcile_affect.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from marrow import cli, config, dashboard, md_index, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    conn.execute(
        "INSERT INTO tasks(category,title,status,due,next_step) "
        "VALUES('study','Essay 370','active','2026-05-20','write intro')"
    )
    today = datetime.now(timezone.utc).date().isoformat()
    conn.execute(
        "INSERT INTO affect(date, ep, valence, arousal, importance, label, description) "
        "VALUES (?, 1, 0.7, 0.7, 3, '开心', '原描述')",
        (today,),
    )
    conn.commit()
    conn.close()
    dash = tmp_path / "dashboard.md"
    sub = tmp_path / "db-pages"
    state = tmp_path / "state"
    monkeypatch.setattr(config, "dashboard_path", lambda: str(dash))
    monkeypatch.setattr(config, "sub_pages_path", lambda: str(sub))
    monkeypatch.setattr(config, "sub_pages_state_path", lambda: str(state))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db, dash


def test_dashboard_affect_edit_survives_mw_refresh(env):
    """The exact failure Lumi reported: edit affect text in md, run
    `mw refresh`. Edit must survive."""
    db, dash = env
    # First refresh — bootstraps dashboard.md.
    assert cli.main(["refresh", "--db", db]) == 0
    text = dash.read_text(encoding="utf-8")
    assert "原描述" in text, "fixture should render initial affect"
    # Hand-edit the description.
    edited = text.replace("原描述", "Lumi 的手改")
    dash.write_text(edited, encoding="utf-8")
    # Run mw refresh again — Bug 1 path (sync_file → write_dashboard).
    assert cli.main(["refresh", "--db", db]) == 0
    final = dash.read_text(encoding="utf-8")
    assert "Lumi 的手改" in final, \
        "affect description edit must survive mw refresh"
    assert "原描述" not in final, \
        "old description must not resurface (DB absorbed Lumi's edit)"


def test_dashboard_alerts_edit_survives_mw_refresh(tmp_path, monkeypatch):
    """Non-RECONCILED block (alerts) — preserved via hash-skip after
    sync_file_observe keeps the baseline frozen."""
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    conn.execute(
        "INSERT INTO alerts(severity,type,message) "
        "VALUES('warn','bug','recall returned 0')"
    )
    conn.commit()
    conn.close()
    dash = tmp_path / "dashboard.md"
    monkeypatch.setattr(config, "dashboard_path", lambda: str(dash))
    monkeypatch.setattr(config, "sub_pages_path", lambda: str(tmp_path / "x"))
    monkeypatch.setattr(config, "sub_pages_state_path", lambda: str(tmp_path / "y"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    assert cli.main(["refresh", "--db", db]) == 0
    text = dash.read_text(encoding="utf-8")
    edited = text.replace(
        "- warn: recall returned 0",
        "- warn: recall returned 0 (lumi investigating)",
    )
    dash.write_text(edited, encoding="utf-8")

    # Second refresh — Bug 1 path. Must NOT clobber.
    assert cli.main(["refresh", "--db", db]) == 0
    final = dash.read_text(encoding="utf-8")
    assert "lumi investigating" in final


def test_dashboard_user_edit_survives_watcher_sync(env, tmp_path):
    """Simulate the watcher's debounced sync_file_observe pass between
    auto-writes. The baseline must remain frozen so the next
    write_dashboard sees `stored != cur_hash` and preserves the edit."""
    db, dash = env
    dashboard.write_dashboard(
        str(dash), storage.connect(db),
        state_dir=str(tmp_path / "state"),
    )
    text = dash.read_text(encoding="utf-8")
    edited = text.replace(
        "- warn:" if "- warn:" in text else "原描述",
        "<edited>", 1,
    ) if "原描述" in text else text
    if "原描述" in text:
        edited = text.replace("原描述", "Lumi 改")
    else:
        pytest.skip("fixture did not render expected affect text")
    dash.write_text(edited, encoding="utf-8")

    # Watcher debounce fires sync_file_observe.
    conn = storage.connect(db)
    try:
        md_index.MdIndex(conn).sync_file_observe(str(dash))
        # Subsequent dashboard refresh must preserve the edit on
        # non-RECONCILED branches; affect block is RECONCILED so
        # reconcile_affect absorbs the edit and the render reproduces it.
        dashboard.write_dashboard(
            str(dash), conn, state_dir=str(tmp_path / "state"),
        )
    finally:
        conn.close()
    final = dash.read_text(encoding="utf-8")
    assert "Lumi 改" in final, \
        "affect edit must survive sync_file_observe → write_dashboard"


def test_task_title_edit_persists_across_refresh(env):
    """Title rewrite '123' → '321'-style edit. mw refresh absorbs it into
    DB via reconcile_tasks; the rendered body shows Lumi's text; a later
    new task lands without clobbering the kept edit."""
    db, dash = env
    assert cli.main(["refresh", "--db", db]) == 0
    text = dash.read_text(encoding="utf-8")
    assert "Essay 370" in text
    # Replace title segment between `[study] ` and the next ` :` or ` [`.
    out_lines = []
    for ln in text.splitlines():
        if "<!-- id:1 -->" in ln and "Essay 370" in ln:
            ln = ln.replace("Essay 370", "Essay 370 (final draft)")
        out_lines.append(ln)
    dash.write_text("\n".join(out_lines), encoding="utf-8")
    assert cli.main(["refresh", "--db", db]) == 0
    after = dash.read_text(encoding="utf-8")
    assert "Essay 370 (final draft)" in after, \
        "title edit must persist through refresh"
    conn = storage.connect(db)
    try:
        title = conn.execute(
            "SELECT title FROM tasks WHERE id=1"
        ).fetchone()[0]
        # Add a new task to DB simulating sessionend.
        conn.execute(
            "INSERT INTO tasks(category,title,status,next_step) "
            "VALUES('study','Brand new task','active','x')"
        )
        conn.commit()
    finally:
        conn.close()
    assert title == "Essay 370 (final draft)"
    assert cli.main(["refresh", "--db", db]) == 0
    after2 = dash.read_text(encoding="utf-8")
    assert "Brand new task" in after2, "new task must surface"
    assert "Essay 370 (final draft)" in after2, \
        "earlier edit must still be there"
