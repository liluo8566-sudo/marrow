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
                   source: str | None = None, created_at: str | None = None,
                   description: str | None = None,
                   unresolved: int = 0, resolved_at: str | None = None):
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label, "
        "description, source, created_at, unresolved, resolved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (date, ep, valence, arousal, importance, label, description, source,
         created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         unresolved, resolved_at),
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
    """v=0.7 a=0.7 → 兴奋 (High/Active). 中文【】brackets."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.7, 0.7, importance=3,
                   label="开心", description="项目过审")
    out = top_sections.render_affect(conn)
    conn.close()
    assert "【兴奋】" in out
    # English [兴奋] must NOT appear (spec change to 中文 brackets).
    assert "[兴奋]" not in out


def test_render_affect_today_band_low(env):
    """v=0.2 a=0.2 → 低落 (Low/Calm)."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.2, 0.2, importance=3,
                   label="难过", description="删笔记")
    out = top_sections.render_affect(conn)
    conn.close()
    assert "【低落】" in out


def test_render_affect_today_single_ep_dedup(env):
    """One today-ep → ep_h == ep_l, dedup to one side, no '· eplN' tail."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.5, 0.5, importance=2,
                   label="平静", description="散步")
    out = top_sections.render_affect(conn)
    conn.close()
    today_line = [
        ln for ln in out.splitlines()
        if ln.startswith("- 【")
    ][0]
    assert "eph2" in today_line
    assert "epl" not in today_line  # no second side
    assert "平静 | 散步" in today_line


def test_render_affect_today_multi_ep_phrase_format(env):
    """Multi-ep day → ephN <label> | <description> · eplN <label> | <description>."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.85, 0.6, importance=4,
                   label="雀跃", description="拿到 HD")
    _insert_affect(conn, today, 2, 0.15, 0.7, importance=3,
                   label="委屈", description="猪一样的队友")
    out = top_sections.render_affect(conn)
    conn.close()
    today_line = [
        ln for ln in out.splitlines() if ln.startswith("- 【")
    ][0]
    assert "eph4 雀跃 | 拿到 HD" in today_line
    assert "epl3 委屈 | 猪一样的队友" in today_line


def test_render_affect_week_variance_label(env):
    """stddev(v) > 0.3 → 主调A → 主调B in week line, 中文 brackets."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date()
    for i in range(6):
        d = (today - timedelta(days=i)).isoformat()
        v = 0.9 if i % 2 == 0 else 0.1
        _insert_affect(conn, d, 1, v, 0.5, importance=3,
                       label=("雀跃" if v > 0.5 else "低落"),
                       description=("高峰" if v > 0.5 else "低谷"))
    out = top_sections.render_affect(conn)
    conn.close()
    week_section = out.split("### This Week")[1].split("### Pending")[0]
    assert "→" in week_section
    assert "【" in week_section and "】" in week_section
    # 4 key eps formatted with eph/epl prefix and description.
    assert week_section.count("eph") + week_section.count("epl") >= 4
    assert "高峰" in week_section or "低谷" in week_section


def test_render_affect_pending_unresolved_and_resolved(env):
    """Pending: open row → '- [ ] <desc>'; resolved row → '- [x] <desc>'."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    # Unresolved still open.
    _insert_affect(conn, today, 1, 0.3, 0.8, importance=3,
                   label="焦虑", description="演讲前夜",
                   unresolved=1, resolved_at=None)
    # Previously unresolved but resolved within the week.
    resolved_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_affect(conn, yesterday, 1, 0.2, 0.7, importance=4,
                   label="争执", description="吵架",
                   unresolved=1, resolved_at=resolved_ts)
    out = top_sections.render_affect(conn)
    conn.close()
    pending_section = out.split("### Pending")[1]
    assert "- [ ] 演讲前夜" in pending_section
    assert "- [x] 吵架" in pending_section
    assert "- (none)" not in pending_section


