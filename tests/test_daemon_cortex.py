"""goal/wish MCP tools (C3 marrow-side plumbing) + recall cortex guard."""
from __future__ import annotations

import pytest

from marrow import config, cortex_bridge, daemon, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    storage.init_db(db).close()
    monkeypatch.setattr(daemon, "_DB", db)
    monkeypatch.setattr(cortex_bridge, "_DB", db)
    monkeypatch.setattr(config, "db_path", lambda: db)
    return db, tmp_path


def test_goal_set_creates_row(env):
    out = cortex_bridge.goal("set", "sleep", "8", "h")
    assert out == {"ok": True, "key": "sleep", "value": "8", "unit": "h"}
    rows = cortex_bridge.goal("list")
    assert rows == [{"key": "sleep", "value": "8", "unit": "h",
                      "updated_at": rows[0]["updated_at"]}]


def test_goal_set_updates_existing_key(env):
    cortex_bridge.goal("set", "sleep", "7", "h")
    cortex_bridge.goal("set", "sleep", "8", "h")
    rows = cortex_bridge.goal("list")
    assert len(rows) == 1
    assert rows[0]["value"] == "8"


def test_goal_set_requires_key_and_value(env):
    assert cortex_bridge.goal("set", "", "8")["ok"] is False
    assert cortex_bridge.goal("set", "sleep", "")["ok"] is False


def test_goal_list_multiple_sorted(env):
    cortex_bridge.goal("set", "sleep", "8", "h")
    cortex_bridge.goal("set", "exercise", "3", "x/week")
    rows = cortex_bridge.goal("list")
    assert [r["key"] for r in rows] == ["exercise", "sleep"]


def test_goal_delete_removes_key(env):
    cortex_bridge.goal("set", "sleep", "8", "h")
    out = cortex_bridge.goal("delete", "sleep")
    assert out == {"ok": True, "key": "sleep", "deleted": True}
    assert cortex_bridge.goal("list") == []


def test_goal_delete_missing_key_reports_not_deleted(env):
    out = cortex_bridge.goal("delete", "nope")
    assert out == {"ok": True, "key": "nope", "deleted": False}


def test_goal_unknown_action(env):
    out = cortex_bridge.goal("nope")
    assert out["ok"] is False


def test_wish_creates_file_with_header(env, tmp_path, monkeypatch):
    home = tmp_path / "cortex"
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"home": str(home)}})
    out = cortex_bridge.wish("新出的那个奶茶")
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
    cortex_bridge.wish("second wish")
    text = wishlist.read_text(encoding="utf-8")
    assert "her own hand-written note" in text
    assert "second wish" in text
    assert text.index("her own hand-written note") < text.index("second wish")


def test_wish_requires_text(env, tmp_path, monkeypatch):
    home = tmp_path / "cortex"
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"home": str(home)}})
    assert cortex_bridge.wish("")["ok"] is False
    assert not home.exists() or not (home / "wishlist.md").exists()


def test_wish_uses_explicit_wishlist_path(env, tmp_path, monkeypatch):
    target = tmp_path / "somewhere" / "my-wishes.md"
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {"home": str(tmp_path / "cortex"), "wishlist_path": str(target)},
    })
    cortex_bridge.wish("custom path wish")
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
    assert cortex_bridge._CORTEX is False
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

    monkeypatch.setattr(cortex_bridge.subprocess, "run", _fake_run)
    out = cortex_bridge.lie_down()
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

    monkeypatch.setattr(cortex_bridge.subprocess, "run", _fake_run)
    out = cortex_bridge.say()
    assert out["ok"] is True
    assert captured["cmd"][1:] == ["-m", "cortex.say"]


def test_cortex_tool_not_configured(env, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {"cortex": {}})
    run_fn = cortex_bridge.lie_down
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

    monkeypatch.setattr(cortex_bridge.subprocess, "run", lambda *a, **k: _P())
    run_fn = cortex_bridge.say
    out = run_fn()
    assert out["ok"] is False
    assert "ModuleNotFoundError" in out["error"]


# ── [cortex].enabled master switch ─────────────────────────────────────────────

from mcp.server.fastmcp import FastMCP


def _fresh_mcp():
    m = FastMCP("t")

    def marrow_tool():
        return m.tool(meta={"anthropic/alwaysLoad": True})

    return m, marrow_tool


def _force_enabled(monkeypatch, value, extra=None):
    real = config.load

    def _patched():
        cfg = dict(real())
        cx = dict(cfg.get("cortex", {}))
        cx["enabled"] = value
        if extra:
            cx.update(extra)
        cfg["cortex"] = cx
        return cfg

    monkeypatch.setattr(config, "load", _patched)


def test_switch_off_registers_no_tools(monkeypatch):
    """enabled=false => register() installs none of the six cortex tools."""
    _force_enabled(monkeypatch, False)
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    m, mt = _fresh_mcp()
    cortex_bridge.register(mt)
    assert set(m._tool_manager._tools.keys()) == set()


def test_switch_on_registers_wish_first_goal(monkeypatch):
    """enabled=true (no MARROW_CORTEX) => wish/first/goal for all sessions;
    lie_down/wait/say stay absent (cortex-session inner gate)."""
    _force_enabled(monkeypatch, True)
    m, mt = _fresh_mcp()
    # _CORTEX is the import-time capture; force the non-cortex case explicitly.
    monkeypatch.setattr(cortex_bridge, "_CORTEX", False)
    cortex_bridge.register(mt)
    names = set(m._tool_manager._tools.keys())
    assert {"wish", "first", "goal"} <= names
    assert "lie_down" not in names and "wait" not in names and "say" not in names


def test_switch_on_cortex_session_registers_all_six(monkeypatch):
    """enabled=true AND cortex session (_CORTEX) => all six tools register."""
    _force_enabled(monkeypatch, True)
    m, mt = _fresh_mcp()
    monkeypatch.setattr(cortex_bridge, "_CORTEX", True)
    cortex_bridge.register(mt)
    names = set(m._tool_manager._tools.keys())
    assert {"wish", "first", "goal", "lie_down", "wait", "say"} <= names


def test_switch_off_show_context_gated_empty(monkeypatch, tmp_path):
    """The turn_inject 亮牌 helper itself still checks MARROW_CORTEX; with the
    switch off the hook call site never invokes it (call-site gate), and even if
    invoked without MARROW_CORTEX it returns empty."""
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    assert cortex_bridge._cortex_show_context(str(tmp_path / "none.jsonl")) == ""


def test_switch_off_lie_down_deny_inactive(monkeypatch):
    """lie_down deny helper is inert without a cortex session; and enabled=false
    means the PreToolUse call site never reaches it at all."""
    _force_enabled(monkeypatch, False)
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    inp = {"tool_name": "mcp__marrow__lie_down", "tool_input": {"rotate": True}}
    assert cortex_bridge._cortex_lie_down_deny(inp) is None
