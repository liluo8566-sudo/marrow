"""Tests for reconcile_tasks and related render/sessionend fixes."""
from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from marrow import reconcile, storage
from marrow import dashboard, top_sections

_TZ = ZoneInfo("Australia/Melbourne")


@pytest.fixture()
def conn(tmp_path):
    db = str(tmp_path / "t.db")
    c = storage.init_db(db)
    yield c
    c.close()


def _insert_task(conn, title: str, status: str = "active",
                 category: str = "Study", updated_at: str | None = None) -> int:
    ts = updated_at or datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    cur = conn.execute(
        "INSERT INTO tasks (category, title, status, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (category, title, status, ts),
    )
    conn.commit()
    return cur.lastrowid


def _render_dashboard(conn, tmp_path) -> Path:
    """Write a fresh dashboard and return its path."""
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    return dash


# ── 1. tick: [ ] -> [x] flips DB to done ─────────────────────────────────────

def test_tick_sets_done(conn, tmp_path):
    tid = _insert_task(conn, "Write notes")
    dash = _render_dashboard(conn, tmp_path)
    text = dash.read_text()
    # Simulate Lumi ticking the checkbox.
    text = text.replace(f"- [ ] [Study] Write notes", f"- [x] [Study] Write notes")
    dash.write_text(text)

    rpt = reconcile.reconcile_tasks(conn, dash)

    assert rpt.updated == 1
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "done"


# ── 2. untick: [x] -> [ ] flips DB back to active ────────────────────────────

def test_untick_sets_active(conn, tmp_path):
    # Use today midnight to ensure it shows in Completed.
    today_midnight = datetime.datetime.combine(
        datetime.datetime.now(_TZ).date(),
        datetime.time(0, 0),
        tzinfo=_TZ,
    ).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tid = _insert_task(conn, "Review slides", status="done",
                       updated_at=today_midnight)
    dash = _render_dashboard(conn, tmp_path)
    text = dash.read_text()
    # Simulate Lumi un-ticking.
    text = text.replace(
        f"- [x] [Study] Review slides <!-- id:{tid} -->",
        f"- [ ] [Study] Review slides <!-- id:{tid} -->",
    )
    dash.write_text(text)

    rpt = reconcile.reconcile_tasks(conn, dash)

    assert rpt.updated == 1
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "active"


# ── 3. delete-by-trail: id in trail, missing from md -> archived ──────────────

def test_delete_by_trail_archives(conn, tmp_path):
    tid = _insert_task(conn, "Finish assignment")
    dash = _render_dashboard(conn, tmp_path)
    text = dash.read_text()
    # Strip the task row but keep the trail marker intact.
    lines = [
        ln for ln in text.splitlines()
        if f"<!-- id:{tid} -->" not in ln
    ]
    dash.write_text("\n".join(lines))

    rpt = reconcile.reconcile_tasks(conn, dash)

    assert rpt.deleted == 1
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "archived"


# ── 4. no-op when trail absent ────────────────────────────────────────────────

def test_noop_when_no_trail(conn, tmp_path):
    _insert_task(conn, "Old task")
    # Write a dashboard without anchors (manually constructed, no trail).
    dash = tmp_path / "dashboard.md"
    dash.write_text("## Tasks\n### Completed [0]\n- (none)\n### To-Do List [1]\nToday\n- [ ] [Study] Old task\n")

    rpt = reconcile.reconcile_tasks(conn, dash)

    assert rpt.updated == 0
    assert rpt.deleted == 0
    assert not rpt.conflicts


# ── 5. _seg_task_cand: done status UPDATEs active row, no INSERT ──────────────

def test_seg_task_cand_done_updates_not_inserts(conn, tmp_path):
    from marrow.sessionend_async import _seg_task_cand

    tid = _insert_task(conn, "Fix bug", status="active")
    before = conn.execute("SELECT COUNT(*) c FROM tasks WHERE title='Fix bug'").fetchone()["c"]
    assert before == 1

    raw = '===TASK_CAND===\n[{"title": "Fix bug", "status": "done", "category": "Project"}]\n===END==='
    _seg_task_cand(conn, raw)

    rows = conn.execute("SELECT status FROM tasks WHERE title='Fix bug'").fetchall()
    assert len(rows) == 1  # no duplicate
    assert rows[0]["status"] == "done"


# ── 6. _seg_task_cand: archived title skips insert ───────────────────────────

def test_seg_task_cand_skips_archived(conn, tmp_path):
    from marrow.sessionend_async import _seg_task_cand

    _insert_task(conn, "Old task", status="archived")

    raw = '===TASK_CAND===\n[{"title": "Old task", "status": "active", "category": "Others"}]\n===END==='
    _seg_task_cand(conn, raw)

    count = conn.execute("SELECT COUNT(*) c FROM tasks WHERE title='Old task'").fetchone()["c"]
    assert count == 1  # still only one row


# ── 7. Completed cutoff: task done at 02:00 local shows in Completed ──────────

def test_completed_cutoff_midnight_not_6am(conn, tmp_path):
    # 02:00 local today — was hidden under old 6AM cutoff, must now show.
    today_2am = datetime.datetime.combine(
        datetime.datetime.now(_TZ).date(),
        datetime.time(2, 0),
        tzinfo=_TZ,
    ).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_task(conn, "Early morning task", status="done", updated_at=today_2am)

    md = top_sections.render_tasks(conn)
    assert "Early morning task" in md
    assert "- [x]" in md
