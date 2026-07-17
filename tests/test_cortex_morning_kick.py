"""cli morning flag-pull (P6): a real user turn in a NON-cortex session, with the
cortex night flag set and local time past night.morning_start, spawns a detached
cortex.kick(morning). No night flag, or before morning_start, or a cortex/
disabled session -> no kick. The subprocess is mocked — never launch cortex.kick."""
from __future__ import annotations

import json

import pytest

from marrow import config, cortex_bridge


@pytest.fixture()
def env(tmp_path, monkeypatch):
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
        "core": {"timezone": "Australia/Melbourne"},
        "cortex": {
            "enabled": True, "home": str(home),
            "venv_python": str(py), "repo_root": str(root),
            "wake_state_file": "wake_state.json",
            "wake_audit_log_file": "wake_audit.log",
        },
    })
    # No cortex session (cli path). Ensure MARROW_CORTEX is absent.
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    calls = []
    monkeypatch.setattr(cortex_bridge, "kick_cortex",
                        lambda kind, **kw: calls.append({"kind": kind, **kw}))
    return home, tmp_path, calls


def _set_night(home, mode="night"):
    (home / "wake_state.json").write_text(json.dumps({"mode": mode}))


def _set_morning_start(tmp_path, hhmm):
    (tmp_path / "cortex.toml").write_text(f"[night]\nmorning_start = \"{hhmm}\"\n")


def test_morning_kick_fires_when_night_and_past_start(env):
    home, tmp_path, calls = env
    _set_night(home)
    _set_morning_start(tmp_path, "00:00")   # always past
    cortex_bridge.maybe_morning_kick_cli()
    assert [c["kind"] for c in calls] == ["morning"]


def test_no_kick_when_no_night_flag(env):
    home, tmp_path, calls = env
    (home / "wake_state.json").write_text(json.dumps({"awake": True}))  # day
    _set_morning_start(tmp_path, "00:00")
    cortex_bridge.maybe_morning_kick_cli()
    assert calls == []


def test_no_kick_before_morning_start(env):
    home, tmp_path, calls = env
    _set_night(home)
    _set_morning_start(tmp_path, "23:59")   # not yet reached (any local time)
    cortex_bridge.maybe_morning_kick_cli()
    assert calls == []


def test_no_kick_in_cortex_session(env, monkeypatch):
    home, tmp_path, calls = env
    _set_night(home)
    _set_morning_start(tmp_path, "00:00")
    monkeypatch.setenv("MARROW_CORTEX", "1")   # cortex takes its own path
    cortex_bridge.maybe_morning_kick_cli()
    assert calls == []


def test_no_kick_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"enabled": False}})
    calls = []
    monkeypatch.setattr(cortex_bridge, "kick_cortex",
                        lambda kind, **kw: calls.append(kind))
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    cortex_bridge.maybe_morning_kick_cli()
    assert calls == []


def test_night_mode_reader_absent_file(env):
    home, tmp_path, _ = env
    (home / "wake_state.json").unlink(missing_ok=True)
    assert cortex_bridge._cortex_night_mode() is False


def test_kick_cortex_argv(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {"enabled": True, "home": str(home),
                   "venv_python": str(py), "repo_root": str(root),
                   "wake_audit_log_file": "wake_audit.log"},
    })
    monkeypatch.setattr(config, "db_path", lambda: str(tmp_path / "t.db"))
    captured = {}

    class _P:
        def __init__(self, argv, **kw):
            captured["argv"] = argv

    monkeypatch.setattr(cortex_bridge.subprocess, "Popen", _P)
    cortex_bridge.kick_cortex("timeout", note_id=5, minutes=30)
    argv = captured["argv"]
    assert argv[1:] == ["-m", "cortex.kick", "--kind", "timeout",
                        "--note-id", "5", "--minutes", "30"]
