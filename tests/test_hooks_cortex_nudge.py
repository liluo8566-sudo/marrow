"""T4: lie_down PreToolUse nudge (non-blocking additionalContext, every call)."""
from __future__ import annotations

import io
import json
import time

import pytest

from marrow import config, cortex_bridge, hooks


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _out(capsys):
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"] if out.strip() else {}


def _enable_cortex(monkeypatch, home):
    real = config.load

    def _patched():
        cfg = dict(real())
        cx = dict(cfg.get("cortex", {}))
        cx["enabled"] = True
        cx["home"] = str(home)
        cfg["cortex"] = cx
        return cfg

    monkeypatch.setattr(config, "load", _patched)


def _big_transcript(tmp_path, occupancy, spawn_ts="2026-07-08T10:00:00+00:00"):
    jl = tmp_path / "big.jsonl"
    jl.write_text("\n".join([
        json.dumps({"timestamp": spawn_ts, "type": "user"}),
        json.dumps({"message": {"usage": {"input_tokens": occupancy}}}),
    ]))
    return jl


def test_nudge_plain_lie_down(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, tmp_path / "cortex")
    jl = _big_transcript(tmp_path, 10_000)
    _stdin(monkeypatch, {"tool_name": "mcp__marrow__lie_down",
                         "transcript_path": str(jl), "tool_input": {}})
    assert hooks.main(["pretool_use"]) == 0
    hso = _out(capsys)
    want = config.load()["cortex"]["lie_down_nudge_text"].split("{handoff}")[0]
    assert want in hso["additionalContext"]
    assert "handoff-cli.md" in hso["additionalContext"]
    assert "permissionDecision" not in hso


def test_nudge_rotate_uses_rotate_copy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, tmp_path / "cortex")
    jl = _big_transcript(tmp_path, 10_000)
    # fresh handoff so the deny gate stays open — rotate copy still selected
    home = tmp_path / "cortex"
    home.mkdir(parents=True, exist_ok=True)
    hp = home / "handoff-cli.md"
    hp.write_text("note", encoding="utf-8")
    _stdin(monkeypatch, {"tool_name": "mcp__marrow__lie_down",
                         "transcript_path": str(jl), "tool_input": {"rotate": True}})
    assert hooks.main(["pretool_use"]) == 0
    hso = _out(capsys)
    rotate_copy = config.load()["cortex"]["lie_down_nudge_rotate_text"]
    assert rotate_copy.split("{handoff}")[0] in hso["additionalContext"]
    assert "before rotate" in hso["additionalContext"]


def test_rotate_without_handoff_is_nudged_never_denied(tmp_path, monkeypatch, capsys):
    """No handoff on disk and rotate=True: the call is still ALLOWED (the deny
    guard is gone) and carries only the rotate nudge."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, tmp_path / "cortex")
    jl = _big_transcript(tmp_path, 10_000)
    _stdin(monkeypatch, {"tool_name": "mcp__marrow__lie_down",
                         "transcript_path": str(jl), "tool_input": {"rotate": True}})
    assert hooks.main(["pretool_use"]) == 0
    hso = _out(capsys)
    assert "permissionDecision" not in hso
    assert "before rotate" in hso["additionalContext"]


def test_no_nudge_for_non_cortex(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, tmp_path / "cortex")
    jl = _big_transcript(tmp_path, 10_000)
    _stdin(monkeypatch, {"tool_name": "mcp__marrow__lie_down",
                         "transcript_path": str(jl), "tool_input": {}})
    assert hooks.main(["pretool_use"]) == 0
    hso = _out(capsys)
    assert "Append one line" not in hso.get("additionalContext", "")


def test_nudge_helper_none_for_other_tool():
    inp = {"tool_name": "mcp__marrow__say", "tool_input": {}}
    assert cortex_bridge._cortex_lie_down_nudge(inp) is None
