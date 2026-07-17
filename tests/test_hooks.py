"""Integration tests for marrow/hooks.py — thin CC hook entrypoints.

Hooks read paths from config; tests point config at a tmp db/dashboard via
monkeypatch and drive main() with stdin JSON like CC does.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from marrow import config, hooks, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    dash = str(tmp_path / "dashboard.md")
    sub_folder = str(tmp_path / "db-pages")
    sub_state = str(tmp_path / "db_state")
    conn = storage.init_db(db)
    conn.execute("INSERT INTO tasks(category,title,status) "
                 "VALUES('study','GAMSAT plan','active')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "dashboard_path", lambda: dash)
    monkeypatch.setattr(config, "db_pages_path", lambda: sub_folder)
    monkeypatch.setattr(config, "db_pages_state_path", lambda: sub_state)
    # Legacy aliases kept synced so any caller still hitting the old name
    # (uncommitted other-window edits in daily.py) sees the same tmp paths.
    monkeypatch.setattr(config, "sub_pages_path", lambda: sub_folder)
    monkeypatch.setattr(config, "sub_pages_state_path", lambda: sub_state)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db, dash, tmp_path


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def test_session_start_emits_additional_context(env, monkeypatch, capsys):
    _stdin(monkeypatch, {"session_id": "s1"})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert isinstance(ctx, str)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_session_end_does_not_write_db_pages(env, monkeypatch, tmp_path):
    """SessionEnd MUST NOT touch db-pages — those are owned by daily.py.
    Re-rendering milestone.md every session was the root cause of the
    `Milestone candidate` regrow-after-delete bug (pinned=0 leak into the
    subpage). Dashboard top is still rewritten; db-pages folder is left
    untouched until the next 07:00 daily routine."""
    db, _, _ = env
    conn = storage.connect(db)
    conn.execute("INSERT INTO milestones(scope,date,title,pinned) "
                 "VALUES('me','2026-01-17','Stellan birthday',1)")
    conn.commit()
    conn.close()
    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps(
        {"type": "user", "sessionId": "s1",
         "timestamp": "2026-05-17T01:00:00Z",
         "message": {"role": "user", "content": "ping"}}))
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    assert hooks.main(["session_end"]) == 0
    from pathlib import Path
    sub = Path(tmp_path / "db-pages" / "milestone.md")
    assert not sub.exists(), "session_end must not write milestone.md"


def test_session_end_no_transcript_is_safe(env, monkeypatch):
    _stdin(monkeypatch, {"session_id": "s1"})
    assert hooks.main(["session_end"]) == 0


def test_unknown_event_usage_error(env, monkeypatch):
    _stdin(monkeypatch, {})
    assert hooks.main(["bogus"]) == 2


# ── affect backdrop tests ─────────────────────────────────────────────────────

def _insert_affect(conn, date: str, ep: int, valence: float, arousal: float,
                   importance: int = 5, label: str | None = None,
                   source: str | None = None, description: str | None = None):
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label, "
        "description, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (date, ep, valence, arousal, importance, label, description, source),
    )
    conn.commit()


def _insert_event(conn, date: str, session_id: str = "s1"):
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content) "
        "VALUES (?, ?, 'user', 'hello')",
        (session_id, f"{date}T10:00:00Z"),
    )
    conn.commit()


def test_affect_backdrop_empty_renders_placeholder(env, monkeypatch, capsys):
    """No data => Timeline block renders with _none_ placeholder."""
    _stdin(monkeypatch, {})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Timeline" in ctx
    assert "_none_" in ctx


def test_affect_backdrop_present_in_context(env, monkeypatch, capsys):
    """With recent open affect rows, session_start injects the Timeline block."""
    db, _, _ = env
    conn = storage.connect(db)
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(timezone.utc).date()
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label,"
        " description, source, unresolved, created_at)"
        " VALUES (?, 1, 0.7, 0.7, 3, '开心', '项目过审', 'test', 1, ?)",
        (today.isoformat(), ts_now),
    )
    conn.commit()
    conn.close()

    _stdin(monkeypatch, {})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Timeline" in ctx
    assert "项目过审" in ctx


def test_affect_backdrop_anchors_after_6am_rollover(env, monkeypatch, capsys):
    """Past 6AM: recent session digest appears in Timeline 24h film-strip."""
    db, _, _ = env
    conn = storage.connect(db)
    ts_recent = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO session_digests (sid, date, ts, text, kind, life_lines)"
        " VALUES ('sid-test', ?, ?, 'body', 'casual', '昨晚聊了很多')",
        (ts_recent[:10], ts_recent),
    )
    conn.commit()
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "## Timeline" in ctx
    assert "昨晚聊了很多" in ctx


def test_session_start_zone_caps_keep_output_bounded(env, monkeypatch, capsys):
    """Zone-level caps keep SessionStart output under hook stdout limit."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    for i in range(50):
        conn.execute("INSERT INTO tasks(category,title,status) VALUES('work',?,?)",
                     (f"Task {i} " + "x" * 100, "active"))
    for i in range(20):
        conn.execute("INSERT INTO alerts(severity,type,message) VALUES('warn','test',?)",
                     ("Alert " + "y" * 200,))
    for i in range(10):
        _insert_affect(conn, today.isoformat(), i + 1, 0.5, 0.5,
                       label="Z" * 50)
    conn.commit()
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= 10000


