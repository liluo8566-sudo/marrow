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
    """One today-ep → ep_h == ep_l, only the eph sub-bullet, no epl line."""
    db, _, _, _ = env
    conn = _conn(db)
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, today, 1, 0.5, 0.5, importance=2,
                   label="平静", description="散步")
    out = top_sections.render_affect(conn)
    conn.close()
    today_block = out.split("### Today")[1].split("###")[0]
    assert "eph2 平静 | 散步" in today_block
    # No second-side sub-bullet for dedup case.
    assert "epl" not in today_block
    # Anchor per ep — reconcile_affect relies on `<!-- id:affect.N -->`.
    assert "<!-- id:affect." in today_block


def test_render_affect_today_multi_ep_phrase_format(env):
    """Multi-ep day → eph sub-bullet then epl sub-bullet, each anchored."""
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
    # Both eps anchored so reconcile can absorb edits on either side.
    assert today_block.count("<!-- id:affect.") >= 2


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


# ── Unit: handover_render (Plan H — 4 state-axis sections) ──────────────────

def _write_full(env, sid: str, *, done: str = "- N/A", open_: str = "- N/A",
                plan: str = "- N/A", reference: str = "- N/A"):
    db, _, _, _ = env
    conn = _conn(db)
    try:
        return handover_render.write_handover_full(
            conn, sid, done=done, open_=open_, plan=plan, reference=reference)
    finally:
        conn.close()


def test_handover_write_full_atomic_and_ready_stamp(env):
    """write_handover_full produces file at _RENDERED_PATH with ready stamp +
    4 state-axis sections. Top-section block is stripped from handover."""
    db, _, _, rendered_path = env
    result_path = _write_full(env, "abc123",
                              done="- did X\n- did Y",
                              plan="- pick up Z")
    assert result_path == rendered_path
    assert rendered_path.exists()
    content = rendered_path.read_text(encoding="utf-8")

    assert "<!-- handover: ready sid:abc123 ts:" in content
    assert "pending sid:" not in content
    # Top section markers stripped
    assert "<!-- marrow:top:start -->" not in content
    assert "<!-- marrow:top:end -->" not in content
    assert "## Alerts (active)" not in content
    # 4 state-axis sections present
    assert "## Done" in content
    assert "## Open" in content
    assert "## Plan" in content
    assert "## Reference" in content
    # No legacy time-axis headers
    assert "## Previous Sessions" not in content
    assert "## This Session" not in content
    assert "## Next Session" not in content
    # LLM bullets injected
    assert "- did X" in content
    assert "- pick up Z" in content


def test_handover_write_full_strips_instruction_lines(env):
    """Lines starting with '> ' must not appear in the rendered output."""
    db, _, _, rendered_path = env
    _write_full(env, "s1", done="- a", plan="- b")
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
    _write_full(env, "s-atomic", done="- a", plan="- b")

    assert len(replace_calls) >= 1
    assert any(str(rendered_path) in str(dst) for _, dst in replace_calls)


def test_handover_timestamp_replaced(env):
    """{{YYYY-MM-DD HH:MM}} placeholder is replaced with current time."""
    db, _, _, rendered_path = env
    _write_full(env, "s-ts", done="- a", plan="- b")
    content = rendered_path.read_text(encoding="utf-8")
    assert "{{YYYY-MM-DD HH:MM}}" not in content
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", content)


def test_session_start_is_readonly_for_handover(env, monkeypatch, tmp_path):
    """SessionStart hook must NEVER mutate handover.md — file mtime unchanged."""
    import io, json as _json
    db, _, _, rendered_path = env
    _write_full(env, "pre-existing", done="- prior session content",
                plan="- next plan")
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


# ── render_full + Reference ─────────────────────────────────────────────────

def test_render_full_injects_reference_body(env):
    db, _, _, _ = env
    conn = storage.connect(db)
    out = handover_render.render_full(
        conn, sid="s1",
        done="- did X", open_="- N/A", plan="- pick up Y",
        reference="- `marrow/foo.py:42` — entry point\n- skill: tdd",
        now_epoch=1700000000,
    )
    assert "## Reference\n" in out
    assert "`marrow/foo.py:42` — entry point" in out
    assert "skill: tdd" in out