def test_render_affect_pending_empty_when_no_unresolved(env):
    """No unresolved rows → '- (none)'."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.5, 0.5, importance=2,
                   label="平静", description="散步")
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

def test_handover_write_full_atomic_and_ready_stamp(env):
    """write_handover_full produces file at _RENDERED_PATH with ready stamp + 4 sections + bullets."""
    db, _, _, rendered_path = env
    conn = _conn(db)
    result_path = handover_render.write_handover_full(
        conn, "abc123", "- did X\n- did Y", "- pick up Z")
    conn.close()

    assert result_path == rendered_path
    assert rendered_path.exists()
    content = rendered_path.read_text(encoding="utf-8")

    # Ready stamp present (not pending) — single-writer atomic flow
    assert f"<!-- handover: ready sid:abc123 ts:" in content
    assert "pending sid:" not in content
    # All 4 section headers present
    assert "## Alerts (active)" in content
    assert "## Tasks" in content
    assert "## Milestone candidate" in content
    assert "## Affect" in content
    # LLM bullets injected
    assert "- did X" in content
    assert "- pick up Z" in content


def test_handover_write_full_strips_instruction_lines(env):
    """Lines starting with '> ' must not appear in the rendered output."""
    db, _, _, rendered_path = env
    conn = _conn(db)
    handover_render.write_handover_full(conn, "s1", "- a", "- b")
    conn.close()
    content = rendered_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        assert not line.startswith("> "), f"Instruction line leaked: {line!r}"


def test_handover_write_full_atomic_via_replace(env, monkeypatch):
    """Atomic write uses os.replace (rename). Verify via monkeypatch."""
    db, _, _, rendered_path = env
    replace_calls = []
    real_replace = os.replace

    def tracking_replace(src, dst):
        replace_calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", tracking_replace)
    conn = _conn(db)
    handover_render.write_handover_full(conn, "s-atomic", "- a", "- b")
    conn.close()

    assert len(replace_calls) >= 1
    assert any(str(rendered_path) in str(dst) for _, dst in replace_calls)


def test_handover_timestamp_replaced(env):
    """{{YYYY-MM-DD HH:MM}} placeholder is replaced with current time."""
    db, _, _, rendered_path = env
    conn = _conn(db)
    handover_render.write_handover_full(conn, "s-ts", "- a", "- b")
    conn.close()
    content = rendered_path.read_text(encoding="utf-8")
    assert "{{YYYY-MM-DD HH:MM}}" not in content
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", content)


def test_handover_full_single_write_populates_both_sections(env):
    """Acceptance test for Bug #1: one atomic write contains skeleton + both
    ThisSession + NextSession + ready stamp — no pending stamp ever appears.
    """
    db, _, _, rendered_path = env
    conn = _conn(db)
    handover_render.write_handover_full(
        conn, "sid-acceptance",
        this_session="- shipped Bug #1 fix",
        next_session="- verify pytest + ship merge",
    )
    conn.close()
    content = rendered_path.read_text(encoding="utf-8")
    # Phase A: ThisSession now wraps each segment in `### [ts]` sub-heading.
    this_section = content.split("## This Session")[1].split("## Next Session")[0]
    assert "- shipped Bug #1 fix" in this_section
    assert "### [" in this_section
    assert "## Next Session\n- verify pytest + ship merge" in content
    assert "<!-- handover: ready sid:sid-acceptance ts:" in content
    assert "pending sid:" not in content


def test_session_start_is_readonly_for_handover(env, monkeypatch, tmp_path):
    """SessionStart hook must NEVER mutate handover.md — file mtime unchanged."""
    import io, json as _json
    db, _, _, rendered_path = env
    conn = _conn(db)
    handover_render.write_handover_full(
        conn, "pre-existing", "- prior session content", "- next plan")
    conn.close()
    before_bytes = rendered_path.read_bytes()
    before_mtime = rendered_path.stat().st_mtime_ns

    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps({})))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    rc = hooks.main(["session_start"])
    assert rc == 0

    # File untouched.
    assert rendered_path.read_bytes() == before_bytes
    assert rendered_path.stat().st_mtime_ns == before_mtime


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

def test_session_end_does_not_write_handover(env, monkeypatch, tmp_path):
    """Bug #1 fix: session_end MUST NOT touch handover.md — sessionend_async
    (spawned detached) is now the single writer.
    """
    db, dash, _, rendered_path = env

    # Pre-seed handover.md with content the hook must leave alone.
    rendered_path.write_text("PRE-EXISTING CONTENT", encoding="utf-8")
    before = rendered_path.read_bytes()

    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "sid-end-noop",
        "timestamp": "2026-05-23T10:00:00Z",
        "message": {"role": "user", "content": "hello"},
    }))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "sid-end-noop", "transcript_path": str(jl)})))

    rc = hooks.main(["session_end"])
    assert rc == 0
    assert rendered_path.read_bytes() == before


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


# ── Phase A: multi-session merge ─────────────────────────────────────────────

def _write_via(env, sid: str, this_s: str, next_s: str, now_epoch: int):
    db, _, _, rendered_path = env
    import time as _time
    real_time = _time.time
    try:
        _time.time = lambda: now_epoch  # type: ignore[assignment]
        conn = _conn(db)
        handover_render.write_handover_full(conn, sid, this_s, next_s)
        conn.close()
    finally:
        _time.time = real_time  # type: ignore[assignment]
    return rendered_path.read_text(encoding="utf-8")


def test_phase_a_two_sessions_within_window(env):
    """Two sessions 30min apart → both segments live under ## This Session."""
    base = int(datetime(2026, 5, 24, 10, 0).timestamp())
    _write_via(env, "s1", "- did A", "- next A", base)
    content = _write_via(env, "s2", "- did B", "- next B", base + 30 * 60)

    this_section = content.split("## This Session")[1].split("## Next Session")[0]
    prev_section = content.split("## Previous Sessions")[1].split("## This Session")[0]
    # Newest on top
    assert this_section.index("- did B") < this_section.index("- did A")
    # Both have time sub-headings
    assert "### [2026-05-24 10:30]" in this_section
    assert "### [2026-05-24 10:00]" in this_section
    # Previous Sessions still empty
    assert "- None" in prev_section


