"""session_start / session_end / stop hook entrypoints.

Hooks read paths from config; tests point config at a tmp db via
monkeypatch and drive main() with stdin JSON like CC does.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


from marrow import hooks, storage
from _hooks_shared import (  # noqa: F401 — fixtures resolved by name
    _hook_out,
    _iso,
    _no_real_git,
    _out,
    _pretool,
    _seed_session,
    _stdin,
    env,
)

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


def test_affect_backdrop_empty_renders_placeholder(env, monkeypatch, capsys):
    """No data => Timeline block renders with _none_ placeholder."""
    _stdin(monkeypatch, {})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Timeline" in ctx
    assert "_none_" in ctx


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