def test_render_full_reference_defaults_to_na(env):
    db, _, _, _ = env
    conn = storage.connect(db)
    out = handover_render.render_full(
        conn, sid="s2",
        done="- did X", open_="- N/A", plan="- pick up Y",
        reference="",
        now_epoch=1700000000,
    )
    assert "## Reference\n- N/A" in out


def test_strip_instruction_preserves_trailing_newline():
    src = "## A\n> hide\n> hide2\n"
    out = handover_render._strip_instruction_lines(src)
    assert out.endswith("\n"), "trailing \\n must survive so regex inject sites still match"


# ── Plan H: tombstone + snapshot + flock + structural invariants ────────────

def test_snapshot_audit_row_written_each_write(env):
    """Each write captures the body it is about to atomic_write as a
    handover_snapshot row — that becomes the diff baseline for the next write.
    A second `handover_overwritten` row captures the pre-overwrite text."""
    db, _, _, _ = env
    _write_full(env, "s1", done="- first body", plan="- next1")
    _write_full(env, "s2", done="- second body", plan="- next2")

    conn = _conn(db)
    snapshots = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='handover_snapshot' ORDER BY id"
    ).fetchall()
    overwritten = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='handover_overwritten' ORDER BY id"
    ).fetchall()
    conn.close()
    # Two snapshot rows — one per write.
    assert len(snapshots) == 2
    # Latest snapshot reflects the most recent write.
    assert "- second body" in snapshots[-1]["summary"]
    # Overwritten row captured the pre-overwrite body (from s1).
    assert len(overwritten) == 1
    assert "- first body" in overwritten[0]["summary"]
    assert "sha256=" in snapshots[0]["summary"]


def test_flock_retry_then_partial(env, monkeypatch):
    """flock contention → 3x retry then partial file + audit row, no crash."""
    db, _, _, rendered_path = env
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
        conn, "sid-lock", done="- new", open_="- N/A", plan="- next",
        reference="- N/A")
    rows = conn.execute(
        "SELECT summary FROM audit_log WHERE action='handover_lock_failed'"
    ).fetchall()
    conn.close()
    assert calls["n"] >= 3
    assert "partial" in result.name
    assert result.exists()
    assert len(rows) == 1


def test_user_deleted_bullet_tombstoned_and_filtered_on_next_render(env):
    """End-to-end Lumi-edit survival:
    1. Auto-write places `- decision X` and `- decision Y` in Done.
    2. Lumi edits handover.md and removes `- decision Y` by hand.
    3. Next auto-write re-emits `- decision Y` in Done.
    Tombstone records the diff and the next render drops it again."""
    db, _, _, rendered_path = env

    # Step 1: initial auto-write.
    _write_full(env, "s1",
                done="- decision X\n- decision Y",
                plan="- pick up Z")
    body_1 = rendered_path.read_text(encoding="utf-8")
    assert "- decision Y" in body_1

    # Step 2: Lumi removes "- decision Y" from the rendered file directly.
    edited = body_1.replace("\n- decision Y", "")
    rendered_path.write_text(edited, encoding="utf-8")

    # Step 3: sonnet re-emits both bullets — the next auto-write must filter Y.
    _write_full(env, "s2",
                done="- decision X\n- decision Y",
                plan="- pick up Z")
    body_2 = rendered_path.read_text(encoding="utf-8")
    done_section = body_2.split("## Done")[1].split("## Open")[0]
    assert "- decision X" in done_section
    assert "- decision Y" not in done_section, (
        "Tombstone-filter regression: Lumi-deleted bullet revived by sonnet.")


