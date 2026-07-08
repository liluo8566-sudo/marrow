"""goal/wish MCP tools (C3 marrow-side plumbing) + recall cortex guard."""
from __future__ import annotations

import pytest

from marrow import config, daemon, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    storage.init_db(db).close()
    monkeypatch.setattr(daemon, "_DB", db)
    monkeypatch.setattr(config, "db_path", lambda: db)
    return db, tmp_path


def test_goal_set_creates_row(env):
    out = daemon.goal("set", "sleep", "8", "h")
    assert out == {"ok": True, "key": "sleep", "value": "8", "unit": "h"}
    rows = daemon.goal("list")
    assert rows == [{"key": "sleep", "value": "8", "unit": "h",
                      "updated_at": rows[0]["updated_at"]}]


def test_goal_set_updates_existing_key(env):
    daemon.goal("set", "sleep", "7", "h")
    daemon.goal("set", "sleep", "8", "h")
    rows = daemon.goal("list")
    assert len(rows) == 1
    assert rows[0]["value"] == "8"


def test_goal_set_requires_key_and_value(env):
    assert daemon.goal("set", "", "8")["ok"] is False
    assert daemon.goal("set", "sleep", "")["ok"] is False


def test_goal_list_multiple_sorted(env):
    daemon.goal("set", "sleep", "8", "h")
    daemon.goal("set", "exercise", "3", "x/week")
    rows = daemon.goal("list")
    assert [r["key"] for r in rows] == ["exercise", "sleep"]


def test_goal_delete_removes_key(env):
    daemon.goal("set", "sleep", "8", "h")
    out = daemon.goal("delete", "sleep")
    assert out == {"ok": True, "key": "sleep", "deleted": True}
    assert daemon.goal("list") == []


def test_goal_delete_missing_key_reports_not_deleted(env):
    out = daemon.goal("delete", "nope")
    assert out == {"ok": True, "key": "nope", "deleted": False}


def test_goal_unknown_action(env):
    out = daemon.goal("nope")
    assert out["ok"] is False


def test_wish_creates_file_with_header(env, tmp_path, monkeypatch):
    home = tmp_path / "cortex"
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"home": str(home)}})
    out = daemon.wish("新出的那个奶茶")
    assert out["ok"] is True
    path = home / "wishlist.md"
    assert path.exists()
    assert out["path"] == str(path)
    text = path.read_text(encoding="utf-8")
    assert "# Wishlist" in text
    assert "新出的那个奶茶" in text
    assert out["line"] in text


def test_wish_appends_never_touches_prior_lines(env, tmp_path, monkeypatch):
    home = tmp_path / "cortex"
    home.mkdir(parents=True)
    wishlist = home / "wishlist.md"
    wishlist.write_text("# Wishlist\n\n- 2026-01-01 her own hand-written note\n",
                         encoding="utf-8")
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"home": str(home)}})
    daemon.wish("second wish")
    text = wishlist.read_text(encoding="utf-8")
    assert "her own hand-written note" in text
    assert "second wish" in text
    assert text.index("her own hand-written note") < text.index("second wish")


def test_wish_requires_text(env, tmp_path, monkeypatch):
    home = tmp_path / "cortex"
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"home": str(home)}})
    assert daemon.wish("")["ok"] is False
    assert not home.exists() or not (home / "wishlist.md").exists()


def test_wish_uses_explicit_wishlist_path(env, tmp_path, monkeypatch):
    target = tmp_path / "somewhere" / "my-wishes.md"
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {"home": str(tmp_path / "cortex"), "wishlist_path": str(target)},
    })
    daemon.wish("custom path wish")
    assert target.exists()
    assert "custom path wish" in target.read_text(encoding="utf-8")


def test_recall_allowed_under_marrow_cortex(env, monkeypatch):
    """B3m (07-08): cortex's resumed session gets full memory parity — the
    recall MCP tool works the same as any other session (no hard block)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    assert daemon.recall("anything") == []


# ── cortex lie_down / say tools ────────────────────────────────────────────────

def test_cortex_tools_hidden_without_marrow_cortex():
    """Without MARROW_CORTEX at import time the tools do not register into the
    MCP schema. _CORTEX is captured at import; the test suite runs plain, so it
    must be False and neither tool is in the tool manager."""
    assert daemon._CORTEX is False
    names = set(daemon.mcp._tool_manager._tools.keys())
    assert "lie_down" not in names
    assert "say" not in names


def test_lie_down_runs_module_from_any_cwd(env, monkeypatch, tmp_path):
    """lie_down subprocess is invoked with cwd=repo_root and `-m cortex.lie_down`,
    independent of the caller's cwd (the original slash-command bug)."""
    monkeypatch.chdir("/tmp")
    fake_py = tmp_path / "venv" / "bin" / "python"
    fake_root = tmp_path / "cortex-repo"
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {"venv_python": str(fake_py), "repo_root": str(fake_root)},
    })
    captured = {}

    class _P:
        returncode = 0
        stdout = "lie_down tokens=42 cleared_due=0 rotated=False force_slept=None"
        stderr = ""

    def _fake_run(cmd, cwd=None, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return _P()

    monkeypatch.setattr(daemon.subprocess, "run", _fake_run)
    out = daemon.lie_down.fn() if hasattr(daemon.lie_down, "fn") else daemon.lie_down()
    assert out["ok"] is True
    assert captured["cwd"] == str(fake_root)
    assert captured["cmd"][0] == str(fake_py)
    assert captured["cmd"][1:] == ["-m", "cortex.lie_down"]


def test_say_runs_module(env, monkeypatch, tmp_path):
    fake_py = tmp_path / "python"
    fake_root = tmp_path / "repo"
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {"venv_python": str(fake_py), "repo_root": str(fake_root)},
    })
    captured = {}

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, cwd=None, **kw):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(daemon.subprocess, "run", _fake_run)
    out = daemon.say.fn() if hasattr(daemon.say, "fn") else daemon.say()
    assert out["ok"] is True
    assert captured["cmd"][1:] == ["-m", "cortex.say"]


def test_cortex_tool_not_configured(env, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {"cortex": {}})
    run_fn = daemon.lie_down.fn if hasattr(daemon.lie_down, "fn") else daemon.lie_down
    out = run_fn()
    assert out["ok"] is False
    assert "not configured" in out["error"]


def test_cortex_tool_surfaces_stderr(env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {"venv_python": str(tmp_path / "py"), "repo_root": str(tmp_path)},
    })

    class _P:
        returncode = 1
        stdout = ""
        stderr = "ModuleNotFoundError: No module named 'cortex'"

    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: _P())
    run_fn = daemon.say.fn if hasattr(daemon.say, "fn") else daemon.say
    out = run_fn()
    assert out["ok"] is False
    assert "ModuleNotFoundError" in out["error"]
