"""T4: lie_down PreToolUse nudge (non-blocking additionalContext, every call)."""
from __future__ import annotations

import io
import json

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
    assert "handoff.md" in hso["additionalContext"]
    assert "permissionDecision" not in hso


def test_nudge_rotate_uses_rotate_copy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _enable_cortex(monkeypatch, tmp_path / "cortex")
    jl = _big_transcript(tmp_path, 10_000)
    # fresh handoff so the deny gate stays open — rotate copy still selected
    home = tmp_path / "cortex"
    home.mkdir(parents=True, exist_ok=True)
    hp = home / "handoff.md"
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


# ── over-threshold rotate hint on a plain lie_down ────────────────────────────

def _patch_cfg(monkeypatch, home, cortex=None, rotate=None):
    real = config.load

    def _patched():
        cfg = dict(real())
        cx = dict(cfg.get("cortex", {}))
        cx["enabled"] = True
        cx["home"] = str(home)
        cx.update(cortex or {})
        cfg["cortex"] = cx
        cr = dict(cfg.get("cortex_rotate", {}))
        cr.update(rotate or {})
        cfg["cortex_rotate"] = cr
        return cfg

    monkeypatch.setattr(config, "load", _patched)


@pytest.fixture()
def hint_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def _nudge(tmp_path, tokens=10_000, rotate=False, tpath=True):
    jl = _big_transcript(tmp_path, tokens)
    inp = {"tool_name": "mcp__marrow__lie_down",
           "tool_input": {"rotate": True} if rotate else {}}
    if tpath:
        inp["transcript_path"] = str(jl)
    return cortex_bridge._cortex_lie_down_nudge(inp)


def test_hint_when_plain_lie_down_over_threshold(hint_env, monkeypatch):
    _patch_cfg(monkeypatch, hint_env / "cortex", rotate={"show_tokens": 5_000})
    out = _nudge(hint_env)
    assert "≥5k" in out
    assert "rotate=True" in out
    assert "Append one line" in out


def test_no_hint_when_rotate_already_requested(hint_env, monkeypatch):
    _patch_cfg(monkeypatch, hint_env / "cortex", rotate={"show_tokens": 5_000})
    out = _nudge(hint_env, rotate=True)
    assert "≥5k" not in out
    assert "before rotate" in out


def test_no_hint_under_threshold(hint_env, monkeypatch):
    _patch_cfg(monkeypatch, hint_env / "cortex", rotate={"show_tokens": 5_000})
    out = _nudge(hint_env, tokens=1_000)
    assert "≥5k" not in out
    assert "Append one line" in out


def test_show_tokens_zero_disables_hint(hint_env, monkeypatch):
    _patch_cfg(monkeypatch, hint_env / "cortex", rotate={"show_tokens": 0})
    out = _nudge(hint_env)
    assert "≥" not in out
    assert "Append one line" in out


def test_hint_stands_alone_when_base_copy_unset(hint_env, monkeypatch):
    _patch_cfg(monkeypatch, hint_env / "cortex",
               cortex={"lie_down_nudge_text": ""}, rotate={"show_tokens": 5_000})
    out = _nudge(hint_env)
    assert out is not None
    assert out.startswith("Current session context ≥5k")
    assert "\n" not in out


def test_no_nudge_at_all_when_both_copies_empty(hint_env, monkeypatch):
    _patch_cfg(monkeypatch, hint_env / "cortex",
               cortex={"lie_down_nudge_text": "",
                       "lie_down_over_threshold_text": ""},
               rotate={"show_tokens": 5_000})
    assert _nudge(hint_env) is None


def test_missing_transcript_path_skips_hint(hint_env, monkeypatch):
    _patch_cfg(monkeypatch, hint_env / "cortex", rotate={"show_tokens": 5_000})
    out = _nudge(hint_env, tpath=False)
    assert "≥5k" not in out
    assert "Append one line" in out


def test_hint_copy_overridable_from_config(hint_env, monkeypatch):
    _patch_cfg(monkeypatch, hint_env / "cortex",
               cortex={"lie_down_over_threshold_text": "rotate now ({show_k}k)"},
               rotate={"show_tokens": 5_000})
    out = _nudge(hint_env)
    assert "rotate now (5k)" in out
