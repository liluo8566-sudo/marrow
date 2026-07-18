"""Cortex window-occupancy 亮牌 in turn_inject (MARROW_CORTEX only)."""
from __future__ import annotations

import io
import json

import pytest

from marrow import config, hooks


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _transcript(tmp_path, total_tokens: int):
    """One assistant line whose usage sums to total_tokens (all in input)."""
    jl = tmp_path / "session.jsonl"
    jl.write_text(json.dumps({
        "message": {"role": "assistant", "usage": {
            "input_tokens": total_tokens, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "output_tokens": 0}}
    }) + "\n")
    return jl


def _ctx(capsys):
    out = capsys.readouterr().out
    if not out.strip():
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _enable_cortex(monkeypatch, home=None):
    """turn_inject's 亮牌 injection is gated on [cortex].enabled; force it on so
    these MARROW_CORTEX contract tests exercise the active path. When *home* is
    given, route the cortex home (wake_state) there so tests never touch the real
    ~/.config/marrow/cortex tree."""
    real = config.load

    def _patched():
        cfg = dict(real())
        cx = dict(cfg.get("cortex", {}))
        cx["enabled"] = True
        if home is not None:
            cx["home"] = str(home)
        cfg["cortex"] = cx
        return cfg

    monkeypatch.setattr(config, "load", _patched)


def _write_wake_state(home, **fields):
    """Write a wake_state.json under the cortex *home*/state/ for presence-gate tests."""
    import json as _json
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "wake_state.json").write_text(_json.dumps(fields))


def test_show_fires_over_threshold(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, home=tmp_path / "cortex")
    show = config.load()["cortex_rotate"]["show_tokens"]
    jl = _transcript(tmp_path, show + 1)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "lie_down(rotate=True)" in _ctx(capsys)


def test_show_silent_below_threshold(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, home=tmp_path / "cortex")
    show = config.load()["cortex_rotate"]["show_tokens"]
    jl = _transcript(tmp_path, show - 1000)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "lie_down(rotate=True)" not in _ctx(capsys)


def test_show_absent_for_normal_session(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch)
    show = config.load()["cortex_rotate"]["show_tokens"]
    jl = _transcript(tmp_path, show + 50_000)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "lie_down(rotate=True)" not in _ctx(capsys)


def test_show_held_when_user_active(tmp_path, monkeypatch, capsys):
    """FIX 3 presence gate: over-threshold but the user's last real message is
    younger than show_silent_min -> hold the nudge (mid-chat, fuse is backstop)."""
    from datetime import datetime, timezone
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    home = tmp_path / "cortex"
    _enable_cortex(monkeypatch, home=home)
    recent = datetime.now(timezone.utc).isoformat()
    _write_wake_state(home, last_user_msg_ts=recent, user_replied_this_wake=True)
    show = config.load()["cortex_rotate"]["show_tokens"]
    jl = _transcript(tmp_path, show + 1)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "lie_down(rotate=True)" not in _ctx(capsys)


def test_show_fires_when_user_silent_past_threshold(tmp_path, monkeypatch, capsys):
    """FIX 3 presence gate: over-threshold and the user has been silent past
    show_silent_min -> inject once (the boolean user_replied_this_wake alone must
    no longer suppress it)."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    home = tmp_path / "cortex"
    _enable_cortex(monkeypatch, home=home)
    silent = config.load()["cortex_rotate"]["show_silent_min"]
    old = (datetime.now(timezone.utc) - timedelta(minutes=silent + 5)).isoformat()
    _write_wake_state(home, last_user_msg_ts=old, user_replied_this_wake=True)
    show = config.load()["cortex_rotate"]["show_tokens"]
    jl = _transcript(tmp_path, show + 1)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "lie_down(rotate=True)" in _ctx(capsys)


def test_window_tokens_parser_sums_last_usage(tmp_path):
    jl = tmp_path / "s.jsonl"
    jl.write_text(
        json.dumps({"message": {"usage": {"input_tokens": 10}}}) + "\n"
        + json.dumps({"message": {"usage": {
            "input_tokens": 100, "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 5, "output_tokens": 3}}}) + "\n"
    )
    assert hooks._window_tokens_from_transcript(str(jl)) == 128


def test_window_tokens_missing_transcript_is_zero():
    assert hooks._window_tokens_from_transcript("/no/such/file.jsonl") == 0
