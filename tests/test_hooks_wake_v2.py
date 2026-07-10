"""Wake-pipeline v2 injections in hooks (cortex window only):
- SessionStart arm line (fresh window)
- UserPromptSubmit wake-turn full-note inject
- UserPromptSubmit monitor-death rearm inject
"""
from __future__ import annotations

import io
import json

import pytest

from marrow import config, cortex_bridge, hooks, storage


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _ctx(capsys):
    out = capsys.readouterr().out
    if not out.strip():
        return ""
    return json.loads(out)["hookSpecificOutput"].get("additionalContext", "")


def _enable(monkeypatch, tmp_path, extra=None):
    real = config.load

    def _patched():
        cfg = dict(real())
        cx = dict(cfg.get("cortex", {}))
        cx["enabled"] = True
        cx["home"] = str(tmp_path)
        if extra:
            cx.update(extra)
        cfg["cortex"] = cx
        return cfg

    monkeypatch.setattr(config, "load", _patched)


# ── Item 2: wake-turn full-note inject ────────────────────────────────────────

def test_wake_turn_injects_full_note(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("read me and act", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _stdin(monkeypatch, {"session_id": "s1",
                         "prompt": "[CORTEX-WAKE] 2026-07-11 14:00 wake"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == "read me and act"


def test_wake_turn_missing_note_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "[CORTEX-WAKE] wake"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""


def test_ordinary_chat_no_note_inject(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    (tmp_path / "wakeup_note.md").write_text("secret note", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "今天过得怎么样"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "secret note" not in _ctx(capsys)


def test_non_cortex_session_no_wake_inject(tmp_path, monkeypatch, capsys):
    """No MARROW_CORTEX => the whole cortex branch is skipped."""
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    (tmp_path / "wakeup_note.md").write_text("note", encoding="utf-8")
    _enable(monkeypatch, tmp_path, {"wake_marker": "[CORTEX-WAKE]"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "[CORTEX-WAKE] wake"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "note" not in _ctx(capsys)


# ── Item 3: monitor-death rearm inject ────────────────────────────────────────

_DEATH = ('<task-notification>\n<summary>Monitor event: "ear"</summary>\n'
          '<event>[Monitor stopped — too much output.]</event>\n'
          '</task-notification>')


def test_monitor_death_injects_rearm(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path,
            {"rearm_text": "rearm: tail {signal_log}"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": _DEATH})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == f"rearm: tail {tmp_path/'wake_signal.log'}"


def test_monitor_death_silent_on_normal_chat(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {"rearm_text": "rearm {signal_log}"})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": "Monitor 工具怎么用啊"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "rearm" not in _ctx(capsys)


def test_monitor_death_blank_text_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _enable(monkeypatch, tmp_path, {"rearm_text": ""})
    _stdin(monkeypatch, {"session_id": "s1", "prompt": _DEATH})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert _ctx(capsys) == ""


# ── Item 1: SessionStart arm line (fresh cortex window) ───────────────────────

def _ss_db(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    storage.init_db(db).close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db


def test_arm_line_injected_fresh_cortex_window(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path, {"arm_ear_text": "arm: tail {signal_log}"})
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    _stdin(monkeypatch, {"session_id": "fresh1", "cwd": str(tmp_path),
                         "transcript_path": str(jl)})
    assert hooks.main(["session_start"]) == 0
    assert f"arm: tail {tmp_path/'wake_signal.log'}" in _ctx(capsys)


def test_arm_line_blank_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path, {"arm_ear_text": ""})
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    _stdin(monkeypatch, {"session_id": "fresh2", "cwd": str(tmp_path),
                         "transcript_path": str(jl)})
    assert hooks.main(["session_start"]) == 0
    assert "arm:" not in _ctx(capsys)


def test_arm_line_skipped_non_cortex(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    _ss_db(tmp_path, monkeypatch)
    _enable(monkeypatch, tmp_path, {"arm_ear_text": "arm: tail {signal_log}"})
    jl = tmp_path / "s.jsonl"
    jl.write_text("", encoding="utf-8")
    _stdin(monkeypatch, {"session_id": "fresh3", "cwd": str(tmp_path),
                         "transcript_path": str(jl)})
    assert hooks.main(["session_start"]) == 0
    assert "arm:" not in _ctx(capsys)
