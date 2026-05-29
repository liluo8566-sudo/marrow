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
    sub_folder = str(tmp_path / "db-pages")
    sub_state = str(tmp_path / "db_state")
    rendered_handover = tmp_path / "handover.md"
    conn = storage.init_db(db)
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "dashboard_path", lambda: dash)
    monkeypatch.setattr(config, "db_pages_path", lambda: sub_folder)
    monkeypatch.setattr(config, "db_pages_state_path", lambda: sub_state)
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
    assert "_none_" in out


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
    assert "_none_" in out


# ── Unit: render_affect ───────────────────────────────────────────────────────

def test_render_affect_today_band_excited(env):
    """v=0.7 a=0.7 → 兴奋 (High/Active). 中文【】brackets.

    Line 1 prefers row-supplied label; the 9-tone fallback `兴奋` surfaces
    only when label is null. Either form must appear in 中文 brackets.
    """
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.7, 0.7, importance=3,
                   label=None, description="项目过审")
    out = top_sections.render_affect(conn)
    conn.close()
    assert "【兴奋】" in out
    # English [兴奋] must NOT appear (spec change to 中文 brackets).
    assert "[兴奋]" not in out


def test_render_affect_today_band_low(env):
    """v=0.2 a=0.2 → 低落 (Low/Calm) when no label provided."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.2, 0.2, importance=3,
                   label=None, description="删笔记")
    out = top_sections.render_affect(conn)
    conn.close()
    assert "【低落】" in out


def test_render_affect_today_single_ep_dedup(env):
    """One today-ep → ep_h == ep_l, only the eph segment, no epl on the line."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.5, 0.5, importance=2,
                   label="平静", description="散步")
    out = top_sections.render_affect(conn)
    conn.close()
    today_block = out.split("### Today")[1].split("###")[0]
    assert "eph2 平静 | 散步" in today_block
    # No second-side segment for dedup case.
    assert "epl" not in today_block
    # Bullet body stays anchor-free; trail marker on the next line carries
    # the id so reconcile_affect can pair the segment back to the DB row.
    assert "<!-- id:affect." not in today_block
    assert "<!-- aff:" in today_block


def test_render_affect_today_multi_ep_phrase_format(env):
    """Multi-ep day → eph and epl segments inline on one bullet, each anchored."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.85, 0.6, importance=4,
                   label="雀跃", description="拿到 HD")
    _insert_affect(conn, today, 2, 0.15, 0.7, importance=3,
                   label="委屈", description="猪一样的队友")
    out = top_sections.render_affect(conn)
    conn.close()
    today_block = out.split("### Today")[1].split("###")[0]
    assert "eph4 雀跃 | 拿到 HD" in today_block
    assert "epl3 委屈 | 猪一样的队友" in today_block
    # Bullet body stays anchor-free; trail marker on the next line carries
    # both ids so reconcile can pair each segment back to its DB row.
    assert "<!-- id:affect." not in today_block
    # Trail marker covers both segments.
    import re as _re
    trail = _re.search(r"<!--\s*aff:([0-9,\s]*)-->", today_block)
    assert trail, "expected aff: trail marker below bullet"
    ids = [t.strip() for t in trail.group(1).split(",") if t.strip()]
    assert len(ids) >= 2, f"trail must list both ep ids, got {ids}"
    # eph + epl share the same bullet line (inline format, not sub-bullets).
    bullet = [ln for ln in today_block.splitlines()
              if "eph4" in ln and "epl3" in ln]
    assert bullet, "eph + epl must render on the same bullet line"
    assert " · " in bullet[0], "ep segments joined by ` · `"


def test_render_affect_week_variance_label(env):
    """stddev(v) > 0.3 → 主调A → 主调B in week line, 中文 brackets.

    With 3-line dedup: Line 1 (last batch, shared created_at) + Line 2 (24h)
    absorb 4 ids; Line 3 (week) shows the remaining 2. Tone arrow + bracketed
    tone still surface, plus description text.
    """
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date()
    base = datetime.now(timezone.utc)
    for i in range(6):
        d = (today - timedelta(days=i)).isoformat()
        # Spread created_at across multi-day window so Line 1 (latest batch)
        # only captures the most recent insert; remaining rows fan into the
        # 7d window for the Week aggregate.
        ts = (base - timedelta(days=i, hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        v = 0.9 if i % 2 == 0 else 0.1
        _insert_affect(conn, d, 1, v, 0.5, importance=3,
                       label=("雀跃" if v > 0.5 else "低落"),
                       description=("高峰" if v > 0.5 else "低谷"),
                       created_at=ts)
    out = top_sections.render_affect(conn)
    conn.close()
    week_section = out.split("### This Week")[1].split("### Pending")[0]
    assert "→" in week_section
    assert "【" in week_section and "】" in week_section
    # At least one outlier ep makes it through to Line 3 after dedup.
    assert week_section.count("eph") + week_section.count("epl") >= 1
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
    """No unresolved rows → Pending sub-section hides entirely."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.5, 0.5, importance=2,
                   label="平静", description="散步")
    out = top_sections.render_affect(conn)
    conn.close()
    assert "### Pending" not in out