def test_tombstone_rows_persist_in_md_index(env):
    """MdIndexTombstoneStore writes through to md_index, keyed on handover path."""
    db, _, _, rendered_path = env

    _write_full(env, "s1", done="- keep\n- drop me")
    body_1 = rendered_path.read_text(encoding="utf-8")
    rendered_path.write_text(body_1.replace("\n- drop me", ""),
                             encoding="utf-8")
    _write_full(env, "s2", done="- keep\n- drop me")

    conn = _conn(db)
    rows = conn.execute(
        "SELECT block_id, content_hash FROM md_index"
        " WHERE path=? AND tombstone_at IS NOT NULL",
        (str(rendered_path),),
    ).fetchall()
    conn.close()
    assert len(rows) >= 1
    # content_hash carries the bullet summary (truncated to 200 chars).
    assert any("drop me" in (r["content_hash"] or "") for r in rows)


def test_empty_inputs_render_na_in_all_sections(env):
    """No content for any section → `- N/A` in all four blocks."""
    db, _, _, rendered_path = env
    _write_full(env, "s-empty")
    content = rendered_path.read_text(encoding="utf-8")
    for header in ("## Done", "## Open", "## Plan", "## Reference"):
        section = content.split(header)[1].split("##")[0]
        assert "- N/A" in section, f"{header} missing N/A placeholder"


# ── wt-md-f: MdIndex-backed tombstone adapter ───────────────────────────────

def test_new_store_is_md_index_backed(env):
    """_new_store() returns an MdIndex-backed adapter; tombstones land in
    md_index table, not audit_log."""
    db, _, _, rendered_path = env
    conn = _conn(db)
    store = handover_render._new_store(conn)
    h = "deadbeef" * 5
    store.tombstone(h, summary="some bullet")
    listed = store.list_tombstones()
    # Ensure tombstone surfaces via the new store.
    assert h in listed
    # Confirm row landed in md_index, not audit_log.
    rows = conn.execute(
        "SELECT COUNT(*) FROM md_index WHERE path=? AND block_id=?"
        " AND tombstone_at IS NOT NULL",
        (str(rendered_path), h),
    ).fetchone()
    conn.close()
    assert rows[0] == 1


def test_new_store_clear_tombstone_via_md_index(env):
    db, _, _, rendered_path = env
    conn = _conn(db)
    store = handover_render._new_store(conn)
    h1, h2 = "aaa" * 10, "bbb" * 10
    store.tombstone(h1, summary="a")
    store.tombstone(h2, summary="b")
    assert {h1, h2}.issubset(store.list_tombstones())
    store.clear_tombstone(h1)
    listed = store.list_tombstones()
    conn.close()
    assert h1 not in listed
    assert h2 in listed


def test_new_store_record_and_get_hash_use_md_index(env):
    """record_block / get_hash flow through MdIndex; baseline survives."""
    db, _, _, rendered_path = env
    conn = _conn(db)
    store = handover_render._new_store(conn)
    bid = "blk-handover-1"
    store.record_block(bid, "hash-v1")
    assert store.get_hash(bid) == "hash-v1"
    store.record_block(bid, "hash-v2")
    assert store.get_hash(bid) == "hash-v2"
    conn.close()


def test_user_deleted_bullet_uses_md_index_table(env):
    """End-to-end Lumi-edit survival flow: tombstone row lands in md_index,
    bullet stays filtered on next auto-write."""
    db, _, _, rendered_path = env

    _write_full(env, "s1",
                done="- decision X\n- decision Y",
                plan="- pick up Z")
    body_1 = rendered_path.read_text(encoding="utf-8")
    edited = body_1.replace("\n- decision Y", "")
    rendered_path.write_text(edited, encoding="utf-8")
    _write_full(env, "s2",
                done="- decision X\n- decision Y",
                plan="- pick up Z")
    body_2 = rendered_path.read_text(encoding="utf-8")
    done_section = body_2.split("## Done")[1].split("## Open")[0]
    assert "- decision Y" not in done_section

    conn = _conn(db)
    rows = conn.execute(
        "SELECT block_id FROM md_index WHERE path=?"
        " AND tombstone_at IS NOT NULL",
        (str(rendered_path),),
    ).fetchall()
    conn.close()
    assert rows, "tombstone row did not land in md_index for handover path"
