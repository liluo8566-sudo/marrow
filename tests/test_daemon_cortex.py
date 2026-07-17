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
    out = cortex_bridge.lie_down(next_wake_min=20)
    assert out["ok"] is True
    assert captured["cwd"] == str(fake_root)
    assert captured["cmd"][0] == str(fake_py)
    # next_wake_min is required and always threaded into the CLI args.
    assert captured["cmd"][1:] == ["-m", "cortex.lie_down",
                                   "--next-wake-min", "20"]


def _fake_lie_down_run(monkeypatch, tmp_path, stdout):
    fake_py = tmp_path / "python"
    fake_root = tmp_path / "repo"
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {"venv_python": str(fake_py), "repo_root": str(fake_root)},
    })

    class _P:
        returncode = 0
        stderr = ""

    _P.stdout = stdout
    monkeypatch.setattr(cortex_bridge.subprocess, "run",
                        lambda cmd, cwd=None, **kw: _P())


def test_lie_down_surfaces_next_wake(env, monkeypatch, tmp_path):
    """next_wake in the subprocess JSON is echoed into the tool's text."""
    _fake_lie_down_run(monkeypatch, tmp_path,
                       '{"tokens": 42, "next_wake": "14:35"}')
    out = cortex_bridge.lie_down(next_wake_min=20)
    assert out["ok"] is True
    assert out["next_wake"] == "14:35"
    assert out["text"] == "next wake ≈ 14:35"


def test_lie_down_no_next_wake_field(env, monkeypatch, tmp_path):
    """Old cortex build (no next_wake) — no crash, no next_wake surfaced."""
    _fake_lie_down_run(monkeypatch, tmp_path, '{"tokens": 42}')
    out = cortex_bridge.lie_down(next_wake_min=20)
    assert out["ok"] is True
    assert "next_wake" not in out


def test_lie_down_non_json_stdout(env, monkeypatch, tmp_path):
    """Non-JSON stdout (legacy plain line) tolerated silently."""
    _fake_lie_down_run(monkeypatch, tmp_path, "lie_down tokens=42 rotated=False")
    out = cortex_bridge.lie_down(next_wake_min=20)
    assert out["ok"] is True
    assert "next_wake" not in out


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
    out = cortex_bridge.lie_down(next_wake_min=20)
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


def test_switch_on_registers_wish_only(monkeypatch):
    """enabled=true (no MARROW_CORTEX) => wish for all sessions; first/goal are
    pending (not registered anywhere yet); lie_down/wait/say stay absent
    (cortex-session inner gate)."""
    _force_enabled(monkeypatch, True)
    m, mt = _fresh_mcp()
    # _CORTEX is the import-time capture; force the non-cortex case explicitly.
    monkeypatch.setattr(cortex_bridge, "_CORTEX", False)
    cortex_bridge.register(mt)
    names = set(m._tool_manager._tools.keys())
    assert "wish" in names
    assert "first" not in names and "goal" not in names
    assert "lie_down" not in names and "wait" not in names and "say" not in names


def test_switch_on_cortex_session_registers_wish_and_cortex_trio(monkeypatch):
    """enabled=true AND cortex session (_CORTEX) => wish + lie_down/wait/say
    register; first/goal stay pending (not registered)."""
    _force_enabled(monkeypatch, True)
    m, mt = _fresh_mcp()
    monkeypatch.setattr(cortex_bridge, "_CORTEX", True)
    cortex_bridge.register(mt)
    names = set(m._tool_manager._tools.keys())
    assert {"wish", "lie_down", "wait", "say"} <= names
    assert "first" not in names and "goal" not in names


def test_tool_descriptions_render_clamp_numbers_from_config(monkeypatch, tmp_path):
    """C9/C10: lie_down + wait descriptions render clamp numbers from cortex.toml
    at register(), never hardcoded. A shared cortex.toml supplies the values,
    including the nested [wake.watchdog].silent_max_min auto-timer length."""
    (tmp_path / "cortex.toml").write_text(
        "[wake]\nwait_min = 2\nwait_max = 18\nnext_wake_min = 25\n"
        "next_wake_max = 200\n[wake.watchdog]\nsilent_max_min = 12\n"
        "[night]\nfloor_min = 90\nfloor_max = 300\n")
    monkeypatch.setattr(cortex_bridge.config, "db_path",
                        lambda: str(tmp_path / "marrow.db"))
    _force_enabled(monkeypatch, True)
    m, mt = _fresh_mcp()
    monkeypatch.setattr(cortex_bridge, "_CORTEX", True)
    cortex_bridge.register(mt)
    ld = m._tool_manager._tools["lie_down"].description
    wd = m._tool_manager._tools["wait"].description
    assert "N=25-200 (Day); 90-300 (Night)" in ld
    assert "N=2-18" in wd
    assert "one wait per wake" in wd
    assert "12-min auto timer" in wd  # rendered from [wake.watchdog].silent_max_min
    assert "expiry brings the 3-choice menu" in wd
    # No stale hardcoded ranges leaked in.
    assert "16-55" not in wd and "90-360" not in ld


