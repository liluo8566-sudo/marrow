"""End-to-end regression tests for "user edits get clobbered on refresh".

Pre-fix bugs (now closed):
- mw refresh called MdIndex.sync_file which overwrote the content_hash
  baseline. The dashboard inserter then saw `stored == cur_hash` and
  walked into the "no edit since last auto-write" branch → fresh DB
  body overwrote the user's edit. Fixed by sync_file_observe.
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
    """The exact failure the user reported: edit affect text in md, run
    `mw refresh`. Edit must survive."""
    db, dash = env
    # First refresh — bootstraps dashboard.md.
    assert cli.main(["refresh", "--db", db]) == 0
    text = dash.read_text(encoding="utf-8")
    assert "原描述" in text, "fixture should render initial affect"
    # Hand-edit the description.
    edited = text.replace("原描述", "user 的手改")
    dash.write_text(edited, encoding="utf-8")
    # Run mw refresh again — Bug 1 path (sync_file → write_dashboard).
    assert cli.main(["refresh", "--db", db]) == 0
    final = dash.read_text(encoding="utf-8")
    assert "user 的手改" in final, \
        "affect description edit must survive mw refresh"
    assert "原描述" not in final, \
        "old description must not resurface (DB absorbed the user's edit)"


def test_dashboard_alerts_block_is_db_authoritative(tmp_path, monkeypatch):
    """alerts is a display-only block (ALWAYS_OVERWRITE_BLOCK_IDS).

    DB is sole SoT — user edits do NOT survive refresh. This protects
    against the sticky-empty regression: when md_index lost sync with
    the rendered block, hash-skip hid every live alert for 3 days
    because cur_hash != stored took the "user edit" branch forever.
    """
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
    monkeypatch.setattr(config, "monitor_path", lambda: str(tmp_path / "monitor.md"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    assert cli.main(["refresh", "--db", db]) == 0
    text = dash.read_text(encoding="utf-8")
    edited = text.replace(
        "- warn: recall returned 0",
        "- warn: recall returned 0 (user investigating)",
    )
    dash.write_text(edited, encoding="utf-8")

    # Second refresh — display-only block snaps back to DB content.
    assert cli.main(["refresh", "--db", db]) == 0
    final = dash.read_text(encoding="utf-8")
    assert "user investigating" not in final, \
        "alerts is display-only; user edit must NOT survive refresh"
    assert "- warn: recall returned 0" in final, \
        "DB-driven alert content must always be re-emitted"


def test_dashboard_alerts_recovers_from_stale_md_index_hash(tmp_path, monkeypatch):
    """Regression: sticky-empty bug — alerts hidden for 3 days because
    md_index stored a stale hash and _resolve_blocks took the
    'preserve user edit' branch. alerts is now in RECONCILED_BLOCK_IDS so
    md-side edits are absorbed (delete=resolve) and DB content always
    re-emits. Wiping the block (no id markers) does NOT resolve any rows
    because the md mtime predates any subsequent alert inserts — but
    even if it did, the next render snaps DB live content back in."""
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    conn.execute(
        "INSERT INTO alerts(severity,type,message) "
        "VALUES('critical','x','live alert from DB')"
    )
    conn.commit()
    conn.close()
    dash = tmp_path / "dashboard.md"
    monkeypatch.setattr(config, "dashboard_path", lambda: str(dash))
    monkeypatch.setattr(config, "sub_pages_path", lambda: str(tmp_path / "x"))
    monkeypatch.setattr(config, "sub_pages_state_path", lambda: str(tmp_path / "y"))
    monkeypatch.setattr(config, "monitor_path", lambda: str(tmp_path / "monitor.md"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    assert cli.main(["refresh", "--db", db]) == 0

    # Simulate the in-the-wild drift: alerts block wiped externally to bare
    # H2 (no id markers). Pre-fix this locked the block as 'preserve user
    # edit'. Backdate mtime so the reconcile mtime gate spares all alerts.
    import os as _os
    text = dash.read_text(encoding="utf-8")
    h2_idx = text.index("## Alerts")
    next_block = text.index("\n## ", h2_idx + 1)
    emptied = text[:h2_idx] + "## Alerts\n" + text[next_block + 1:]
    dash.write_text(emptied, encoding="utf-8")
    _os.utime(str(dash), (0, 0))  # mtime = epoch → predates all alerts

    assert cli.main(["refresh", "--db", db]) == 0
    final = dash.read_text(encoding="utf-8")
    assert "live alert from DB" in final, \
        "alerts must recover from md_index hash drift"


def test_dashboard_alerts_zero_anchor_guard(tmp_path, monkeypatch):
    """Safety: legacy/first-render md has no `<!-- id:alert.N -->` markers.
    reconcile_alerts must refuse to mass-resolve in that case — otherwise
    the first refresh after upgrade would nuke every live alert."""
    import os as _os
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    for msg in ("a", "b", "c"):
        conn.execute(
            "INSERT INTO alerts(severity,type,message) VALUES('warn','x',?)",
            (msg,),
        )
    conn.commit()
    conn.close()
    dash = tmp_path / "dashboard.md"
    monkeypatch.setattr(config, "dashboard_path", lambda: str(dash))
    monkeypatch.setattr(config, "sub_pages_path", lambda: str(tmp_path / "x"))
    monkeypatch.setattr(config, "sub_pages_state_path", lambda: str(tmp_path / "y"))
    monkeypatch.setattr(config, "monitor_path", lambda: str(tmp_path / "monitor.md"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    # Hand-write a legacy-style dashboard.md: alerts block without anchors.
    dash.write_text(
        "<!-- marrow:top:start -->\n"
        "<!-- id:dashboard.alerts -->\n"
        "## Alerts\n"
        "- warn: a\n- warn: b\n- warn: c\n\n"
        "<!-- id:dashboard.tasks -->\n## Tasks\n### Completed [0]\n_none_\n"
        "### To-Do List [0]\n_none_\n"
        "<!-- marrow:top:end -->\n",
        encoding="utf-8",
    )
    _os.utime(str(dash), (10**9, 10**9))  # ancient mtime

    assert cli.main(["refresh", "--db", db]) == 0
    conn2 = storage.connect(db)
    unresolved = conn2.execute(
        "SELECT count(*) FROM alerts WHERE resolved=0"
    ).fetchone()[0]
    conn2.close()
    assert unresolved == 3, (
        "first-render against legacy md (no anchors) must NOT mass-resolve"
    )


def test_dashboard_alert_md_delete_resolves_db_row(tmp_path, monkeypatch):
    """md-side delete IS the resolve gesture. Remove the bullet from the
    Alerts block → reconcile_alerts marks resolved=1 → next render omits."""
    import os as _os
    import time as _time
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    conn.execute(
        "INSERT INTO alerts(severity,type,message) "
        "VALUES('warn','x','to-be-dismissed')"
    )
    conn.execute(
        "INSERT INTO alerts(severity,type,message) "
        "VALUES('critical','x','keep-me')"
    )
    conn.commit()
    conn.close()
    dash = tmp_path / "dashboard.md"
    monkeypatch.setattr(config, "dashboard_path", lambda: str(dash))
    monkeypatch.setattr(config, "sub_pages_path", lambda: str(tmp_path / "x"))
    monkeypatch.setattr(config, "sub_pages_state_path", lambda: str(tmp_path / "y"))
    monkeypatch.setattr(config, "monitor_path", lambda: str(tmp_path / "monitor.md"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    assert cli.main(["refresh", "--db", db]) == 0
    text = dash.read_text(encoding="utf-8")
    assert "to-be-dismissed" in text and "keep-me" in text
    # Strip the warn line entirely.
    pruned = "\n".join(
        ln for ln in text.splitlines() if "to-be-dismissed" not in ln
    )
    dash.write_text(pruned, encoding="utf-8")
    # Bump mtime forward so the alert's created_at predates md snapshot.
    future = _time.time() + 5
    _os.utime(str(dash), (future, future))

    assert cli.main(["refresh", "--db", db]) == 0
    final = dash.read_text(encoding="utf-8")
    assert "to-be-dismissed" not in final, \
        "deleted alert must not re-render after reconcile_alerts"
    assert "keep-me" in final, "untouched alert must still render"

    conn3 = storage.connect(db)
    row = conn3.execute(
        "SELECT resolved FROM alerts WHERE message='to-be-dismissed'"
    ).fetchone()
    conn3.close()
    assert row[0] == 1, "md-delete must flip alerts.resolved to 1"


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
        edited = text.replace("原描述", "user 改")
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
    assert "user 改" in final, \
        "affect edit must survive sync_file_observe → write_dashboard"


def test_task_title_edit_persists_across_refresh(env):
    """Title rewrite '123' → '321'-style edit. mw refresh absorbs it into
    DB via reconcile_tasks; the rendered body shows the user's text; a later
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