# ── heartbeat tests ───────────────────────────────────────────────────────────

def test_heartbeat_no_events_no_alert(env, monkeypatch, capsys):
    """No events at all => no heartbeat block."""
    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "⚠" not in ctx


def test_heartbeat_events_with_affect_no_alert(env, monkeypatch, capsys):
    """Day has events AND affect => no heartbeat."""
    db, _, _ = env
    conn = storage.connect(db)
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    _insert_event(conn, yesterday)
    _insert_affect(conn, yesterday, 1, 0.3, 0.3)
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "⚠" not in ctx


def test_heartbeat_events_without_affect_fires(env, monkeypatch, capsys):
    """Day has events but NO affect => heartbeat block appears."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    anchor = (today - timedelta(days=30)).isoformat()
    _insert_affect(conn, anchor, 1, 0.5, 0.5)  # pipeline-start anchor
    _insert_event(conn, yesterday)
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "⚠" in ctx
    assert yesterday in ctx


def test_heartbeat_gap_within_7d(env, monkeypatch, capsys):
    """Gap 6 days ago (events, no affect) => heartbeat fires."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    six_ago = (today - timedelta(days=6)).isoformat()
    anchor = (today - timedelta(days=30)).isoformat()
    _insert_affect(conn, anchor, 1, 0.5, 0.5)  # pipeline-start anchor
    _insert_event(conn, six_ago)
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "⚠" in ctx
    assert six_ago in ctx


def test_heartbeat_reports_most_recent_gap(env, monkeypatch, capsys):
    """Multiple gaps: report the most recent one (smallest days_ago)."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    anchor = (today - timedelta(days=30)).isoformat()
    _insert_affect(conn, anchor, 1, 0.5, 0.5)  # pipeline-start anchor
    for delta in [2, 5]:
        d = (today - timedelta(days=delta)).isoformat()
        _insert_event(conn, d)
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "⚠" in ctx
    two_ago = (today - timedelta(days=2)).isoformat()
    assert two_ago in ctx


def test_heartbeat_beyond_7d_ignored(env, monkeypatch, capsys):
    """Gap at day 8 (outside 7d window) => no heartbeat."""
    db, _, _ = env
    conn = storage.connect(db)
    eight_ago = (datetime.now(timezone.utc).date() - timedelta(days=8)).isoformat()
    _insert_event(conn, eight_ago)
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "⚠" not in ctx


def test_heartbeat_before_pipeline_start_ignored(env, monkeypatch, capsys):
    """Events older than first affect row are pre-pipeline → no warning."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    five_ago = (today - timedelta(days=5)).isoformat()
    # Pipeline first produced affect yesterday; 5 days ago events are pre-pipeline.
    _insert_affect(conn, yesterday, 1, 0.5, 0.5)
    _insert_event(conn, five_ago)
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "⚠" not in ctx


def test_heartbeat_no_affect_anywhere_ignored(env, monkeypatch, capsys):
    """Affect pipeline never produced anything → silent (warning would be noise)."""
    db, _, _ = env
    conn = storage.connect(db)
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    _insert_event(conn, yesterday)
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "⚠" not in ctx


# ── user_prompt_submit tests (wired to recall.recall_fusion) ─────────────────

