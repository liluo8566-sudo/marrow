"""Tests for _handle_mm_prefix three-branch logic in hooks.py.

Covers:
  - mm+ empty arg        → current sid spawn (existing behaviour)
  - mm+ UUID arg         → named sid spawn
  - mm+ natural-language → no audit write, no spawn, stdout has injection JSON
  - mm- natural-language → same handoff
  - mm- empty            → current sid manual_skip (existing behaviour)
  - mm- UUID arg         → named sid manual_skip
  - mm+ current sid      → pre-archives jsonl before spawn
  - mm+ named sid        → skips archive (other sid's session)
  - mm+ missing jsonl    → fails silently, spawn still fires
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


# ── mm+ empty → current sid spawn ────────────────────────────────────────────

def test_mm_plus_empty_spawns_current_sid(env, monkeypatch, capsys):
    """mm+ with no arg after prefix runs sessionend_async for the current sid."""
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    _stdin(monkeypatch, {"prompt": "mm+", "session_id": "cur-sid-001"})
    popen_calls = []
    with patch("marrow.hooks.popen_detach",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""  # no recall injection for mm+ prompts
    assert any("sessionend_async" in " ".join(c) for c in popen_calls)
    # Audit row written for current sid
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE target_id='cur-sid-001' AND action='sessionend_extract' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["summary"] == "reset:mm_plus"


# ── mm+ UUID → named sid spawn ────────────────────────────────────────────────

def test_mm_plus_uuid_spawns_named_sid(env, monkeypatch, capsys):
    """mm+ <uuid> runs sessionend_async for the given sid, not current."""
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    named_sid = "7f1473ca-a8ab-4207-a8a8-57418d3a2c5b"
    _stdin(monkeypatch, {"prompt": f"mm+ {named_sid}", "session_id": "other-sid"})
    popen_calls = []
    with patch("marrow.hooks.popen_detach",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert any("sessionend_async" in " ".join(c) for c in popen_calls)
    # The popen call must reference the named sid, not current
    spawned_args = [c for c in popen_calls if "sessionend_async" in " ".join(c)]
    assert any(named_sid in " ".join(c) for c in spawned_args)
    # Audit row for named sid
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT 1 FROM audit_log"
            f" WHERE target_id='{named_sid}' AND action='sessionend_extract' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


# ── mm+ natural language → inject + no spawn ─────────────────────────────────

def test_mm_plus_natural_language_injects_context(env, monkeypatch, capsys):
    """mm+ <natural-lang clue> writes additionalContext JSON, no audit write, no spawn."""
    db, _ = env
    clue = "我来试试看～嘿嘿嘿（亲一口）\n都commit了么宝宝"
    _stdin(monkeypatch, {"prompt": f"mm+\n{clue}", "session_id": "s-cur"})
    popen_calls = []
    with patch("marrow.hooks.popen_detach",
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
    assert "定位请求" in ctx
    # Clue text appears (first line, stripped)
    assert "我来试试看" in ctx


# ── mm- natural language → inject + no skip write ────────────────────────────

def test_mm_minus_natural_language_injects_context(env, monkeypatch, capsys):
    """mm- <natural-lang> writes additionalContext, no manual_skip audit, no spawn."""
    db, _ = env
    clue = "之前那个session好像漏了"
    _stdin(monkeypatch, {"prompt": f"mm- {clue}", "session_id": "s-cur"})
    popen_calls = []
    with patch("marrow.hooks.popen_detach",
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
    assert "定位请求" in ctx
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


# ── mm+ active session → pre-archive events before spawn ─────────────────────

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


def test_mm_plus_active_session_archives_events_before_spawn(env, monkeypatch, capsys):
    """mm+ on current sid pre-archives jsonl; events table has rows after hook."""
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    sid = "aabbccdd-1111-2222-3333-444455556666"
    jl = tmp_path / f"{sid}.jsonl"
    _make_jsonl(jl, sid, n_turns=5)

    _stdin(monkeypatch, {
        "prompt": "mm+",
        "session_id": sid,
        "transcript_path": str(jl),
    })
    popen_calls = []
    with patch("marrow.hooks.popen_detach",
               side_effect=lambda a, log_path: popen_calls.append(a)):
        rc = hooks.main(["user_prompt_submit"])
    assert rc == 0

    # Spawn fired
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


def test_mm_plus_named_sid_skips_archive(env, monkeypatch, capsys):
    """mm+ <other-uuid> does NOT archive current session's jsonl."""
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    current_sid = "c0ffee00-0000-0000-0000-000000000000"
    other_sid = "deadbeef-1111-2222-3333-444455556666"
    jl = tmp_path / "current.jsonl"
    _make_jsonl(jl, current_sid, n_turns=3)

    _stdin(monkeypatch, {
        "prompt": f"mm+ {other_sid}",
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

    with patch("marrow.hooks.popen_detach",
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


def test_mm_plus_jsonl_missing_still_spawns(env, monkeypatch, capsys):
    """Non-existent transcript_path silently skipped; spawn still fires."""
    db, tmp_path = env
    (tmp_path / "logs").mkdir(exist_ok=True)
    sid = "missing0-ffff-eeee-dddd-ccccbbbbaaaa"
    nonexistent = str(tmp_path / "no_such_file.jsonl")

    _stdin(monkeypatch, {
        "prompt": "mm+",
        "session_id": sid,
        "transcript_path": nonexistent,
    })
    popen_calls = []
    with patch("marrow.hooks.popen_detach",
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