def test_render_affect_empty_tables(env):
    """No affect rows → Today/This Week show _none_, Pending hides."""
    db, _, _, _ = env
    conn = _conn(db)
    out = top_sections.render_affect(conn)
    conn.close()
    assert "### Today" in out
    assert "### This Week" in out
    assert "### Pending" not in out
    assert "_none_" in out


# ── render_skeleton / template helpers (3-section handover) ──────────────────

def test_render_skeleton_has_three_sections_and_no_instruction_lines(env):
    db, _, _, _ = env
    conn = _conn(db)
    out = handover_render.render_skeleton(conn)
    conn.close()
    assert "## Done" in out
    assert "## Doing" in out
    assert "## Lumi's Note" in out
    # No legacy 4-section headers.
    assert "## Open" not in out
    assert "## Plan" not in out
    assert "## Reference" not in out
    # `> ` instruction lines stripped.
    for line in out.splitlines():
        assert not line.startswith("> "), f"instruction line leaked: {line!r}"


def test_render_skeleton_timestamp_replaced(env):
    db, _, _, _ = env
    conn = _conn(db)
    out = handover_render.render_skeleton(conn)
    conn.close()
    assert "{{YYYY-MM-DD HH:MM}}" not in out
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", out)


def test_strip_instruction_preserves_trailing_newline():
    src = "## A\n> hide\n> hide2\n"
    out = handover_render._strip_instruction_lines(src)
    assert out.endswith("\n"), (
        "trailing \\n must survive so regex inject sites still match")


# ── seg_handover atomic write via apply_diff ────────────────────────────────

def _apply(env, sid: str, raw: str):
    """Run seg_handover (diff-apply) against the env's redirected file."""
    from marrow.sessionend_writers import seg_handover
    db, _, _, _ = env
    conn = _conn(db)
    try:
        return seg_handover(conn, raw, sid)
    finally:
        conn.close()


def _add_raw(scope: str, title: str, current: str = "s", nxt: str = "n"):
    return (
        "===DOING_DIFF===\nADD:\n"
        f"[{scope}] - {title}\n"
        f"  - Current: {current}\n"
        f"  - Next: {nxt}\n"
        "  - Reference: N/A\n"
        "===END===\n"
        "===NOTE_DONE===\nN/A\n===END===\n")


def test_apply_diff_atomic_and_ready_stamp(env):
    """First diff-apply seeds the file with ready stamp + 3 sections."""
    db, _, _, rendered_path = env
    _apply(env, "abc123", _add_raw("Marrow", "did X"))
    assert rendered_path.exists()
    content = rendered_path.read_text(encoding="utf-8")
    assert "<!-- handover: ready sid:abc123 ts:" in content
    assert "pending sid:" not in content
    assert "## Done" in content and "## Doing" in content
    assert "## Lumi's Note" in content
    assert "did X" in content
    assert "<!-- id:1 -->" in content


def test_apply_diff_uses_os_replace(env, monkeypatch):
    """Atomic write goes through os.replace (rename)."""
    db, _, _, rendered_path = env
    replace_calls = []
    real_replace = os.replace

    def tracking_replace(src, dst):
        replace_calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", tracking_replace)
    _apply(env, "s-atomic", _add_raw("Marrow", "thing"))
    assert len(replace_calls) >= 1
    assert any(str(rendered_path) in str(dst) for _, dst in replace_calls)


