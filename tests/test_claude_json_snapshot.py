"""Unit tests for hooks._claude_json_snapshot_block (~/.claude.json backup)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from marrow import config, hooks


def _ticking_datetime():
    """Fake datetime.now() that advances 1s per call, to avoid same-second
    filename collisions when a test snapshots repeatedly in a tight loop."""
    class _Fake:
        _t = datetime(2026, 1, 1, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            cls._t += timedelta(seconds=1)
            return cls._t
    return _Fake


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("marrow.hooks.Path.home", lambda: home)
    monkeypatch.setattr(config, "db_path", lambda: str(tmp_path / "t.db"))
    return home, data_dir


def _snap_dir(data_dir):
    return data_dir / "backups" / "claude-json"


def test_missing_file_returns_none(env):
    home, data_dir = env
    assert hooks._claude_json_snapshot_block() is None
    assert not _snap_dir(data_dir).exists()


def test_first_run_creates_snapshot(env):
    home, data_dir = env
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"a": 1}}))
    line = hooks._claude_json_snapshot_block()
    assert line == "claude.json: snapshot saved (mcpServers changed)"
    snaps = list(_snap_dir(data_dir).glob("claude-json-*.json"))
    assert len(snaps) == 1


def test_unchanged_mcp_no_new_snapshot(env):
    home, data_dir = env
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"a": 1}, "other": 1}))
    hooks._claude_json_snapshot_block()
    snaps_before = list(_snap_dir(data_dir).glob("claude-json-*.json"))
    assert len(snaps_before) == 1

    # Rewrite with a change to a field outside mcpServers — hash unaffected.
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"a": 1}, "other": 2}))
    line = hooks._claude_json_snapshot_block()
    assert line is None
    snaps_after = list(_snap_dir(data_dir).glob("claude-json-*.json"))
    assert len(snaps_after) == 1


def test_changed_mcp_creates_new_snapshot(env, monkeypatch):
    home, data_dir = env
    monkeypatch.setattr(hooks, "datetime", _ticking_datetime())
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"a": 1}}))
    hooks._claude_json_snapshot_block()

    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"a": 2}}))
    line = hooks._claude_json_snapshot_block()
    assert line == "claude.json: snapshot saved (mcpServers changed)"
    snaps = list(_snap_dir(data_dir).glob("claude-json-*.json"))
    assert len(snaps) == 2


def test_prune_keeps_newest_n(env, monkeypatch):
    home, data_dir = env
    monkeypatch.setattr(hooks, "datetime", _ticking_datetime())
    monkeypatch.setattr(config, "load", lambda: {"hooks": {"claude_json_snapshot_keep": 3}})
    for i in range(5):
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"a": i}}))
        hooks._claude_json_snapshot_block()
    snaps = sorted(_snap_dir(data_dir).glob("claude-json-*.json"))
    assert len(snaps) == 3


def test_corrupt_json_alerts_and_skips_snapshot(env, monkeypatch):
    home, data_dir = env
    (home / ".claude.json").write_text("{not valid json")
    mock_alert = MagicMock()
    monkeypatch.setattr(hooks.repo, "add_alert", mock_alert)

    line = hooks._claude_json_snapshot_block()

    assert line == "claude.json: ⚠️ corrupt JSON, snapshot skipped"
    mock_alert.assert_called_once()
    args, kwargs = mock_alert.call_args
    assert args[0] == "warn"
    assert args[1] == "claude_json_corrupt"
    assert not _snap_dir(data_dir).exists()
