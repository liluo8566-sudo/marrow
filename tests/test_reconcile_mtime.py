"""mtime gate tests for reconcile UPDATE/archive passes.

For each gate: insert a row with a future timestamp, run reconcile,
assert the row is not overwritten by stale md content.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from marrow import dashboard, reconcile, storage


@pytest.fixture()
def conn(tmp_path):
    c = storage.init_db(str(tmp_path / "t.db"))
    yield c
    c.close()


def _future_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))


def _past_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))


# ── 1. milestones UPDATE gate ─────────────────────────────────────────────────

def _ms_md(mid: int, title: str, desc: str) -> str:
    return (
        f"<!-- marrow:milestone:start -->\n"
        f"## Me\n"
        f"##### [2026-01-01] {title}\n"
        f"{desc} <!-- id:{mid} -->\n"
        f"<!-- marrow:milestone:end -->\n"
    )


def test_milestones_update_gate_skips_newer_row(conn, tmp_path):
    """Row updated after md snapshot must not be overwritten."""
    cur = conn.execute(
        "INSERT INTO milestones (scope, date, title, description, theme, pinned, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        ("Me", "2026-01-01", "Old title", "Old desc", None, _past_ts(), _past_ts()),
    )
    conn.commit()
    mid = cur.lastrowid

    # md reflects old title — reconcile would normally update to "Old title"
    # but DB row now has a future updated_at, so gate should block it
    md = tmp_path / "milestones.md"
    md.write_text(_ms_md(mid, "Old title", "Old desc"))

    # Simulate: DB row updated AFTER the md was written
    conn.execute(
        "UPDATE milestones SET title=?, updated_at=? WHERE id=?",
        ("Newer title", _future_ts(), mid),
    )
    conn.commit()

    rpt = reconcile.reconcile_milestones(conn, md)

    row = conn.execute("SELECT title FROM milestones WHERE id=?", (mid,)).fetchone()
    assert row["title"] == "Newer title", "mtime gate failed: DB row was overwritten"
    assert rpt.updated == 0


def test_milestones_update_gate_allows_older_row(conn, tmp_path):
    """Row updated before md snapshot IS allowed to be overwritten."""
    cur = conn.execute(
        "INSERT INTO milestones (scope, date, title, description, theme, pinned, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        ("Me", "2026-01-01", "Old title", "Old desc", None, _past_ts(), _past_ts()),
    )
    conn.commit()
    mid = cur.lastrowid

    md = tmp_path / "milestones.md"
    md.write_text(_ms_md(mid, "New title", "New desc"))

    rpt = reconcile.reconcile_milestones(conn, md)

    row = conn.execute("SELECT title FROM milestones WHERE id=?", (mid,)).fetchone()
    assert row["title"] == "New title"
    assert rpt.updated == 1


# ── 2. tasks archive gate ─────────────────────────────────────────────────────

def _render_dashboard(conn, tmp_path) -> Path:
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    return dash


def test_tasks_archive_gate_skips_newer_row(conn, tmp_path):
    """Task created after md snapshot must not be archived by absence."""
    # Insert task with a past timestamp so it gets rendered in the trail
    cur = conn.execute(
        "INSERT INTO tasks (category, title, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("Study", "Old task", "active", _past_ts(), _past_ts()),
    )
    conn.commit()
    old_tid = cur.lastrowid

    # Render dashboard to capture trail
    dash = _render_dashboard(conn, tmp_path)

    # Now insert a NEW task with future created_at (after md was written)
    cur2 = conn.execute(
        "INSERT INTO tasks (category, title, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("Study", "New task", "active", _future_ts(), _future_ts()),
    )
    conn.commit()
    new_tid = cur2.lastrowid

    # Manually add new_tid to trail in the md so reconcile sees it as missing-from-anchored
    text = dash.read_text()
    # Inject new_tid into trail marker so it's in trail_ids but not anchored
    text = text.replace(
        "<!-- tasks-trail:",
        f"<!-- tasks-trail:{new_tid},",
    )
    dash.write_text(text)

    rpt = reconcile.reconcile_tasks(conn, dash)

    row = conn.execute("SELECT status FROM tasks WHERE id=?", (new_tid,)).fetchone()
    assert row["status"] == "active", "mtime gate failed: new task was archived"


def test_tasks_archive_gate_archives_older_row(conn, tmp_path):
    """Task created before md snapshot IS archived when absent."""
    cur = conn.execute(
        "INSERT INTO tasks (category, title, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("Study", "Removable task", "active", _past_ts(), _past_ts()),
    )
    conn.commit()
    tid = cur.lastrowid

    dash = _render_dashboard(conn, tmp_path)

    # Remove the task row from the md (keep trail but remove anchor)
    text = dash.read_text()
    lines = [ln for ln in text.splitlines() if f"<!-- id:{tid} -->" not in ln]
    dash.write_text("\n".join(lines))

    rpt = reconcile.reconcile_tasks(conn, dash)

    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "archived"
    assert rpt.deleted >= 1


# ── 3. tasks retitle gate ────────────────────────────────────────────────────


def _task_dashboard_md(tid: int, title: str) -> str:
    """Minimal dashboard md with one active task row and matching trail."""
    return (
        f"<!-- tasks-trail:{tid} -->\n"
        f"- [ ] {title} <!-- id:{tid} -->\n"
        f"<!-- tasks-trail-end -->\n"
    )


def test_tasks_retitle_gate_skips_newer_row(conn, tmp_path):
    """Task updated after md snapshot must not have its title overwritten."""
    cur = conn.execute(
        "INSERT INTO tasks (category, title, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("Study", "DB title", "active", _past_ts(), _future_ts()),
    )
    conn.commit()
    tid = cur.lastrowid

    dash = tmp_path / "dashboard.md"
    dash.write_text(_task_dashboard_md(tid, "MD stale title"))

    rpt = reconcile.reconcile_tasks(conn, dash)

    row = conn.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["title"] == "DB title", "mtime gate failed: task title was overwritten"
    assert rpt.updated == 0


# ── 4. affect UPDATE gate ─────────────────────────────────────────────────────

def _insert_affect(conn, *, label: str, description: str, created_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, "
        "label, description, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("2026-01-01", 1, 0.5, 0.5, 3, label, description, created_at),
    )
    conn.commit()
    return cur.lastrowid


def _affect_md(aid: int, label: str, desc: str) -> str:
    # Use ep_segs bullet format: - 【tone】 · eph1 label | desc <!-- aff:N -->
    return (
        f"## Affect\n"
        f"- 【平淡】 · eph1 {label} | {desc} <!-- aff:{aid} -->\n"
    )


def test_affect_update_gate_skips_newer_row(conn, tmp_path):
    """Affect row created after md snapshot must not be rewritten."""
    aid = _insert_affect(
        conn, label="original", description="original", created_at=_future_ts()
    )

    dash = tmp_path / "dashboard.md"
    dash.write_text(_affect_md(aid, "edited", "edited desc"))

    rpt = reconcile.reconcile_affect(conn, dash)

    row = conn.execute("SELECT description FROM affect WHERE id=?", (aid,)).fetchone()
    assert row["description"] == "original", "mtime gate failed: affect row was overwritten"
    assert rpt.updated == 0


def test_affect_update_gate_allows_older_row(conn, tmp_path):
    """Affect row created before md snapshot IS updated."""
    aid = _insert_affect(
        conn, label="original", description="original", created_at=_past_ts()
    )

    dash = tmp_path / "dashboard.md"
    dash.write_text(_affect_md(aid, "edited", "edited desc"))

    rpt = reconcile.reconcile_affect(conn, dash)

    row = conn.execute("SELECT description FROM affect WHERE id=?", (aid,)).fetchone()
    assert row["description"] == "edited desc"
    assert rpt.updated == 1