def test_phase_a_two_sessions_outside_window(env):
    """Two sessions 4h apart → old ThisSession pushed to Previous Sessions."""
    base = int(datetime(2026, 5, 24, 6, 0).timestamp())
    _write_via(env, "s1", "- old work", "- old plan", base)
    content = _write_via(env, "s2", "- new work", "- new plan", base + 4 * 3600)

    this_section = content.split("## This Session")[1].split("## Next Session")[0]
    prev_section = content.split("## Previous Sessions")[1].split("## This Session")[0]

    assert "- new work" in this_section
    assert "- old work" not in this_section
    assert "- old work" in prev_section
    assert "### [2026-05-24 06:00]" in prev_section
    assert "### [2026-05-24 10:00]" in this_section


def test_phase_a_three_sessions_mix(env):
    """3 sessions: 4h ago, 30min ago, now → previous=[4h], this=[now, 30min]."""
    base = int(datetime(2026, 5, 24, 6, 0).timestamp())
    _write_via(env, "s1", "- four hr ago", "- np1", base)
    _write_via(env, "s2", "- thirty min ago", "- np2", base + 3 * 3600 + 30 * 60)
    content = _write_via(env, "s3", "- right now", "- np3", base + 4 * 3600)

    this_section = content.split("## This Session")[1].split("## Next Session")[0]
    prev_section = content.split("## Previous Sessions")[1].split("## This Session")[0]
    next_section = content.split("## Next Session")[1].split("## Reference")[0]

    assert "- four hr ago" in prev_section
    assert "- thirty min ago" in this_section
    assert "- right now" in this_section
    assert this_section.index("- right now") < this_section.index("- thirty min ago")
    # NextSession union — all three preserved
    for marker in ("- np1", "- np2", "- np3"):
        assert marker in next_section