def test_tool_descriptions_fall_back_to_defaults(monkeypatch, tmp_path):
    """No cortex.toml -> tolerant defaults (day 21-240, wait 1-20, night 120-360,
    auto timer 20)."""
    monkeypatch.setattr(cortex_bridge.config, "db_path",
                        lambda: str(tmp_path / "marrow.db"))  # no cortex.toml here
    _force_enabled(monkeypatch, True)
    m, mt = _fresh_mcp()
    monkeypatch.setattr(cortex_bridge, "_CORTEX", True)
    cortex_bridge.register(mt)
    assert "N=21-240 (Day); 120-360 (Night)" in \
        m._tool_manager._tools["lie_down"].description
    assert "20-min auto timer" in m._tool_manager._tools["wait"].description
    assert "N=1-20" in m._tool_manager._tools["wait"].description


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




# ── wake v2 (Item 1-3) ────────────────────────────────────────────────────────

def test_arm_ear_text_substitutes_signal_log(monkeypatch, tmp_path):
    """arm_ear_text substitutes {signal_log} with the path resolved under home."""
    _force_enabled(monkeypatch, True, extra={
        "home": str(tmp_path),
        "arm_ear_text": "arm: tail {signal_log}",
    })
    assert cortex_bridge.arm_ear_text() == f"arm: tail {tmp_path/'state'/'wake_signal.log'}"


def test_arm_ear_text_absolute_override(monkeypatch, tmp_path):
    """An absolute wake_signal_log_file override is used as-is."""
    log = tmp_path / "custom.log"
    _force_enabled(monkeypatch, True, extra={
        "home": str(tmp_path),
        "wake_signal_log_file": str(log),
        "arm_ear_text": "tail {signal_log}",
    })
    assert cortex_bridge.arm_ear_text() == f"tail {log}"


def test_arm_ear_text_blank_returns_none(monkeypatch, tmp_path):
    """Blank arm text -> None (caller injects nothing)."""
    _force_enabled(monkeypatch, True,
                   extra={"home": str(tmp_path), "arm_ear_text": ""})
    assert cortex_bridge.arm_ear_text() is None


def test_wake_marker_reads_config(monkeypatch):
    """wake_marker reflects [cortex].wake_marker (stripped)."""
    _force_enabled(monkeypatch, True, extra={"wake_marker": "  [CORTEX-WAKE] "})
    assert cortex_bridge.wake_marker() == "[CORTEX-WAKE]"


def test_wakeup_note_text_reads_file(monkeypatch, tmp_path):
    """wakeup_note_text returns the note file contents (stripped)."""
    (tmp_path / "wakeup_note.md").write_text("  do the thing  ", encoding="utf-8")
    _force_enabled(monkeypatch, True, extra={"home": str(tmp_path)})
    assert cortex_bridge.wakeup_note_text() == "do the thing"


def test_wakeup_note_text_missing_returns_none(monkeypatch, tmp_path):
    """Missing note file -> None (no crash)."""
    _force_enabled(monkeypatch, True, extra={"home": str(tmp_path)})
    assert cortex_bridge.wakeup_note_text() is None


def test_wakeup_note_text_empty_returns_none(monkeypatch, tmp_path):
    """Empty note file -> None (caller injects nothing)."""
    (tmp_path / "wakeup_note.md").write_text("   \n", encoding="utf-8")
    _force_enabled(monkeypatch, True, extra={"home": str(tmp_path)})
    assert cortex_bridge.wakeup_note_text() is None


def test_rearm_text_substitutes_signal_log(monkeypatch, tmp_path):
    """rearm_text substitutes {signal_log}."""
    _force_enabled(monkeypatch, True, extra={
        "home": str(tmp_path),
        "rearm_text": "rearm: tail {signal_log}",
    })
    assert cortex_bridge.rearm_text() == f"rearm: tail {tmp_path/'state'/'wake_signal.log'}"


def test_rearm_text_blank_returns_none(monkeypatch, tmp_path):
    """Blank rearm text -> None."""
    _force_enabled(monkeypatch, True,
                   extra={"home": str(tmp_path), "rearm_text": ""})
    assert cortex_bridge.rearm_text() is None


def test_is_monitor_death_matches_notification():
    """Fires on the harness Monitor-stopped task-notification shape."""
    prompt = ('<task-notification>\n<task-id>bwkjxl09h</task-id>\n'
              '<summary>Monitor event: "ear"</summary>\n'
              '<event>[Monitor stopped — too much output.]</event>\n'
              '</task-notification>')
    assert cortex_bridge.is_monitor_death(prompt) is True


def test_is_monitor_death_silent_on_normal_chat():
    """Never fires on ordinary chat, or on a live (non-stopped) monitor event."""
    assert cortex_bridge.is_monitor_death("聊聊天，顺便说下 Monitor 怎么用") is False
    assert cortex_bridge.is_monitor_death("") is False
    live = ('<task-notification>\n<summary>Monitor event: "ear"</summary>\n'
            '<event>[CORTEX-WAKE] 2026-07-11 wake</event>\n</task-notification>')
    assert cortex_bridge.is_monitor_death(live) is False


def test_boot_rules_helpers_removed():
    """The rejected boot_rules SessionStart mechanism is fully gone."""
    assert not hasattr(cortex_bridge, "cortex_boot_rules")
    assert not hasattr(cortex_bridge, "_cortex_boot_rules_path")
