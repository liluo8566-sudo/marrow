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


def test_session_end_archives_and_renders(env, monkeypatch, tmp_path):
    """session_end archives events; dashboard is NOT written by the hook
    (moved to sessionend_async tail)."""
    db, dash, _ = env
    jl = tmp_path / "s.jsonl"
    jl.write_text("\n".join(json.dumps(o) for o in [
        {"type": "user", "sessionId": "s1", "timestamp": "2026-05-17T01:00:00Z",
         "message": {"role": "user", "content": "build phase 1"}},
        {"type": "assistant", "sessionId": "s1",
         "timestamp": "2026-05-17T01:00:09Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "on it"}]}},
    ]))
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    rc = hooks.main(["session_end"])
    assert rc == 0
    conn = storage.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    finally:
        conn.close()
    assert n == 2
    # Dashboard must NOT be written by session_end hook (moved to async tail).
    from pathlib import Path
    assert not Path(dash).exists(), "session_end must not write dashboard directly"


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


def test_session_end_dashboard_eperm_alerts_warn(env, monkeypatch, tmp_path):
    """session_end no longer calls dashboard.write_dashboard — the call moved
    to sessionend_async tail. Hook must complete without calling dashboard and
    events must still be archived."""
    db, dash, _ = env
    jl = tmp_path / "s.jsonl"
    jl.write_text("\n".join(json.dumps(o) for o in [
        {"type": "user", "sessionId": "s1", "timestamp": "2026-05-17T01:00:00Z",
         "message": {"role": "user", "content": "build phase 1"}},
        {"type": "assistant", "sessionId": "s1",
         "timestamp": "2026-05-17T01:00:09Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "on it"}]}},
    ]))

    dash_calls: list = []

    def track(*a, **k):
        dash_calls.append(1)

    with patch("marrow.dashboard.write_dashboard", side_effect=track):
        _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
        rc = hooks.main(["session_end"])
    assert rc == 0
    conn = storage.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    finally:
        conn.close()
    assert n == 2
    assert dash_calls == [], "session_end must not call write_dashboard directly"


def test_session_end_real_error_still_alerts(env, monkeypatch, tmp_path):
    """session_end no longer calls dashboard — confirm hook runs cleanly
    without any dashboard reference and archives events as expected."""
    db, dash, _ = env
    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps(
        {"type": "user", "sessionId": "s1", "timestamp": "2026-05-17T01:00:00Z",
         "message": {"role": "user", "content": "hi"}}))

    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    assert hooks.main(["session_end"]) == 0
    conn = storage.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


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
    """No affect rows => Affect block still renders with _none_ placeholders."""
    _stdin(monkeypatch, {})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Affect" in ctx
    assert "_none_" in ctx


def test_affect_backdrop_present_in_context(env, monkeypatch, capsys):
    """With recent affect rows, session_start injects the shared render_affect block."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    _insert_affect(conn, today.isoformat(), 1, 0.7, 0.7, importance=3, label="开心",
                   description="项目过审")
    conn.close()

    _stdin(monkeypatch, {})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Affect" in ctx
    assert "### Today" in ctx
    assert "### This Week" in ctx
    assert "eph3 开心 | 项目过审" in ctx


def test_affect_backdrop_anchors_after_6am_rollover(env, monkeypatch, capsys):
    """Past 6AM with no new sessionend → prior day still surfaces (no empty Today)."""
    db, _, _ = env
    conn = storage.connect(db)
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    _insert_affect(conn, yesterday, 1, 0.5, 0.5, importance=2, label="昨",
                   description="昨天的事")
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "### Today" in ctx
    assert "eph2 昨" in ctx
    assert "_none_" not in ctx.split("### This Week")[0].split("### Today")[1]


def test_session_start_total_hard_cap(env, monkeypatch, capsys):
    """Total SessionStart output never exceeds SESSION_START_HARD_CAP chars."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    # Add a lot of tasks and alerts to bloat the output.
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
    assert len(ctx) <= hooks.SESSION_START_HARD_CAP


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
    with patch("marrow.hooks.popen_detach_lazy"):
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


def test_session_end_skips_popen_when_already_covered(env, monkeypatch, tmp_path):
    """session_end with ok,user_count=10 and 10 events -> popen_detach NOT called.

    After archive_events runs, DB has 10 user events. ok,user_count=10 means
    current_user (10) <= last_ok (10) -> gate fires, popen skipped.
    """
    db, _, _ = env
    jl = tmp_path / "s.jsonl"
    # Write 10 user events into transcript.
    lines = []
    for i in range(10):
        lines.append(json.dumps({
            "type": "user", "sessionId": "idem-sid",
            "timestamp": f"2026-05-25T10:{i:02d}:00Z",
            "message": {"role": "user", "content": f"msg {i}"},
        }))
    jl.write_text("\n".join(lines))
    # Pre-seed ok,user_count=10 row only (no events — archive_events inserts them).
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', 'idem-sid', 'sessionend_extract', 'ok,user_count=10')"
        )
    conn.close()
    _stdin(monkeypatch, {"session_id": "idem-sid", "transcript_path": str(jl)})
    popen_calls: list = []
    with patch("marrow.hooks.popen_detach_lazy",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["session_end"])
    assert rc == 0
    async_calls = [c for c in popen_calls if "sessionend_async" in " ".join(c)]
    assert async_calls == [], "popen_detach must be skipped when events already covered"


def test_session_end_fires_popen_when_events_grew(env, monkeypatch, tmp_path):
    """session_end with ok,user_count=10 but 15 new events -> popen_detach called.

    Transcript has 15 user events. After archive_events, DB has 15. ok,user_count=10
    means current_user (15) > last_ok (10) -> gate skipped, popen fires.
    """
    db, _, _ = env
    jl = tmp_path / "s.jsonl"
    lines = []
    for i in range(15):
        lines.append(json.dumps({
            "type": "user", "sessionId": "grew-sid",
            "timestamp": f"2026-05-25T10:{i:02d}:00Z",
            "message": {"role": "user", "content": f"msg {i}"},
        }))
    jl.write_text("\n".join(lines))
    # Pre-seed ok at count=10 only (no events — archive_events inserts 15).
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', 'grew-sid', 'sessionend_extract', 'ok,user_count=10')"
        )
    conn.close()
    _stdin(monkeypatch, {"session_id": "grew-sid", "transcript_path": str(jl)})
    popen_calls: list = []
    with patch("marrow.hooks.popen_detach_lazy",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["session_end"])
    assert rc == 0
    async_calls = [c for c in popen_calls if "sessionend_async" in " ".join(c)]
    assert len(async_calls) == 1, "popen_detach must be called when events grew"
