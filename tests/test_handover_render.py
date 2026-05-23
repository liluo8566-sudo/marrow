"""Tests for top_sections.py, handover_render.py, and their wiring.

Temp-DB fixture pattern follows tests/test_hooks.py.
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from marrow import config, hooks, storage
from marrow import top_sections, handover_render


# ── shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    dash = str(tmp_path / "dashboard.md")
    sub_folder = str(tmp_path / "sub_pages")
    sub_state = str(tmp_path / "sub_state")
    rendered_handover = tmp_path / "handover.md"
    conn = storage.init_db(db)
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "dashboard_path", lambda: dash)
    monkeypatch.setattr(config, "sub_pages_path", lambda: sub_folder)
    monkeypatch.setattr(config, "sub_pages_state_path", lambda: sub_state)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # Redirect handover_render output path to tmp
    monkeypatch.setattr(handover_render, "_RENDERED_PATH", rendered_handover)
    return db, dash, tmp_path, rendered_handover


def _conn(db: str):
    return storage.connect(db)


def _insert_affect(conn, date: str, ep: int, valence: float, arousal: float,
                   importance: int = 3, label: str | None = None,
                   source: str | None = None, created_at: str | None = None):
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (date, ep, valence, arousal, importance, label, source,
         created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    conn.commit()


# ── Unit: render_alerts ───────────────────────────────────────────────────────

def test_render_alerts_active_only(env):
    db, _, _, _ = env
    conn = _conn(db)
    # 2 active, 1 resolved
    conn.execute("INSERT INTO alerts(severity,type,message,resolved) VALUES('warn','test','active-a',0)")
    conn.execute("INSERT INTO alerts(severity,type,message,resolved) VALUES('critical','test','active-b',0)")
    conn.execute("INSERT INTO alerts(severity,type,message,resolved) VALUES('warn','test','should-not-appear',1)")
    conn.commit()

    out = top_sections.render_alerts(conn)
    conn.close()

    assert "active-a" in out
    assert "active-b" in out
    assert "should-not-appear" not in out


def test_render_alerts_empty(env):
    db, _, _, _ = env
    conn = _conn(db)
    out = top_sections.render_alerts(conn)
    conn.close()
    assert "- (none)" in out


# ── Unit: render_tasks ────────────────────────────────────────────────────────

def test_render_tasks_grouping_by_tag_and_time(env):
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()
    far = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()

    # Today tasks (no due date, but we also test with a today due)
    conn.execute("INSERT INTO tasks(category,title,status,due) VALUES('Study','Study today','active',?)", (today,))
    conn.execute("INSERT INTO tasks(category,title,status,due) VALUES('Project','Project tomorrow','active',?)", (tomorrow,))
    conn.execute("INSERT INTO tasks(category,title,status,due) VALUES('Daily','Daily later','active',?)", (far,))
    # No-due ends up in Later
    conn.execute("INSERT INTO tasks(category,title,status) VALUES('Appointment','No due task','active')")
    # Done today
    conn.execute("INSERT INTO tasks(category,title,status,updated_at) VALUES('Study','Done study','done',?)",
                 (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    conn.commit()

    out = top_sections.render_tasks(conn)
    conn.close()

    # Completed block comes before To-Do
    assert out.index("### Completed") < out.index("### To-Do List")
    # Today block before Next 7 Days
    assert out.index("Today") < out.index("Next 7 Days")
    # Study tag appears in output
    assert "Study" in out
    # Project appears in Next 7 Days section
    next7_section = out.split("Next 7 Days")[1].split("Later")[0]
    assert "Project" in next7_section
    # Done task appears in Completed
    completed_section = out.split("### Completed")[1].split("### To-Do")[0]
    assert "Done study" in completed_section


def test_render_tasks_tag_order(env):
    """Study comes before Project comes before Others."""
    db, _, _, _ = env
    conn = _conn(db)
    far = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    conn.execute("INSERT INTO tasks(category,title,status,due) VALUES('Others','Others task','active',?)", (far,))
    conn.execute("INSERT INTO tasks(category,title,status,due) VALUES('Study','Study task','active',?)", (far,))
    conn.execute("INSERT INTO tasks(category,title,status,due) VALUES('Project','Project task','active',?)", (far,))
    conn.commit()

    out = top_sections.render_tasks(conn)
    conn.close()

    later_section = out.split("Later")[1]
    assert later_section.index("Study task") < later_section.index("Project task")
    assert later_section.index("Project task") < later_section.index("Others task")


# ── Unit: render_milestone_candidate ─────────────────────────────────────────

def test_render_milestone_candidate_relative_time(env):
    db, _, _, _ = env
    conn = _conn(db)
    three_h_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(timezone.utc).date().isoformat()
    conn.execute(
        "INSERT INTO milestones(scope,date,title,pinned,created_at) VALUES('me',?,?,0,?)",
        (today, "Test milestone", three_h_ago),
    )
    conn.commit()

    out = top_sections.render_milestone_candidate(conn)
    conn.close()
    assert "3h ago" in out
    assert "Test milestone" in out


def test_render_milestone_candidate_pinned_excluded(env):
    """pinned=1 rows must not appear in candidates."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    conn.execute(
        "INSERT INTO milestones(scope,date,title,pinned) VALUES('me',?,'Pinned one',1)", (today,))
    conn.execute(
        "INSERT INTO milestones(scope,date,title,pinned) VALUES('me',?,'Candidate one',0)", (today,))
    conn.commit()

    out = top_sections.render_milestone_candidate(conn)
    conn.close()
    assert "Pinned one" not in out
    assert "Candidate one" in out


