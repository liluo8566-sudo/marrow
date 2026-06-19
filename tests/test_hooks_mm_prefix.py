"""Tests for _handle_mm_prefix three-branch logic in hooks.py.

Covers:
  - mm+ empty arg        → current sid force flag
  - mm+ UUID arg         → named sid force flag
  - mm+ natural-language → no audit write, no spawn, stdout has injection JSON
  - mm- natural-language → same handoff
  - mm- empty            → current sid manual_skip (existing behaviour)
  - mm- UUID arg         → named sid manual_skip
  - mm! empty            → lists sessions without successful sessionend
  - mm! UUID             → immediate named sid spawn
  - mm!! current sid     → pre-archives jsonl before spawn
"""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from marrow import config, hooks, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db, tmp_path


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


# ── _looks_like_sid unit tests ────────────────────────────────────────────────

def test_looks_like_sid_full_uuid():
    assert hooks._looks_like_sid("7f1473ca-a8ab-4207-a8a8-57418d3a2c5b") is True


def test_looks_like_sid_short_prefix_8():
    assert hooks._looks_like_sid("7f1473ca") is True


def test_looks_like_sid_short_with_dash():
    assert hooks._looks_like_sid("7f1473ca-a8ab") is True


def test_looks_like_sid_rejects_natural_language():
    assert hooks._looks_like_sid("我来试试看～嘿嘿嘿") is False


def test_looks_like_sid_rejects_whitespace():
    assert hooks._looks_like_sid("7f1473ca a8ab") is False


def test_looks_like_sid_rejects_multiline():
    assert hooks._looks_like_sid("7f1473ca\nabc") is False


def test_looks_like_sid_rejects_empty():
    assert hooks._looks_like_sid("") is False


# ── mm+ empty → current sid force flag ───────────────────────────────────────