def test_phase_a_next_session_union_dedup(env):
    base = int(datetime(2026, 5, 24, 10, 0).timestamp())
    _write_via(env, "s1", "- a", "- shared\n- only-old", base)
    content = _write_via(env, "s2", "- b", "- shared\n- only-new", base + 30 * 60)
    next_section = content.split("## Next Session")[1].split("## Reference")[0]
    # `- shared` appears exactly once
    assert next_section.count("- shared") == 1
    assert "- only-old" in next_section
    assert "- only-new" in next_section
    # New on top
    assert next_section.index("- only-new") < next_section.index("- only-old")


def test_phase_a_empty_inputs_render_none(env):
    base = int(datetime(2026, 5, 24, 10, 0).timestamp())
    content = _write_via(env, "s1", "", "N/A", base)
    this_section = content.split("## This Session")[1].split("## Next Session")[0]
    next_section = content.split("## Next Session")[1].split("## Reference")[0]
    prev_section = content.split("## Previous Sessions")[1].split("## This Session")[0]
    assert "- None" in this_section
    assert "- None" in next_section
    assert "- None" in prev_section


def test_phase_a_snapshot_audit_row_written(env):
    db, _, _, rendered_path = env
    base = int(datetime(2026, 5, 24, 10, 0).timestamp())
    _write_via(env, "s1", "- first body", "- next1", base)
    _write_via(env, "s2", "- second body", "- next2", base + 30 * 60)

    conn = _conn(db)
    rows = conn.execute(
        "SELECT target_id, summary FROM audit_log"
        " WHERE action='handover_snapshot' ORDER BY id"
    ).fetchall()
    conn.close()
    # Second write should have captured the first write as snapshot.
    assert len(rows) >= 1
    assert any("- first body" in r["summary"] for r in rows)
    assert any("sha256=" in r["summary"] for r in rows)


def test_phase_a_flock_retry_then_partial(env, monkeypatch):
    """flock contention → 3x retry then partial file + audit row, no crash."""
    db, _, _, rendered_path = env
    # Pre-seed file so flock target exists.
    rendered_path.parent.mkdir(parents=True, exist_ok=True)
    rendered_path.write_text("seed", encoding="utf-8")

    calls = {"n": 0}
    real_flock = handover_render.fcntl.flock

    def flaky_flock(fd, op):
        if op & handover_render.fcntl.LOCK_EX:
            calls["n"] += 1
            raise BlockingIOError("locked")
        return real_flock(fd, op)

    monkeypatch.setattr(handover_render.fcntl, "flock", flaky_flock)
    monkeypatch.setattr(handover_render.time, "sleep", lambda s: None)

    conn = _conn(db)
    result = handover_render.write_handover_full(
        conn, "sid-lock", "- new", "- next")
    # Audit row for lock failure exists.
    rows = conn.execute(
        "SELECT summary FROM audit_log WHERE action='handover_lock_failed'"
    ).fetchall()
    conn.close()
    assert calls["n"] >= 3
    assert "partial" in result.name
    assert result.exists()
    assert len(rows) == 1


def test_phase_a_legacy_format_treated_as_single_segment(env):
    """Old handover.md without ### sub-headings folds into one segment using footer ts."""
    db, _, _, rendered_path = env
    base = int(datetime(2026, 5, 24, 10, 0).timestamp())
    legacy = (
        "# Marrow handover — 2026-05-24 09:00\n\n"
        "## Previous Sessions\n- None\n\n"
        "## This Session\n- legacy bullet without timestamp\n\n"
        "## Next Session\n- legacy next\n\n"
        "## Reference (last 3 commits)\n\n"
        f"<!-- handover: ready sid:old ts:{base - 30 * 60} -->\n"
    )
    rendered_path.parent.mkdir(parents=True, exist_ok=True)
    rendered_path.write_text(legacy, encoding="utf-8")

    content = _write_via(env, "s-new", "- new bullet", "- new next", base)
    this_section = content.split("## This Session")[1].split("## Next Session")[0]
    # Both legacy + new live in ThisSession (legacy was 30min ago, within window).
    assert "- new bullet" in this_section
    assert "- legacy bullet without timestamp" in this_section
    assert "### [2026-05-24 09:30]" in this_section  # footer ts label
    assert "### [2026-05-24 10:00]" in this_section