def test_user_prompt_submit_explicit_disable(env, monkeypatch, capsys):
    """Explicit recall.vector = false => no-op, no output."""
    base_cfg = config.load()
    base_cfg.setdefault("recall", {})["vector"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    _stdin(monkeypatch, {"prompt": "hello", "session_id": "s1"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_user_prompt_submit_exclude_cwds_match_noop(env, monkeypatch, capsys):
    """[recall].exclude_cwds (C3 groundwork, HANDOVER queue item 2): session
    cwd starting with a listed prefix skips recall injection entirely."""
    base_cfg = config.load()
    base_cfg.setdefault("recall", {})["vector"] = True
    base_cfg["recall"]["exclude_cwds"] = ["/Users/Gabrielle/private-project"]
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    _stdin(monkeypatch, {"prompt": "hello", "session_id": "s1",
                         "cwd": "/Users/Gabrielle/private-project/sub"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_user_prompt_submit_exclude_cwds_no_match_proceeds(env, monkeypatch, capsys):
    """A cwd not matching any exclude_cwds prefix is unaffected (falls
    through to the normal gate/config checks below, not a forced hit)."""
    base_cfg = config.load()
    base_cfg.setdefault("recall", {})["vector"] = False  # isolate this gate
    base_cfg["recall"]["exclude_cwds"] = ["/Users/Gabrielle/private-project"]
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    _stdin(monkeypatch, {"prompt": "hello", "session_id": "s1",
                         "cwd": "/Users/Gabrielle/CC-Lab/marrow"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    # vector=false still no-ops downstream, but for a DIFFERENT reason —
    # confirms the exclude_cwds branch didn't consume it (no output either way).
    assert capsys.readouterr().out == ""


def _force_vector_on(monkeypatch, min_score: float = 0.30):
    base_cfg = config.load()
    base_cfg.setdefault("recall", {})["vector"] = True
    # Lower min_score so FTS-only event hits (vec=0, bm25+recency ~0.35-0.39)
    # clear the gate in tests that have no embedder loaded.
    base_cfg["recall"]["min_score"] = min_score
    monkeypatch.setattr(config, "load", lambda: base_cfg)


def test_user_prompt_submit_no_hits_noop(env, monkeypatch, capsys):
    """vector=true + no matching events => no additionalContext written."""
    _force_vector_on(monkeypatch)
    _stdin(monkeypatch, {"prompt": "hello", "session_id": "s1"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_user_prompt_submit_empty_prompt_noop(env, monkeypatch, capsys):
    """Empty prompt with vector=true => graceful no-op."""
    _force_vector_on(monkeypatch)
    _stdin(monkeypatch, {"prompt": "", "session_id": "s1"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_user_prompt_submit_emits_recall_block(env, monkeypatch, capsys):
    """vector=true + matching FTS event => ## Recall block in additionalContext."""
    db, _, _ = env
    conn = storage.connect(db)
    conn.execute(
        "INSERT INTO events(session_id,timestamp,role,content) "
        "VALUES('s9','2026-05-20T10:00:00Z','user','build phase 1 plan')")
    conn.commit()
    conn.close()
    _force_vector_on(monkeypatch)
    _stdin(monkeypatch, {"prompt": "phase 1 plan", "session_id": "s1"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out, "expected stdout JSON with additionalContext"
    data = json.loads(out)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "## Recall" in ctx
    assert "phase 1 plan" in ctx
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


# ── lifecycle marker tests ────────────────────────────────────────────────────

def test_session_start_writes_lifecycle_marker(env, monkeypatch, capsys):
    """session_start with a session_id -> audit_log has lifecycle:start row."""
    db, _, _ = env
    _stdin(monkeypatch, {"session_id": "test-lc-start"})
    rc = hooks.main(["session_start"])
    assert rc == 0
    # Consume stdout to avoid pytest capsys noise.
    capsys.readouterr()
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE action='session_lifecycle:start' AND target_id='test-lc-start'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "lifecycle:start row not written"
    summary = row["summary"]
    assert "ppid=" in summary
    assert "source=cc" in summary
    assert "started_at=" in summary


def test_session_end_writes_lifecycle_end_marker(env, monkeypatch, tmp_path):
    """session_end -> audit_log has lifecycle:end row."""
    db, _, _ = env
    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "lc-end-sid",
        "timestamp": "2026-05-25T10:00:00Z",
        "message": {"role": "user", "content": "hello"},
    }))
    _stdin(monkeypatch, {"session_id": "lc-end-sid", "transcript_path": str(jl)})
    rc = hooks.main(["session_end"])
    assert rc == 0
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT 1 FROM audit_log"
            " WHERE action='session_lifecycle:end' AND target_id='lc-end-sid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "lifecycle:end row not written"


def test_session_end_headless_writes_lifecycle_end_and_ended_at(
    env, monkeypatch, tmp_path
):
    """Headless SessionEnd exits early, but still leaves a terminal marker."""
    db, _, _ = env
    jl = tmp_path / "headless.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "headless-sid",
        "timestamp": "2026-05-25T10:00:00Z",
        "message": {
            "role": "user",
            "content": "Compress this file per the rules. Output ONLY",
        },
    }))
    conn = storage.connect(db)
    with conn:
        conn.execute("INSERT INTO sessions (sid) VALUES ('headless-sid')")
    conn.close()

    _stdin(monkeypatch, {
        "session_id": "headless-sid",
        "transcript_path": str(jl),
    })
    rc = hooks.main(["session_end"])

    assert rc == 0
    conn = storage.connect(db)
    try:
        sess = conn.execute(
            "SELECT ended_at FROM sessions WHERE sid='headless-sid'"
        ).fetchone()
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE action='session_lifecycle:end'"
            " AND target_id='headless-sid'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    finally:
        conn.close()
    assert sess is not None and sess["ended_at"]
    assert row is not None and row["summary"] == "headless=1"
    assert n == 0


def test_session_end_subagent_writes_lifecycle_end_and_ended_at(
    env, monkeypatch, tmp_path
):
    """Task-tool transcripts under /tasks/ skip archive/extract cleanly."""
    db, _, _ = env
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    jl = tasks_dir / "subagent.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "subagent-sid",
        "timestamp": "2026-05-25T10:00:00Z",
        "message": {"role": "user", "content": "normal subagent work"},
    }))
    conn = storage.connect(db)
    with conn:
        conn.execute("INSERT INTO sessions (sid) VALUES ('subagent-sid')")
    conn.close()

    _stdin(monkeypatch, {
        "session_id": "subagent-sid",
        "transcript_path": str(jl),
    })
    rc = hooks.main(["session_end"])

    assert rc == 0
    conn = storage.connect(db)
    try:
        sess = conn.execute(
            "SELECT ended_at FROM sessions WHERE sid='subagent-sid'"
        ).fetchone()
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE action='session_lifecycle:end'"
            " AND target_id='subagent-sid'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    finally:
        conn.close()
    assert sess is not None and sess["ended_at"]
    assert row is not None and row["summary"] == "subagent=1"
    assert n == 0


def test_session_start_marrow_cortex_full_parity(env, monkeypatch, capsys):
    """B3m (07-08): cortex session_start gets the same lifecycle:start row,
    sessions row (channel=ct via MARROW_CHANNEL set alongside MARROW_CORTEX
    in llm.py) and injected context as any other session."""
    db, _, _ = env
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    _stdin(monkeypatch, {"session_id": "cortex-sid-1"})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["additionalContext"] != ""
    conn = storage.connect(db)
    try:
        lc = conn.execute(
            "SELECT 1 FROM audit_log"
            " WHERE action='session_lifecycle:start' AND target_id='cortex-sid-1'"
        ).fetchone()
        sess = conn.execute(
            "SELECT channel FROM sessions WHERE sid='cortex-sid-1'"
        ).fetchone()
    finally:
        conn.close()
    assert lc is not None
    assert sess is not None and sess["channel"] == "ct"


def test_session_end_marrow_cortex_full_parity(env, monkeypatch, tmp_path):
    """B3m (07-08): cortex session_end writes lifecycle:end like any other
    session. Events are archived per-turn by the Stop hook, not here."""
    db, _, _ = env
    jl = tmp_path / "cortex.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "cortex-sid-2",
        "timestamp": "2026-07-03T10:00:00Z",
        "message": {"role": "user", "content": "cortex wake prompt"},
    }))
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    _stdin(monkeypatch, {"session_id": "cortex-sid-2", "transcript_path": str(jl)})
    rc = hooks.main(["session_end"])
    assert rc == 0
    conn = storage.connect(db)
    try:
        lc = conn.execute(
            "SELECT 1 FROM audit_log"
            " WHERE action='session_lifecycle:end' AND target_id='cortex-sid-2'"
        ).fetchone()
    finally:
        conn.close()
    assert lc is not None


def test_user_prompt_submit_marrow_cortex_full_parity(env, monkeypatch, capsys):
    """B3m (07-08): cortex user_prompt_submit gets title/model backfill +
    touch like any other session (full memory parity, no recall short-circuit)."""
    db, _, _ = env
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    conn = storage.connect(db)
    conn.execute("INSERT INTO sessions (sid) VALUES ('cortex-sid-3')")
    conn.commit()
    conn.close()
    _stdin(monkeypatch, {"session_id": "cortex-sid-3", "prompt": "what should I do now?"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    conn = storage.connect(db)
    try:
        sess = conn.execute(
            "SELECT last_active FROM sessions WHERE sid='cortex-sid-3'"
        ).fetchone()
    finally:
        conn.close()
    assert sess is not None and sess["last_active"]


# ── pretool_use backup guard — stateless, two tiers ──────────────────────────
# Silent (tmp/scratchpad/worktrees, same-command backup, git) / Reminder
# (additionalContext, fires EVERY call, no dedup) / Deny (permissionDecision
# "deny": recursive rm / db destruction with no same-command backup;
# downgrades to reminder when backup_guard_intercept=false). Git ops are owned
# by the git-revert ask guard and the force-push deny guard.

from pathlib import Path as _Path

_BG_MSG = "back up code/db OR archive docs"
_BG_DENY_MSG = "bulk deletion with no backup"
_MV_DST = str(_Path.home() / "CC-Lab" / "marrow" / "_bg_test_dst")


def _pretool(monkeypatch, tool_name, tool_input, sid="s1", cwd=None):
    payload = {"session_id": sid, "tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        payload["cwd"] = cwd
    _stdin(monkeypatch, payload)
    return hooks.main(["pretool_use"])


def _out(capsys):
    return json.loads(capsys.readouterr().out)


def _hook_out(capsys):
    """hookSpecificOutput dict; empty stdout (fully silent) -> {}."""
    raw = capsys.readouterr().out.strip()
    if not raw:
        return {}
    return json.loads(raw).get("hookSpecificOutput", {})


def test_backup_guard_rm_single_file_whitelisted_no_trigger(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "rm /tmp/foo.txt"})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG not in out["hookSpecificOutput"]["additionalContext"]


def test_backup_guard_git_status_no_trigger(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git status"})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG not in out["hookSpecificOutput"]["additionalContext"]


# -- Silent: whitelist + same-command backup ----------------------------------

def test_backup_guard_rm_rf_tmp_silent(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf /tmp/foo"})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG not in out["hookSpecificOutput"]["additionalContext"]


def test_backup_guard_rm_rf_private_tmp_silent(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf /private/tmp/foo"})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG not in out["hookSpecificOutput"]["additionalContext"]


def test_backup_guard_scratchpad_silent(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "rm -rf /Users/x/project/scratchpad/old"})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG not in out["hookSpecificOutput"]["additionalContext"]


def test_backup_guard_recursive_rm_with_tar_backup_silent(env, monkeypatch, capsys):
    # Escape hatch: a backup action in the SAME command → fully silent allow,
    # no deny AND no reminder.
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "tar -czf /tmp/bak.tgz ~/projects/x && rm -rf ~/projects/x"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")
    assert _BG_DENY_MSG not in out.get("additionalContext", "")


def test_backup_guard_recursive_rm_with_cp_backup_silent(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "cp -r ~/projects/x /tmp/bak && rm -rf ~/projects/x"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_recursive_rm_backup_after_still_denies(env, monkeypatch, capsys):
    """Codex P2 fix: the escape hatch is segment-ORDERED. A backup keyword
    landing AFTER the destructive segment must not launder it — deny stands."""
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "rm -rf ~/projects/x && tar -czf /tmp/bak.tgz ~/projects/x"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert _BG_DENY_MSG in out["permissionDecisionReason"]


def test_backup_guard_recursive_rm_unrelated_cp_before_allows_order_only(
    env, monkeypatch, capsys
):
    """Position-only check, no backup-target matching (explicitly rejected —
    false-positive explosion vs minimal-interception). A `cp` of an UNRELATED
    path before the destructive segment still satisfies the escape hatch."""
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "cp ~/unrelated /tmp/whatever && rm -rf ~/projects/x"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out


# -- Reminder: fires EVERY call, no dedup -------------------------------------

def test_backup_guard_rm_single_file_reminds_every_call(env, monkeypatch, capsys):
    # Non-recursive rm on a non-whitelisted path → reminder, every call (no
    # once-per-session dedup).
    for _ in range(2):
        rc = _pretool(monkeypatch, "Bash", {"command": "rm ~/projects/note.txt"})
        assert rc == 0
        out = _out(capsys)["hookSpecificOutput"]
        assert "permissionDecision" not in out
        assert _BG_MSG in out["additionalContext"]


def test_backup_guard_bulk_mv_reminds(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": f"mv src/* {_MV_DST}"})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG in out["hookSpecificOutput"]["additionalContext"]


def test_backup_guard_delete_from_no_where_elsewhere_reminds(env, monkeypatch, capsys):
    # DELETE FROM without WHERE that is NOT a sqlite3 .db destruction → reminder.
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'psql -c "DELETE FROM events"'})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG in out["hookSpecificOutput"]["additionalContext"]


def test_backup_guard_event_clear_reminds(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "mcp__marrow__event_clear", {})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG in out["hookSpecificOutput"]["additionalContext"]


def test_backup_guard_mcp_action_delete_reminds(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "mcp__marrow__milestone", {"action": "delete"})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG in out["hookSpecificOutput"]["additionalContext"]


# -- Deny: recursive rm / db destruction, stateless ---------------------------

def test_backup_guard_recursive_rm_no_backup_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/projects/x"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert _BG_DENY_MSG in out["permissionDecisionReason"]
    assert "additionalContext" not in out


def test_backup_guard_recursive_rm_relative_no_backup_denies(env, monkeypatch, capsys):
    # Any non-whitelisted path (relative too) with recursive rm → deny.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -r build/output"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


# -- Relative path + cwd resolution (whitelist test only) ---------------------

def test_backup_guard_relative_rm_rf_cwd_in_scratchpad_silent(env, monkeypatch, capsys):
    # Bug fix: `cd <scratchpad> && rm -rf ask-demo` was denied even though cwd
    # resolves inside the whitelisted scratchpad zone.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ask-demo"},
                  cwd="/private/tmp/claude-501/proj/scratchpad")
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_relative_rm_rf_cwd_outside_whitelist_denies(env, monkeypatch, capsys):
    # cwd outside both whitelist AND trash zones → relative rm -rf still denies.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ask-demo"},
                  cwd="/Users/Gabrielle/projects")
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_relative_rm_single_file_cwd_whitelisted_silent(env, monkeypatch, capsys):
    # Non-recursive relative rm with a whitelisted cwd → fully silent (no
    # reminder either — the resolved path IS whitelisted).
    rc = _pretool(monkeypatch, "Bash", {"command": "rm ask-demo.txt"},
                  cwd="/private/tmp/claude-501/proj/scratchpad")
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_relative_rm_rf_missing_cwd_denies(env, monkeypatch, capsys):
    # No cwd provided at all (not just empty) + relative recursive rm →
    # unchanged today's behavior: treated as non-whitelisted, deny.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ask-demo"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_rm_db_file_denies(env, monkeypatch, capsys):
    # rm of a *.db file (even non-recursive) outside the whitelist → deny.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm ~/.config/marrow/marrow.db"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_rm_db_file_with_backup_allows(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "cp ~/x.db /tmp/x.db.backup && rm ~/x.db"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_sqlite_delete_no_where_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'sqlite3 t.db "DELETE FROM events"'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_sqlite_delete_no_where_with_backup_allows(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'cp t.db /tmp/t.db.bak && sqlite3 t.db "DELETE FROM events"'})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_sqlite_delete_backup_after_still_denies(env, monkeypatch, capsys):
    """Same ordering fix applied to db-destruction: cp AFTER the sqlite3
    destructive segment must not launder it."""
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'sqlite3 t.db "DELETE FROM events" && cp t.db /tmp/t.db.bak'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_drop_table_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'sqlite3 t.db "DROP TABLE tasks"'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_settings_json_edit_now_silent(env, monkeypatch, capsys):
    # Write/Edit is no longer guarded — a write requires a prior read, so it is
    # recoverable.
    rc = _pretool(monkeypatch, "Edit",
                  {"file_path": "/Users/x/.claude/settings.json", "old_string": "a",
                   "new_string": "b"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_intercept_off_downgrades_deny_to_reminder(env, monkeypatch, capsys):
    base_cfg = config.load()
    base_cfg.setdefault("hooks", {})["backup_guard_intercept"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/projects/x"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert _BG_MSG in out["additionalContext"]


# -- Config off / fail-open ---------------------------------------------------

def test_backup_guard_disabled_via_config(env, monkeypatch, capsys):
    base_cfg = config.load()
    base_cfg.setdefault("hooks", {})["backup_guard"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)

    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/projects/x"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert _BG_MSG not in out["additionalContext"]


def test_backup_guard_fail_open_malformed_input(env, monkeypatch, capsys):
    _stdin(monkeypatch, {"session_id": "s1", "tool_name": "Bash",
                         "tool_input": "not-a-dict"})
    rc = hooks.main(["pretool_use"])
    assert rc == 0


# ── rm → trash auto-rewrite ──────────────────────────────────────────────────
# Bash `rm` whose positional targets ALL fall under a trash_paths prefix is
# rewritten to `/usr/bin/trash <paths>` (recoverable) BEFORE the backup guard.
# Mixed / out-of-zone / wildcard targets fall through to the guard untouched.

_HOME = str(_Path.home())
_ICLOUD = _HOME + "/Library/Mobile Documents/com~apple~CloudDocs/Study/x.pdf"


def test_rm_to_trash_icloud_absolute(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'rm "~/Library/Mobile Documents/com~apple~CloudDocs/Study/x.pdf"'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["updatedInput"]["command"].startswith("/usr/bin/trash ")
    assert _ICLOUD in out["updatedInput"]["command"]
    assert "permissionDecision" not in out
    assert "rm auto-rewritten to trash" in out["additionalContext"]
    assert _BG_MSG not in out["additionalContext"]


def test_rm_to_trash_icloud_cwd_relative(env, monkeypatch, capsys):
    cwd = _HOME + "/Library/Mobile Documents/com~apple~CloudDocs/Study"
    rc = _pretool(monkeypatch, "Bash", {"command": "rm x.pdf"}, cwd=cwd)
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["updatedInput"]["command"].startswith("/usr/bin/trash ")
    assert _ICLOUD in out["updatedInput"]["command"]
    assert "permissionDecision" not in out


def test_rm_to_trash_rf_ny_flags_dropped(env, monkeypatch, capsys):
    # ~/Desktop/NY/ is covered by the wider ~/Desktop/ trash prefix.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/Desktop/NY/db-pages/old"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    cmd = out["updatedInput"]["command"]
    assert cmd.startswith("/usr/bin/trash ")
    assert "-rf" not in cmd and "-r" not in cmd
    assert (_HOME + "/Desktop/NY/db-pages/old") in cmd
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_rm_to_trash_desktop_non_ny_rewritten(env, monkeypatch, capsys):
    # Whole ~/Desktop is iCloud-synced personal-file territory, not just NY.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/Desktop/random-project/old"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    cmd = out["updatedInput"]["command"]
    assert cmd.startswith("/usr/bin/trash ")
    assert (_HOME + "/Desktop/random-project/old") in cmd
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_rm_to_trash_non_trash_repo_not_rewritten_reminds(env, monkeypatch, capsys):
    # Path outside trash_paths (git repo) → NOT rewritten; guard reminder fires.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm ~/projects/note.txt"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "updatedInput" not in out
    assert _BG_MSG in out["additionalContext"]


def test_rm_to_trash_non_trash_recursive_still_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/projects/x"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "updatedInput" not in out
    assert out["permissionDecision"] == "deny"


def test_rm_to_trash_mixed_targets_not_rewritten(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "rm ~/Documents/a.txt ~/projects/b.txt"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "updatedInput" not in out
    assert _BG_MSG in out["additionalContext"]


def test_rm_to_trash_chained_only_rm_segment_rewritten(env, monkeypatch, capsys):
    import shlex
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "cd X && rm ~/Downloads/old.zip && echo done"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    expected = (
        "cd X && /usr/bin/trash "
        + shlex.quote(_HOME + "/Downloads/old.zip")
        + " && echo done"
    )
    assert out["updatedInput"]["command"] == expected
    assert "permissionDecision" not in out


def test_rm_to_trash_spaces_quoted_roundtrip(env, monkeypatch, capsys):
    import shlex
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'rm "~/Library/Mobile Documents/com~apple~CloudDocs/Study/x.pdf"'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    toks = shlex.split(out["updatedInput"]["command"])
    assert toks[0] == "/usr/bin/trash"
    assert toks[1:] == [_ICLOUD]


def test_rm_to_trash_disabled_via_config(env, monkeypatch, capsys):
    base_cfg = config.load()
    base_cfg.setdefault("hooks", {})["rm_to_trash"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    rc = _pretool(monkeypatch, "Bash", {"command": "rm ~/Downloads/old.zip"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "updatedInput" not in out


# -- git force-push guard — hard deny -----------------------------------------

def test_git_force_push_force_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git push --force origin main"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "force push" in out["permissionDecisionReason"]


def test_git_force_push_with_lease_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git push --force-with-lease origin main"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_git_force_push_short_flag_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git push -f"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_git_force_push_in_worktree_still_denies(env, monkeypatch, capsys):
    # No worktree exemption for force push.
    _stdin(monkeypatch, {
        "session_id": "s1", "tool_name": "Bash",
        "cwd": "/Users/x/.claude/worktrees/agent-abc/marrow",
        "tool_input": {"command": "git push --force origin br"},
    })
    rc = hooks.main(["pretool_use"])
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_git_force_push_commit_message_no_false_positive(env, monkeypatch, capsys):
    # A commit whose -m message merely mentions force push must NOT be denied.
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'git commit -m "git push --force is dangerous"'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out.get("permissionDecision") != "deny"


def test_git_plain_push_and_commit_silent(env, monkeypatch, capsys):
    for cmd in ("git push origin main", "git commit -m wip", "git merge feature"):
        rc = _pretool(monkeypatch, "Bash", {"command": cmd})
        assert rc == 0
        out = _out(capsys)["hookSpecificOutput"]
        assert out.get("permissionDecision") is None, cmd


def test_git_force_push_disabled_via_config(env, monkeypatch, capsys):
    base_cfg = config.load()
    base_cfg.setdefault("hooks", {})["git_force_push_guard"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    rc = _pretool(monkeypatch, "Bash", {"command": "git push --force origin main"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out.get("permissionDecision") != "deny"


# -- git revert-type authorship guard ("ask", 🤡 message) ---------------------

_ROBOT = "🤡"


def test_git_revert_reset_hard_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git reset --hard HEAD~1"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert _ROBOT in out["permissionDecisionReason"]


def test_git_revert_reset_hard_in_commit_message_no_match(env, monkeypatch, capsys):
    # A commit whose -m message merely contains "reset --hard" must NOT match.
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'git commit -m "reset --hard in message"'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out.get("permissionDecision") != "ask"
    assert out.get("permissionDecision") != "deny"


def test_git_revert_checkout_file_discard_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git checkout -- marrow/hooks.py"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_revert_checkout_treeish_before_dashdash_asks(env, monkeypatch, capsys):
    for cmd in ("git checkout HEAD -- marrow/hooks.py",
                "git checkout deadbeef1 -- marrow/hooks.py"):
        rc = _pretool(monkeypatch, "Bash", {"command": cmd})
        assert rc == 0
        out = _out(capsys)["hookSpecificOutput"]
        assert out["permissionDecision"] == "ask", cmd


def test_git_revert_checkout_branch_switch_no_dashdash_not_held(
    env, monkeypatch, capsys
):
    for cmd in ("git checkout some-branch", "git checkout -b newbranch"):
        rc = _pretool(monkeypatch, "Bash", {"command": cmd})
        assert rc == 0
        out = _out(capsys)["hookSpecificOutput"]
        assert out.get("permissionDecision") != "ask", cmd


def test_git_revert_restore_worktree_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git restore marrow/hooks.py"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_revert_restore_staged_only_is_safe(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git restore --staged marrow/hooks.py"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "permissionDecision" not in out


def test_git_revert_clean_f_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git clean -fd"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_branch_cap_d_asks_for_authorship(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git branch -D old-feature"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_worktree_remove_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git worktree remove /tmp/wt"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert _ROBOT in out["permissionDecisionReason"]


def test_git_worktree_remove_in_worktree_cwd_silent(env, monkeypatch, capsys):
    _stdin(monkeypatch, {
        "session_id": "s1", "tool_name": "Bash",
        "cwd": "/Users/x/.claude/worktrees/agent-abc/marrow",
        "tool_input": {"command": "git worktree remove /tmp/wt"},
    })
    rc = hooks.main(["pretool_use"])
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "permissionDecision" not in out


# -- per-segment evaluation ---------------------------------------------------

def test_git_revert_compound_restore_staged_then_unsafe_restore_asks(
    env, monkeypatch, capsys
):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git restore --staged a && git restore b"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_revert_restore_staged_alone_still_passes(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git restore --staged a"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "permissionDecision" not in out


def test_git_revert_compound_status_then_reset_hard_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git status && git reset --hard"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_revert_normal_git_commands_pass(env, monkeypatch, capsys):
    for cmd in ("git status", "git log --oneline", "git diff HEAD",
                "git commit -m wip", "git push origin main"):
        rc = _pretool(monkeypatch, "Bash", {"command": cmd})
        assert rc == 0
        out = _out(capsys)["hookSpecificOutput"]
        assert out.get("permissionDecision") != "ask", cmd


def test_git_revert_branch_cap_d_worktree_cwd_silent(env, monkeypatch, capsys):
    # branch -D whose cwd is a worktree = agent teardown → ask skipped
    # (worktree exemption). Git no longer routes through the backup deny gate,
    # so with nothing else destructive it is silent.
    _stdin(monkeypatch, {
        "session_id": "s1", "tool_name": "Bash",
        "cwd": "/Users/x/.claude/worktrees/agent-abc/marrow",
        "tool_input": {"command": "git branch -D agent-abc"},
    })
    rc = hooks.main(["pretool_use"])
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "permissionDecision" not in out


def test_git_revert_worktree_substring_compound_bypass_still_denies(
    env, monkeypatch, capsys
):
    # A compound command whose git-revert segment substring-matches the
    # worktree exemption must NOT let an unrelated recursive rm on a
    # non-whitelisted path ride through — the "" exempt result only skips the
    # ASK, never the backup deny.
    cmd = ("git checkout -- /Users/x/.claude/worktrees/agent-abc/f "
           "&& rm -rf ~/projects/y")
    rc = _pretool(monkeypatch, "Bash", {"command": cmd})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out.get("permissionDecision") == "deny"
    assert "permissionDecisionReason" in out


def test_git_revert_relative_worktree_path_in_cmd_silent(env, monkeypatch, capsys):
    # Relative worktree path in the command (no leading slash) must still hit
    # the worktree/agent-cleanup exemption — cwd itself is not a worktree.
    cmd = (
        'git merge --no-ff some-branch -m "x" '
        "&& git worktree remove .claude/worktrees/agent-foo "
        "&& git branch -d some-branch"
    )
    rc = _pretool(monkeypatch, "Bash", {"command": cmd})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "permissionDecision" not in out


def test_git_revert_disabled_via_config(env, monkeypatch, capsys):
    base_cfg = config.load()
    base_cfg.setdefault("hooks", {})["git_revert_guard"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    rc = _pretool(monkeypatch, "Bash", {"command": "git reset --hard HEAD~1"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out.get("permissionDecision") != "ask"
