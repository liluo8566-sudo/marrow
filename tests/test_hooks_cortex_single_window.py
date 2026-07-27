"""Cortex-window hook behavior: env-marker session identity (the conversation
is the identity), the PreToolUse no-deny path, and the UserPromptSubmit
wake-branch human-bell recognition (receipt exact-match / shape fallback /
epoch staleness) plus the user-wake reset on an ordinary chat turn.
"""
from __future__ import annotations

import io
import json

import pytest

from marrow import config, cortex_bridge, hooks


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _ctx(capsys):
    out = capsys.readouterr().out
    if not out.strip():
        return ""
    return json.loads(out)["hookSpecificOutput"].get("additionalContext", "")


def _hso(capsys):
    out = capsys.readouterr().out
    if not out.strip():
        return {}
    return json.loads(out)["hookSpecificOutput"]


@pytest.fixture()
def cortex_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    root = tmp_path / "repo"
    root.mkdir()
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {
            "enabled": True, "home": str(home),
            "venv_python": str(py), "repo_root": str(root),
            "wake_state_file": "wake_state.json",
            "watchdog_pidfile": "watchdog.pid",
            "tuck_in_marker": "[TUCK-IN]",
            "machine_markers": ["[NEW ROUND]", "[TUCK-IN]",
                                "[NIGHT]", "[FUSE]", "[CTL]", "[CMD"],
            "compact_markers": ["===== BEGIN ORIGINAL TRANSCRIPT",
                                "===== END ORIGINAL TRANSCRIPT"],
            "compact_marker_head_chars": 200,
            "wake_audit_log_file": "wake_audit.log",
            "handoff_file": "handoff.md",
            "wake_signal_log_file": "state/wake_signal.log",
        },
        "outbox": {"inject_header": "📮 Message from {channel}·{sid4} {time}"},
        "recall": {"exclude_cwds": []},
        "replay": {"enabled": False},
    })
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(cortex_bridge, "_spawn_watchdog_if_absent", lambda: None)
    return home, db


def _ws(home):
    p = home / "wake_state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _write_ws(home, **fields):
    (home / "wake_state.json").write_text(json.dumps(fields))


def _jsonl(tmp_path, name):
    p = tmp_path / f"{name}.jsonl"
    p.write_text("")
    return str(p)


# --- cortex-session identity (env marker = conversation identity) -------------

def test_is_cortex_session_env_only_true(cortex_env):
    """MARROW_CORTEX set -> cortex session regardless of transcript."""
    assert cortex_bridge.is_cortex_session() is True
    assert cortex_bridge.is_cortex_session("/anything.jsonl") is True


def test_is_cortex_session_cli_window_not_cortex(cortex_env, tmp_path, monkeypatch):
    """A plain cli window: no env marker -> never cortex."""
    home, _ = cortex_env
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    tpath = _jsonl(tmp_path, "cli")
    assert cortex_bridge.is_cortex_session(tpath) is False


def test_is_cortex_session_no_tpath_no_env_not_cortex(cortex_env, monkeypatch):
    """No env marker and no transcript to match -> env-only, so not cortex."""
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    assert cortex_bridge.is_cortex_session() is False
    assert cortex_bridge.is_cortex_session(None) is False


# --- PreToolUse hook integration: cortex tools never denied -------------------

def test_pretool_cortex_lie_down_not_denied(cortex_env, tmp_path, monkeypatch, capsys):
    """A cortex window's lie_down is not blocked by the PreToolUse gate."""
    home, _ = cortex_env
    tpath = _jsonl(tmp_path, "resident")
    _stdin(monkeypatch, {
        "transcript_path": tpath, "session_id": "s1",
        "tool_name": "mcp__marrow__lie_down", "tool_input": {"next_wake_min": 30},
    })
    assert hooks.main(["pretool_use"]) == 0
    assert _hso(capsys).get("permissionDecision") != "deny"