def test_snapshot_audit_row_written_each_apply(env):
    """Each diff-apply writes a handover_snapshot row (next-write diff baseline)
    plus a handover_overwritten row when the body changed."""
    db, _, _, _ = env
    _apply(env, "s1", _add_raw("Marrow", "first body"))
    _apply(env, "s2", _add_raw("Study", "second body"))
    conn = _conn(db)
    snapshots = conn.execute(
        "SELECT summary FROM audit_log WHERE action='handover_snapshot'"
        " ORDER BY id").fetchall()
    overwritten = conn.execute(
        "SELECT summary FROM audit_log WHERE action='handover_overwritten'"
        " ORDER BY id").fetchall()
    conn.close()
    assert len(snapshots) == 2
    assert "second body" in snapshots[-1]["summary"]
    assert "sha256=" in snapshots[0]["summary"]
    # The second apply overwrote the first body.
    assert len(overwritten) >= 1
    assert any("first body" in o["summary"] for o in overwritten)


def test_apply_diff_flock_retry_then_partial(env, monkeypatch):
    """flock contention → retry then partial file + audit row, no crash."""
    db, _, _, rendered_path = env
    rendered_path.parent.mkdir(parents=True, exist_ok=True)
    rendered_path.write_text("## Done\n- N/A\n\n## Doing\n- N/A\n\n"
                             "## Lumi's Note\n- N/A\n", encoding="utf-8")

    calls = {"n": 0}
    real_flock = handover_render.fcntl.flock

    def flaky_flock(fd, op):
        if op & handover_render.fcntl.LOCK_EX:
            calls["n"] += 1
            raise BlockingIOError("locked")
        return real_flock(fd, op)

    monkeypatch.setattr(handover_render.fcntl, "flock", flaky_flock)
    monkeypatch.setattr(handover_render.time, "sleep", lambda s: None)

    from marrow import handover_diff
    conn = _conn(db)
    result = handover_diff.apply_diff(
        conn, "sid-lock",
        {"close": [], "keep": [], "update": [],
         "add": ["[Marrow] - new\n  - Current: s\n  - Next: n"
                 "\n  - Reference: N/A"]}, [])
    rows = conn.execute(
        "SELECT summary FROM audit_log WHERE action='handover_lock_failed'"
    ).fetchall()
    conn.close()
    assert calls["n"] >= 3
    assert "partial" in result.name
    assert result.exists()
    assert len(rows) == 1


# ── dashboard swap (independent of handover) ────────────────────────────────

def test_dashboard_top_now_uses_4_sections(env):
    """render_top from dashboard should use top_sections and contain all 4 headers."""
    from marrow import dashboard
    db, dash, _, _ = env
    conn = _conn(db)
    block = dashboard.render_top(conn)
    conn.close()
    assert "## Alerts" in block
    assert "## Tasks" in block
    assert "## Milestone candidate" in block
    assert "## Affect" in block
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
    assert "## Alerts" in content


# ── hook wiring: handover is read-only at session_start / session_end ───────

def test_session_start_is_readonly_for_handover(env, monkeypatch, tmp_path):
    """SessionStart hook must NEVER mutate handover.md — file mtime unchanged."""
    import io as _io
    import json as _json
    db, _, _, rendered_path = env
    _apply(env, "pre-existing", _add_raw("Marrow", "prior session content"))
    before_bytes = rendered_path.read_bytes()
    before_mtime = rendered_path.stat().st_mtime_ns

    monkeypatch.setattr("sys.stdin", _io.StringIO(_json.dumps({})))
    monkeypatch.setattr("sys.stdout", _io.StringIO())
    rc = hooks.main(["session_start"])
    assert rc == 0
    assert rendered_path.read_bytes() == before_bytes
    assert rendered_path.stat().st_mtime_ns == before_mtime


def test_session_end_does_not_write_handover(env, monkeypatch, tmp_path):
    """Bug #1 fix: session_end MUST NOT touch handover.md — sessionend_async
    (spawned detached) is now the single writer."""
    db, dash, _, rendered_path = env
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