def test_render_milestone_candidate_empty(env):
    db, _, _, _ = env
    conn = _conn(db)
    out = top_sections.render_milestone_candidate(conn)
    conn.close()
    assert "- (none)" in out


# ── Unit: render_affect ───────────────────────────────────────────────────────

def test_render_affect_today_band_excited(env):
    """v=0.7 a=0.7 → 兴奋 (High/Active)."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.7, 0.7, importance=3, label="开心")
    out = top_sections.render_affect(conn)
    conn.close()
    assert "兴奋" in out


def test_render_affect_today_band_low(env):
    """v=0.2 a=0.2 → 低落 (Low/Calm)."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.2, 0.2, importance=3, label="难过")
    out = top_sections.render_affect(conn)
    conn.close()
    assert "低落" in out


def test_render_affect_week_variance_label(env):
    """stddev(v) > 0.3 → A → B arrow in week line."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date()
    # Alternate between very high and very low valence
    for i in range(6):
        d = (today - timedelta(days=i)).isoformat()
        v = 0.9 if i % 2 == 0 else 0.1
        _insert_affect(conn, d, 1, v, 0.5, importance=3)
    out = top_sections.render_affect(conn)
    conn.close()
    week_section = out.split("### This Week")[1].split("### Pending")[0]
    assert "→" in week_section


def test_render_affect_pending_empty_until_2_5c(env):
    """Pending block body must be '- (none)' — unresolved column not yet in schema."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.5, 0.5, importance=2)
    out = top_sections.render_affect(conn)
    conn.close()
    pending_section = out.split("### Pending")[1]
    assert "- (none)" in pending_section


def test_render_affect_empty_tables(env):
    """No affect rows → all subsections still render with (none)."""
    db, _, _, _ = env
    conn = _conn(db)
    out = top_sections.render_affect(conn)
    conn.close()
    assert "### Today" in out
    assert "### This Week" in out
    assert "### Pending" in out


# ── Unit: handover_render ─────────────────────────────────────────────────────

def test_handover_write_atomic_and_handover_stamp(env, monkeypatch, tmp_path):
    """write_handover produces file at _RENDERED_PATH with sid stamp + 4 sections."""
    db, _, _, rendered_path = env
    conn = _conn(db)
    result_path = handover_render.write_handover(conn, "abc123")
    conn.close()

    assert result_path == rendered_path
    assert rendered_path.exists()
    content = rendered_path.read_text(encoding="utf-8")

    # Narrative stamp present
    assert "<!-- handover: pending sid:abc123 -->" in content
    # All 4 section headers present
    assert "## Alerts (active)" in content
    assert "## Tasks" in content
    assert "## Milestone candidate" in content
    assert "## Affect" in content


def test_handover_write_strips_instruction_lines(env):
    """Lines starting with '> ' must not appear in the rendered output."""
    db, _, _, rendered_path = env
    conn = _conn(db)
    handover_render.write_handover(conn, "s1")
    conn.close()
    content = rendered_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        assert not line.startswith("> "), f"Instruction line leaked: {line!r}"