def test_pretool_cortex_monitor_not_denied(cortex_env, tmp_path, monkeypatch, capsys):
    """Arming a Monitor on the wake-signal path is not denied."""
    home, _ = cortex_env
    tpath = _jsonl(tmp_path, "resident")
    signal_log = str(home / "state" / "wake_signal.log")
    _stdin(monkeypatch, {
        "transcript_path": tpath, "session_id": "s1", "tool_name": "Monitor",
        "tool_input": {"command": f"tail -n 0 -f {signal_log}"},
    })
    assert hooks.main(["pretool_use"]) == 0
    assert _hso(capsys).get("permissionDecision") != "deny"


# --- UserPromptSubmit wake-branch: receipt bell + note injection --------------

def test_wake_turn_registered_window_gets_normal_payload(cortex_env, tmp_path, monkeypatch, capsys):
    home, _ = cortex_env
    tpath = _jsonl(tmp_path, "resident")
    _write_ws(home)
    (home / "wakeup_note.md").write_text("## cli\nnote body here")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _stdin(monkeypatch, {
        "session_id": "s1", "transcript_path": tpath,
        "prompt": "☀️ 09:00",  # human-text bell -> shape fallback (no receipt)
    })
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "note body here" in _ctx(capsys)


def test_wake_turn_receipt_bell_injects_note_and_consumes_receipt(cortex_env, tmp_path, monkeypatch, capsys):
    """New human-text bell ('☀️ 09:00') matched via the wake_state receipt:
    the note is injected and the receipt is consumed (one-shot)."""
    home, _ = cortex_env
    from datetime import datetime, timezone
    tpath = _jsonl(tmp_path, "resident")
    _write_ws(home, gen=5, state_id="cafe",
              wake_receipt={"text": "☀️ 09:00", "gen": 5, "state_id": "cafe",
                            "rearm": False,
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "template_prefix": "☀️ "})
    (home / "wakeup_note.md").write_text("## cli\nnote body here")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _stdin(monkeypatch, {
        "session_id": "s1", "transcript_path": tpath, "prompt": "☀️ 09:00",
    })
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "note body here" in _ctx(capsys)
    assert "wake_receipt" not in _ws(home)  # consumed


def test_wake_turn_shape_fallback_injects_note_when_receipt_missing(cortex_env, tmp_path, monkeypatch, capsys):
    """Receipt gone but the on-screen line matches the template shape -> fail
    OPEN: the note is still injected (never drop a genuine wake)."""
    home, _ = cortex_env
    tpath = _jsonl(tmp_path, "resident")
    _write_ws(home)  # no receipt
    (home / "wakeup_note.md").write_text("## cli\nnote body here")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _stdin(monkeypatch, {
        "session_id": "s1", "transcript_path": tpath, "prompt": "☀️ 09:00",
    })
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "note body here" in _ctx(capsys)


def test_wake_turn_receipt_stale_epoch_suppressed(cortex_env, tmp_path, monkeypatch, capsys):
    """A receipt-matched bell whose epoch token is stale (a newer epoch
    superseded it) suppresses the note — no injection."""
    home, _ = cortex_env
    from datetime import datetime, timezone
    tpath = _jsonl(tmp_path, "resident")
    # Live epoch gen=9 but the receipt token is gen=5 -> stale.
    _write_ws(home, gen=9, state_id="cafe",
              wake_receipt={"text": "☀️ 09:00", "gen": 5, "state_id": "cafe",
                            "rearm": False,
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "template_prefix": "☀️ "})
    (home / "wakeup_note.md").write_text("## cli\nnote body here")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _stdin(monkeypatch, {
        "session_id": "s1", "transcript_path": tpath, "prompt": "☀️ 09:00",
    })
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""  # suppressed


def test_chat_turn_registered_window_still_resets(cortex_env, tmp_path, monkeypatch, capsys):
    home, _ = cortex_env
    tpath = _jsonl(tmp_path, "resident")
    _write_ws(home, awake=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _stdin(monkeypatch, {
        "session_id": "s1", "transcript_path": tpath,
        "prompt": "hey are you there?",
    })
    assert hooks.main(["user_prompt_submit"]) == 0
    d = _ws(home)
    assert d.get("awake") is True  # registered window -> normal reset fires
