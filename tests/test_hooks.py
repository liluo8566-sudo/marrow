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
    conn = storage.init_db(db)
    conn.execute("INSERT INTO threads(category,title,status) "
                 "VALUES('study','GAMSAT plan','active')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "dashboard_path", lambda: dash)
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
    assert "GAMSAT plan" in ctx
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_session_end_archives_and_renders(env, monkeypatch, tmp_path):
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
    txt = open(dash).read()
    assert "GAMSAT plan" in txt and hooks.dashboard.M0 in txt


def test_session_end_dashboard_eperm_degrades_no_alert(env, monkeypatch, tmp_path):
    """TCC-protected Desktop write -> PermissionError must skip dashboard
    regen only; events still archived; no alert (mirrors alert#11)."""
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

    def boom(*a, **k):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(hooks.dashboard, "write_dashboard", boom)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    rc = hooks.main(["session_end"])
    assert rc == 0
    conn = storage.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        alerts = conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
    finally:
        conn.close()
    assert n == 2  # events archive leg still succeeded
    assert alerts == 0  # degraded silently, no handoff pollution


def test_session_end_real_error_still_alerts(env, monkeypatch, tmp_path):
    """A non-permission failure must still surface an alert (no broad catch)."""
    db, dash, _ = env
    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps(
        {"type": "user", "sessionId": "s1", "timestamp": "2026-05-17T01:00:00Z",
         "message": {"role": "user", "content": "hi"}}))

    def boom(*a, **k):
        raise ValueError("genuine bug")
    monkeypatch.setattr(hooks.dashboard, "write_dashboard", boom)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    assert hooks.main(["session_end"]) == 0
    conn = storage.connect(db)
    try:
        alerts = conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
    finally:
        conn.close()
    assert alerts == 1


def test_session_end_no_transcript_is_safe(env, monkeypatch):
    _stdin(monkeypatch, {"session_id": "s1"})
    assert hooks.main(["session_end"]) == 0


def test_unknown_event_usage_error(env, monkeypatch):
    _stdin(monkeypatch, {})
    assert hooks.main(["bogus"]) == 2


# ── affect backdrop tests ─────────────────────────────────────────────────────

def _insert_affect(conn, date: str, ep: int, valence: float, arousal: float,
                   importance: int = 5, label: str | None = None,
                   source: str | None = None):
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (date, ep, valence, arousal, importance, label, source),
    )
    conn.commit()


def _insert_event(conn, date: str, session_id: str = "s1"):
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content) "
        "VALUES (?, ?, 'user', 'hello')",
        (session_id, f"{date}T10:00:00Z"),
    )
    conn.commit()


def test_affect_backdrop_empty_returns_empty(env, monkeypatch, capsys):
    """No affect rows => backdrop section absent from context."""
    _stdin(monkeypatch, {})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Affect" not in ctx


def test_affect_backdrop_present_in_context(env, monkeypatch, capsys):
    """With recent affect rows, backdrop appears under ## Affect."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    _insert_affect(conn, today.isoformat(), 1, 0.6, 0.5, importance=7, label="开心")
    conn.close()

    _stdin(monkeypatch, {})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Affect" in ctx
    assert "亮" in ctx  # valence > 0.3
    assert "重" in ctx  # arousal >= 0.4


def test_affect_backdrop_valence_沉(env, monkeypatch, capsys):
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    _insert_affect(conn, today.isoformat(), 1, -0.5, 0.2, label="难过")
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "沉" in ctx
    assert "轻" in ctx


def test_affect_backdrop_trend_line_calm(env, monkeypatch, capsys):
    """Many similar valence rows -> 情绪平稳."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    for i in range(5):
        d = (today - timedelta(days=i)).isoformat()
        _insert_affect(conn, d, 1, 0.4, 0.3, importance=5)
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "情绪平稳" in ctx


def test_affect_backdrop_trend_line_swing(env, monkeypatch, capsys):
    """Alternating high/low valence -> 情绪波动 or 情绪剧烈波动."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    for i in range(6):
        d = (today - timedelta(days=i)).isoformat()
        v = 0.9 if i % 2 == 0 else -0.9
        _insert_affect(conn, d, 1, v, 0.5, importance=5)
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "波动" in ctx


def test_affect_backdrop_pending_element(env, monkeypatch, capsys):
    """source='pending' rows appear in ④ emotional-pending."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    _insert_affect(conn, today.isoformat(), 1, 0.5, 0.6, label="开心")
    _insert_affect(conn, today.isoformat(), 2, -0.4, 0.7,
                   label="争吵未解决", source="pending")
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "情感悬挂" in ctx
    assert "争吵未解决" in ctx


def test_affect_backdrop_pending_excluded_from_trend(env, monkeypatch, capsys):
    """pending rows excluded from trend calculation; only non-pending counted."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    # Only pending + one real
    _insert_affect(conn, today.isoformat(), 1, 0.4, 0.3, label="normal")
    _insert_affect(conn, today.isoformat(), 2, -0.9, 0.9,
                   label="unresolved", source="pending")
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    # Only 1 non-pending row -> no trend line (need >= 2)
    assert "趋势" not in ctx


def test_affect_backdrop_char_cap(env, monkeypatch, capsys):
    """Backdrop never exceeds BACKDROP_MAX_CHARS."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    for i in range(20):
        d = (today - timedelta(days=i % 7)).isoformat()
        long_label = "X" * 80
        _insert_affect(conn, d, i + 1, 0.5, 0.5, label=long_label)
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    # Full context can be longer (threads+alerts+backdrop), but backdrop itself ≤350
    # Extract backdrop section
    if "## Affect" in ctx:
        backdrop_section = ctx.split("## Affect\n", 1)[1].split("\n\n")[0]
        assert len(backdrop_section) <= hooks.BACKDROP_MAX_CHARS


def test_session_start_total_hard_cap(env, monkeypatch, capsys):
    """Total SessionStart output never exceeds SESSION_START_HARD_CAP chars."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    # Add a lot of threads and alerts to bloat the output.
    for i in range(50):
        conn.execute("INSERT INTO threads(category,title,status) VALUES('work',?,?)",
                     (f"Thread {i} " + "x" * 100, "active"))
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
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
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
    six_ago = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
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


# ── user_prompt_submit scaffold tests ─────────────────────────────────────────

def test_user_prompt_submit_disabled_by_default(env, monkeypatch, capsys):
    """Config recall.vector = false (default) => no-op, no output."""
    _stdin(monkeypatch, {"prompt": "hello", "session_id": "s1"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_user_prompt_submit_enabled_scaffold_noop(env, monkeypatch, capsys):
    """With recall.vector = true, scaffold runs but produces no context (TODO stub)."""
    _stdin(monkeypatch, {"prompt": "hello", "session_id": "s1"})

    base_cfg = config.load()
    base_cfg.setdefault("recall", {})["vector"] = True
    monkeypatch.setattr(config, "load", lambda: base_cfg)

    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    # Stub: no additionalContext output yet (wired after worktree C merges).
    assert capsys.readouterr().out == ""


def test_user_prompt_submit_empty_prompt_noop(env, monkeypatch, capsys):
    """Empty prompt with vector=true => graceful no-op."""
    _stdin(monkeypatch, {"prompt": "", "session_id": "s1"})

    base_cfg = config.load()
    base_cfg.setdefault("recall", {})["vector"] = True
    monkeypatch.setattr(config, "load", lambda: base_cfg)

    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""