def test_mm_plus_empty_flags_current_sid(env, monkeypatch, capsys):
    """mm+ with no arg clears manual skip and flags current sid for sessionend."""
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    _stdin(monkeypatch, {"prompt": "mm+", "session_id": "cur-sid-001"})
    popen_calls = []
    with patch("marrow.hooks.popen_detach_lazy",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert "mm+ control signal" in capsys.readouterr().out
    assert popen_calls == []
    conn = storage.connect(db)
    try:
        rows = conn.execute(
            "SELECT action, summary FROM audit_log"
            " WHERE target_id='cur-sid-001' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert [(r["action"], r["summary"]) for r in rows] == [
        ("manual_skip", "skip_cleared"),
        ("force_sessionend", "mm_plus_flag"),
    ]


# ── mm+ UUID → named sid force flag ──────────────────────────────────────────

def test_mm_plus_uuid_flags_named_sid(env, monkeypatch, capsys):
    """mm+ <uuid> flags the given sid, not current."""
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    named_sid = "7f1473ca-a8ab-4207-a8a8-57418d3a2c5b"
    _stdin(monkeypatch, {"prompt": f"mm+ {named_sid}", "session_id": "other-sid"})
    popen_calls = []
    with patch("marrow.hooks.popen_detach_lazy",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert "mm+ control signal" in capsys.readouterr().out
    assert popen_calls == []
    conn = storage.connect(db)
    try:
        rows = conn.execute(
            "SELECT action, summary FROM audit_log"
            " WHERE target_id=? ORDER BY id",
            (named_sid,),
        ).fetchall()
    finally:
        conn.close()
    assert [(r["action"], r["summary"]) for r in rows] == [
        ("manual_skip", "skip_cleared"),
        ("force_sessionend", "mm_plus_flag"),
    ]


def test_mm_plus_force_flag_survives_context_manager_rollback(env, monkeypatch, capsys):
    """force_sessionend is written after a rolled-back earlier context."""
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    named_sid = "7f1473ca-a8ab-4207-a8a8-57418d3a2c5b"
    real_connect = storage.connect

    class RollbackFirstContext:
        def __init__(self, conn):
            self._conn = conn
            self._rollback_next = True

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            if self._rollback_next and exc_type is None:
                self._rollback_next = False
                self._conn.rollback()
                return False
            return self._conn.__exit__(exc_type, exc, tb)

        def execute(self, *args, **kwargs):
            return self._conn.execute(*args, **kwargs)

        def commit(self):
            return self._conn.commit()

        def close(self):
            return self._conn.close()

    monkeypatch.setattr(
        storage, "connect",
        lambda path=None: RollbackFirstContext(real_connect(path)),
    )
    _stdin(monkeypatch, {"prompt": f"mm+ {named_sid}", "session_id": "other-sid"})
    popen_calls = []
    with patch("marrow.hooks.popen_detach_lazy",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["user_prompt_submit"])

    assert rc == 0
    assert "mm+ control signal" in capsys.readouterr().out
    assert popen_calls == []

    conn = real_connect(db)
    try:
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE target_id=? AND action='force_sessionend'"
            " ORDER BY id DESC LIMIT 1",
            (named_sid,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["summary"] == "mm_plus_flag"


# ── mm+ natural language → inject + no spawn ─────────────────────────────────

def test_mm_plus_natural_language_injects_context(env, monkeypatch, capsys):
    """mm+ <natural-lang clue> writes additionalContext JSON, no audit write, no spawn."""
    db, _ = env
    clue = "我来试试看～嘿嘿嘿（亲一口）\n都commit了么宝宝"
    _stdin(monkeypatch, {"prompt": f"mm+\n{clue}", "session_id": "s-cur"})
    popen_calls = []
    with patch("marrow.hooks.popen_detach_lazy",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    # No spawn
    assert popen_calls == []
    # No audit row written
    conn = storage.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action='sessionend_extract'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 0
    # stdout must be the inject JSON
    out = capsys.readouterr().out
    assert out, "expected stdout JSON with additionalContext"
    data = json.loads(out)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "mm+" in ctx
    assert "locate request" in ctx
    # Clue text appears (first line, stripped)
    assert "我来试试看" in ctx


# ── mm- natural language → inject + no skip write ────────────────────────────

def test_mm_minus_natural_language_injects_context(env, monkeypatch, capsys):
    """mm- <natural-lang> writes additionalContext, no manual_skip audit, no spawn."""
    db, _ = env
    clue = "之前那个session好像漏了"
    _stdin(monkeypatch, {"prompt": f"mm- {clue}", "session_id": "s-cur"})
    popen_calls = []
    with patch("marrow.hooks.popen_detach_lazy",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert popen_calls == []
    # No manual_skip audit written
    conn = storage.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action='manual_skip'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "mm-" in ctx
    assert "locate request" in ctx
    assert clue in ctx


# ── mm- empty → current sid manual_skip ──────────────────────────────────────

def test_mm_minus_empty_skips_current_sid(env, monkeypatch, capsys):
    """mm- with no arg writes manual_skip for current sid."""
    db, _ = env
    _stdin(monkeypatch, {"prompt": "mm-", "session_id": "skip-me-001"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    # hook emits silent_ack additionalContext telling LLM mm- is a control
    # signal (committed _inject_silent_ack behaviour, not chatter)
    assert "mm- control signal" in capsys.readouterr().out
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE target_id='skip-me-001' AND action='manual_skip' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["summary"] == "skip"


# ── mm- UUID → named sid manual_skip ─────────────────────────────────────────

def test_mm_minus_uuid_skips_named_sid(env, monkeypatch, capsys):
    """mm- <uuid> writes manual_skip for the named sid, not current."""
    db, _ = env
    named_sid = "abcdef12-1234-5678-9abc-def012345678"
    _stdin(monkeypatch, {"prompt": f"mm- {named_sid}", "session_id": "other-sid"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    # hook emits silent_ack additionalContext (committed _inject_silent_ack)
    assert "mm- control signal" in capsys.readouterr().out
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT summary FROM audit_log"
            f" WHERE target_id='{named_sid}' AND action='manual_skip' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["summary"] == "skip"


# ── mm! / mm!! immediate sessionend controls ─────────────────────────────────

def _make_jsonl(path, sid: str, n_turns: int = 5) -> None:
    """Write a minimal non-headless jsonl with n_turns user turns."""
    lines = []
    for i in range(n_turns):
        lines.append(json.dumps({
            "type": "user",
            "sessionId": sid,
            "timestamp": f"2026-05-27T0{i}:00:00Z",
            "message": {"role": "user", "content": f"turn {i}"},
        }))
    path.write_text("\n".join(lines))


def test_mm_bang_lists_unrun_sessions(env, monkeypatch, capsys):
    db, _ = env
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO sessions (sid, title, channel)"
            " VALUES ('sid-ok', 'Done', 'cli')"
        )
        conn.execute(
            "INSERT INTO sessions (sid, title, channel)"
            " VALUES ('sid-missing', 'Needs run', 'wx')"
        )
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', 'sid-ok', 'sessionend_extract', 'ok,user_count=4')"
        )
    conn.close()

    _stdin(monkeypatch, {"prompt": "mm！", "session_id": "cur"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "sid-missing" in ctx
    assert "sid-ok" not in ctx


def test_mm_bang_uuid_spawns_named_sid(env, monkeypatch, capsys):
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    named_sid = "7f1473ca-a8ab-4207-a8a8-57418d3a2c5b"
    _stdin(monkeypatch, {"prompt": f"mm! {named_sid}", "session_id": "other-sid"})
    popen_calls = []
    with patch("marrow.hooks.popen_detach_lazy",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert "mm! control signal" in capsys.readouterr().out
    spawned = [c for c in popen_calls if "sessionend_async" in " ".join(c)]
    assert any(named_sid in " ".join(c) for c in spawned)
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE target_id=? AND action='force_sessionend'",
            (named_sid,),
        ).fetchone()
    finally:
        conn.close()
    assert row["summary"] == "mm_immediate"


def test_mm_bang_bang_active_session_archives_events_before_spawn(env, monkeypatch, capsys):
    """mm!! on current sid pre-archives jsonl; events table has rows after hook."""
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    sid = "aabbccdd-1111-2222-3333-444455556666"
    jl = tmp_path / f"{sid}.jsonl"
    _make_jsonl(jl, sid, n_turns=5)

    _stdin(monkeypatch, {
        "prompt": "mm！！",
        "session_id": sid,
        "transcript_path": str(jl),
    })
    popen_calls = []
    with patch("marrow.hooks.popen_detach_lazy",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert "mm!! control signal" in capsys.readouterr().out

    assert any("sessionend_async" in " ".join(c) for c in popen_calls)
    spawned = [c for c in popen_calls if "sessionend_async" in " ".join(c)]
    assert any(sid in " ".join(c) for c in spawned)

    # Events were archived before spawn
    conn = storage.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE session_id=?", (sid,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 5


def test_mm_bang_named_sid_skips_archive(env, monkeypatch, capsys):
    """mm! <other-uuid> does NOT archive current session's jsonl."""
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    current_sid = "c0ffee00-0000-0000-0000-000000000000"
    other_sid = "deadbeef-1111-2222-3333-444455556666"
    jl = tmp_path / "current.jsonl"
    _make_jsonl(jl, current_sid, n_turns=3)

    _stdin(monkeypatch, {
        "prompt": f"mm! {other_sid}",
        "session_id": current_sid,
        "transcript_path": str(jl),
    })
    popen_calls = []
    clean_calls = []

    import marrow.transcript as _transcript
    orig_clean = _transcript.clean

    def _spy_clean(path):
        clean_calls.append(path)
        return orig_clean(path)

    with patch("marrow.hooks.popen_detach_lazy",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        with patch("marrow.transcript.clean", side_effect=_spy_clean):
            rc = hooks.main(["user_prompt_submit"])
    assert rc == 0

    # Spawn called with other_sid
    spawned = [c for c in popen_calls if "sessionend_async" in " ".join(c)]
    assert any(other_sid in " ".join(c) for c in spawned)

    # clean was NOT called (no archive for current sid)
    assert clean_calls == []

    # events table for current_sid stays empty
    conn = storage.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE session_id=?", (current_sid,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 0


def test_mm_bang_bang_jsonl_missing_still_spawns(env, monkeypatch, capsys):
    """Non-existent transcript_path silently skipped; spawn still fires."""
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    sid = "missing0-ffff-eeee-dddd-ccccbbbbaaaa"
    nonexistent = str(tmp_path / "no_such_file.jsonl")

    _stdin(monkeypatch, {
        "prompt": "mm!!",
        "session_id": sid,
        "transcript_path": nonexistent,
    })
    popen_calls = []
    with patch("marrow.hooks.popen_detach_lazy",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["user_prompt_submit"])
    assert rc == 0

    # Spawn still happened despite missing file
    assert any("sessionend_async" in " ".join(c) for c in popen_calls)
    spawned = [c for c in popen_calls if "sessionend_async" in " ".join(c)]
    assert any(sid in " ".join(c) for c in spawned)

    # No events written (file was missing)
    conn = storage.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE session_id=?", (sid,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 0