def test_handover_write_atomic_via_replace(env, monkeypatch, tmp_path):
    """Atomic write uses os.replace (rename). Verify via monkeypatch."""
    db, _, _, rendered_path = env
    replace_calls = []
    real_replace = os.replace

    def tracking_replace(src, dst):
        replace_calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", tracking_replace)
    conn = _conn(db)
    handover_render.write_handover(conn, "s-atomic")
    conn.close()

    assert len(replace_calls) >= 1
    # The destination must be the rendered path
    assert any(str(rendered_path) in str(dst) for _, dst in replace_calls)


def test_handover_timestamp_replaced(env):
    """{{YYYY-MM-DD HH:MM}} placeholder is replaced with current time."""
    db, _, _, rendered_path = env
    conn = _conn(db)
    handover_render.write_handover(conn, "s-ts")
    conn.close()
    content = rendered_path.read_text(encoding="utf-8")
    assert "{{YYYY-MM-DD HH:MM}}" not in content
    # Should contain a date in YYYY-MM-DD format
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", content)


# ── Unit: dashboard swap ──────────────────────────────────────────────────────

def test_dashboard_top_now_uses_4_sections(env):
    """render_top from dashboard should use top_sections and contain all 4 headers."""
    from marrow import dashboard
    db, dash, _, _ = env
    conn = _conn(db)
    block = dashboard.render_top(conn)
    conn.close()

    assert "## Alerts (active)" in block
    assert "## Tasks" in block
    assert "## Milestone candidate" in block
    assert "## Affect" in block
    # Old "Open Tasks" header must not appear
    assert "## Open Tasks" not in block


def test_dashboard_top_markers_present(env):
    """M0/M1 markers are emitted by write_dashboard."""
    from marrow import dashboard
    db, dash, tmp_path, _ = env
    conn = _conn(db)
    dashboard.write_dashboard(dash, conn, state_dir=str(tmp_path / "state"), db=db)
    conn.close()
    content = Path(dash).read_text(encoding="utf-8")
    assert dashboard.M0 in content
    assert dashboard.M1 in content
    assert "## Alerts (active)" in content


# ── Unit: hook wiring ─────────────────────────────────────────────────────────

def test_handover_render_hook_invoked_on_session_end(env, monkeypatch, tmp_path):
    """session_end must call handover_render.write_handover once with the session_id."""
    db, dash, _, _ = env

    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "sid-handover-test",
        "timestamp": "2026-05-23T10:00:00Z",
        "message": {"role": "user", "content": "hello"},
    }))

    write_calls = []

    def fake_write(conn, session_id):
        write_calls.append(session_id)
        return tmp_path / "handover.md"

    monkeypatch.setattr(hooks.handover_render, "write_handover", fake_write)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "sid-handover-test", "transcript_path": str(jl)})))

    rc = hooks.main(["session_end"])
    assert rc == 0
    assert write_calls == ["sid-handover-test"]


def test_handover_render_hook_fail_soft(env, monkeypatch, tmp_path):
    """If write_handover raises, session_end still returns 0 and adds an alert."""
    db, dash, _, _ = env

    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "sid-softerr",
        "timestamp": "2026-05-23T10:00:00Z",
        "message": {"role": "user", "content": "hello"},
    }))

    def boom(conn, session_id):
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr(hooks.handover_render, "write_handover", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "sid-softerr", "transcript_path": str(jl)})))

    rc = hooks.main(["session_end"])
    assert rc == 0

    conn = _conn(db)
    try:
        alerts = conn.execute(
            "SELECT message FROM alerts WHERE type='handover_render'"
        ).fetchall()
    finally:
        conn.close()
    assert alerts, "expected an alert for the render failure"


# ── _inject_reference_commits / _last_3_commits ──────────────────────────────

def test_inject_reference_commits_replaces_body():
    text = ("## Reference (last 3 commits)\n"
            "old body\n"
            "more old\n\n"
            "<!-- handover: pending -->\n")
    out = handover_render._inject_reference_commits(text, "- abc one\n- def two")
    assert "## Reference (last 3 commits)\n- abc one\n- def two\n\n" in out
    assert "old body" not in out
    assert "<!-- handover: pending -->" in out


def test_inject_reference_commits_noop_on_empty():
    text = "## Reference (last 3 commits)\nkeep me\n"
    assert handover_render._inject_reference_commits(text, "") == text


def test_last_3_commits_returns_bullets():
    out = handover_render._last_3_commits()
    # marrow repo has commits; expect bullet lines starting with "- "
    if out:
        for ln in out.splitlines():
            assert ln.startswith("- ")
