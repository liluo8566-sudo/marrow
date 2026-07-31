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


TG_WAKE_NOTE = "\u23f3 [NEW ROUND] 87 min since the user\u2019s last message."


def test_show_fires_on_a_machine_turn_over_threshold(tmp_path, monkeypatch, capsys):
    """The tg wake note is a machine turn -> over threshold it carries the nudge."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, home=tmp_path / "cortex")
    show = config.load()["cortex_rotate"]["show_tokens"]
    jl = _transcript(tmp_path, show + 1)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl),
                         "prompt": TG_WAKE_NOTE})
    assert hooks.main(["turn_inject"]) == 0
    assert "lie_down(rotate=True)" in _ctx(capsys)


def test_show_fires_when_the_prompt_is_absent(tmp_path, monkeypatch, capsys):
    """No prompt in the payload -> treated as machine (fail-open toward nudging)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, home=tmp_path / "cortex")
    show = config.load()["cortex_rotate"]["show_tokens"]
    jl = _transcript(tmp_path, show + 1)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "lie_down(rotate=True)" in _ctx(capsys)


def test_show_held_on_a_real_user_turn(tmp_path, monkeypatch, capsys):
    """A real user turn never carries the nudge, however full the window."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, home=tmp_path / "cortex")
    show = config.load()["cortex_rotate"]["show_tokens"]
    jl = _transcript(tmp_path, show + 50_000)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl),
                         "prompt": "did the [NEW ROUND] path fire?"})
    assert hooks.main(["turn_inject"]) == 0
    assert "lie_down(rotate=True)" not in _ctx(capsys)


def test_show_silent_below_threshold(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, home=tmp_path / "cortex")
    show = config.load()["cortex_rotate"]["show_tokens"]
    jl = _transcript(tmp_path, show - 1000)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl),
                         "prompt": TG_WAKE_NOTE})
    assert hooks.main(["turn_inject"]) == 0
    assert "lie_down(rotate=True)" not in _ctx(capsys)


def test_show_off_when_threshold_is_zero(tmp_path, monkeypatch):
    """show_tokens = 0 is the off-switch, machine turn or not."""
    from marrow import cortex_bridge
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, home=tmp_path / "cortex")
    monkeypatch.setattr(cortex_bridge, "_show_tokens", lambda: 0)
    jl = _transcript(tmp_path, 900_000)
    assert cortex_bridge._cortex_show_context(str(jl), TG_WAKE_NOTE) == ""


def test_show_absent_for_normal_session(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch)
    show = config.load()["cortex_rotate"]["show_tokens"]
    jl = _transcript(tmp_path, show + 50_000)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl),
                         "prompt": TG_WAKE_NOTE})
    assert hooks.main(["turn_inject"]) == 0
    assert "lie_down(rotate=True)" not in _ctx(capsys)


def test_tg_wake_note_opener_is_a_machine_line():
    """The real tg wake-note opening line must classify as machine — the whole
    turn-type gate rests on it."""
    from marrow import cortex_bridge
    assert cortex_bridge.is_machine_line(TG_WAKE_NOTE) is True


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
