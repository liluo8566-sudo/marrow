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


def test_lie_down_surfaces_rotate_refused_text(env, monkeypatch, tmp_path):
    """P17: rotate refused while the window's own ear tail is alive — the
    refusal text must reach the calling session as the tool result text,
    else the rotate precondition is invisible to the model."""
    _fake_lie_down_run(
        monkeypatch, tmp_path,
        '{"skipped": "rotate_refused", "refused": '
        '"TaskStop your monitor first, then call lie_down again."}')
    out = cortex_bridge.lie_down(next_wake_min=20, rotate=True)
    assert out["ok"] is True
    assert out["refused"] == "TaskStop your monitor first, then call lie_down again."
    assert out["text"] == "TaskStop your monitor first, then call lie_down again."


def test_lie_down_passes_human_override_flag(env, monkeypatch, tmp_path):
    fake_py = tmp_path / "python"
    fake_root = tmp_path / "repo"
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {"venv_python": str(fake_py), "repo_root": str(fake_root)},
    })
    captured = {}

    class _P:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def _fake_run(cmd, cwd=None, **kw):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(cortex_bridge.subprocess, "run", _fake_run)
    cortex_bridge.lie_down(next_wake_min=10, human_override=True)
    assert "--human-override" in captured["cmd"]


def test_lie_down_no_human_override_flag_by_default(env, monkeypatch, tmp_path):
    fake_py = tmp_path / "python"
    fake_root = tmp_path / "repo"
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {"venv_python": str(fake_py), "repo_root": str(fake_root)},
    })
    captured = {}

    class _P:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def _fake_run(cmd, cwd=None, **kw):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(cortex_bridge.subprocess, "run", _fake_run)
    cortex_bridge.lie_down(next_wake_min=10)
    assert "--human-override" not in captured["cmd"]


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
    assert "lie_down" not in names and "say" not in names


def test_switch_on_cortex_session_registers_wish_and_cortex_pair(monkeypatch):
    """enabled=true AND cortex session (_CORTEX) => wish + lie_down/say
    register (wait retired T1); first/goal stay pending (not registered)."""
    _force_enabled(monkeypatch, True)
    m, mt = _fresh_mcp()
    monkeypatch.setattr(cortex_bridge, "_CORTEX", True)
    cortex_bridge.register(mt)
    names = set(m._tool_manager._tools.keys())
    assert {"wish", "lie_down", "say"} <= names
    assert "wait" not in names
    assert "first" not in names and "goal" not in names


def _register_as(monkeypatch, shell_env, shells=None):
    """register() under a given MARROW_CORTEX value (T8 shell id). shells=None
    keeps the config default (["cli"])."""
    extra = None if shells is None else {"shells": shells}
    _force_enabled(monkeypatch, True, extra=extra)
    if shell_env is None:
        monkeypatch.delenv("MARROW_CORTEX", raising=False)
    else:
        monkeypatch.setenv("MARROW_CORTEX", shell_env)
    monkeypatch.setattr(cortex_bridge, "_CORTEX", shell_env is not None)
    m, mt = _fresh_mcp()
    cortex_bridge.register(mt)
    return set(m._tool_manager._tools.keys())


def test_shell_legacy_and_cli_register_lie_down_and_say(monkeypatch):
    """T8: MARROW_CORTEX=1 (legacy) and =cli are the same shell — full cli kit."""
    for value in ("1", "cli"):
        names = _register_as(monkeypatch, value)
        assert {"wish", "lie_down", "say"} <= names


def test_shell_tg_registers_lie_down_without_say(monkeypatch):
    """T8: say is cli-only; the tg shell still gets lie_down."""
    names = _register_as(monkeypatch, "tg", shells=["cli", "tg"])
    assert {"wish", "lie_down"} <= names
    assert "say" not in names


def test_shell_absent_from_shells_registers_no_cortex_tools(monkeypatch):
    """T8: tg session while shells=["cli"] -> plain session (wish only)."""
    names = _register_as(monkeypatch, "tg", shells=["cli"])
    assert names == {"wish"}


def test_empty_shells_disables_cortex_tools_for_cli(monkeypatch):
    """T8: shells=[] switches every shell off; enabled stays the master switch."""
    names = _register_as(monkeypatch, "cli", shells=[])
    assert names == {"wish"}


def test_shell_enabled_follows_env_and_config(monkeypatch):
    """_shell_enabled: cortex env + listed shell id. Non-cortex always False."""
    _force_enabled(monkeypatch, True, extra={"shells": ["cli", "tg"]})
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    assert cortex_bridge._shell_enabled() is False
    monkeypatch.setenv("MARROW_CORTEX", "1")
    assert cortex_bridge._shell_enabled() is True
    monkeypatch.setenv("MARROW_CORTEX", "tg")
    assert cortex_bridge._shell_enabled() is True
    _force_enabled(monkeypatch, True, extra={"shells": ["cli"]})
    assert cortex_bridge._shell_enabled() is False


def test_shell_gates_go_plain_when_shell_not_listed(monkeypatch):
    """T8: a cortex-env session off the shells list takes no cortex branch."""
    _force_enabled(monkeypatch, True, extra={"shells": ["cli"]})
    monkeypatch.setenv("MARROW_CORTEX", "tg")
    inp = {"tool_name": "mcp__marrow__lie_down", "tool_input": {"rotate": True}}
    assert cortex_bridge._cortex_lie_down_nudge(inp) is None
    assert cortex_bridge._cortex_show_context("", None) == ""


# ── per-shell state file ──────────────────────────────────────────────────────

def test_shell_state_roundtrip_with_lock_file(monkeypatch, tmp_path):
    """write -> read roundtrip; lock sibling created; only contract keys stored."""
    _force_enabled(monkeypatch, True,
                   extra={"shell_state_dir": str(tmp_path / "shells")})
    p = cortex_bridge.shell_state_write(
        {"session_id": "abc", "next_wake_at": "2026-07-25T10:00:00+00:00",
         "last_note_ts": "2026-07-25T09:00:00+00:00", "junk": 1}, shell="tg")
    assert p == tmp_path / "shells" / "tg.json"
    assert (tmp_path / "shells" / "tg.lock").exists()
    assert cortex_bridge.shell_state_read("tg") == {
        "session_id": "abc", "next_wake_at": "2026-07-25T10:00:00+00:00",
        "last_note_ts": "2026-07-25T09:00:00+00:00"}


def test_shell_state_write_merges_and_drops_none(monkeypatch, tmp_path):
    """Partial write keeps untouched keys; None drops its key; no temp residue."""
    _force_enabled(monkeypatch, True,
                   extra={"shell_state_dir": str(tmp_path / "shells")})
    cortex_bridge.shell_state_write({"session_id": "abc", "next_wake_at": "t1"},
                                    shell="tg")
    cortex_bridge.shell_state_write({"next_wake_at": None, "last_note_ts": "t2"},
                                    shell="tg")
    assert cortex_bridge.shell_state_read("tg") == {"session_id": "abc",
                                                    "last_note_ts": "t2"}
    assert not list((tmp_path / "shells").glob("*.tmp.*"))


def test_shell_state_read_missing_file(monkeypatch, tmp_path):
    _force_enabled(monkeypatch, True,
                   extra={"shell_state_dir": str(tmp_path / "shells")})
    assert cortex_bridge.shell_state_read("tg") == {}


def test_shell_state_path_defaults_to_data_dir_and_env_shell(monkeypatch):
    """Empty shell_state_dir -> <DATA_DIR>/state/shells; shell=None -> env id."""
    _force_enabled(monkeypatch, True, extra={"shell_state_dir": ""})
    monkeypatch.setenv("MARROW_CORTEX", "tg")
    p = cortex_bridge._shell_state_path()
    assert p == config.DATA_DIR / "state" / "shells" / "tg.json"


# ── T9: lie_down routing for a non-cli shell ──────────────────────────────────

def _tg_lie_down_env(monkeypatch, tmp_path, sock="", wake=None):
    """tg-shell window with its own state dir; cortex.toml supplies the bands."""
    (tmp_path / "cortex.toml").write_text(
        wake or "[wake]\nnext_wake_low_max = 55\nnext_wake_high_min = 180\n"
                "next_wake_max = 360\n")
    monkeypatch.setattr(cortex_bridge, "_cortex_toml_path",
                        lambda: tmp_path / "cortex.toml")
    _force_enabled(monkeypatch, True,
                   extra={"shells": ["cli", "tg"], "shell_socket": sock,
                          "shell_state_dir": str(tmp_path / "shells")})
    monkeypatch.setenv("MARROW_CORTEX", "tg")


def test_tg_lie_down_writes_ledger_and_kicks_without_running_cortex(
        monkeypatch, tmp_path):
    """T9: the tg shell host owns the timing — lie_down only writes
    next_wake_at and pokes the socket. The cortex module is never spawned."""
    _tg_lie_down_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cortex_bridge.subprocess, "run",
                        lambda *a, **k: pytest.fail("cortex module spawned"))
    kicks = []
    monkeypatch.setattr(cortex_bridge, "_shell_kick",
                        lambda shell: kicks.append(shell) or True)

    out = cortex_bridge.lie_down(next_wake_min=30)
    assert out["ok"] is True and out["shell"] == "tg" and out["kicked"] is True
    assert kicks == ["tg"]
    from datetime import datetime
    when = datetime.fromisoformat(
        cortex_bridge.shell_state_read("tg")["next_wake_at"])
    delta = (when - datetime.now(when.tzinfo)).total_seconds()
    assert 29 * 60 < delta <= 30 * 60
    assert out["next_wake"] == when.strftime("%H:%M")


def test_tg_lie_down_zero_is_immediate(monkeypatch, tmp_path):
    _tg_lie_down_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cortex_bridge, "_shell_kick", lambda shell: True)
    cortex_bridge.lie_down(next_wake_min=0)
    from datetime import datetime
    when = datetime.fromisoformat(
        cortex_bridge.shell_state_read("tg")["next_wake_at"])
    assert abs((when - datetime.now(when.tzinfo)).total_seconds()) < 5


def _tg_booked_minutes(next_wake_min, **kw):
    """Minutes the tg ledger actually booked for a lie_down call."""
    from datetime import datetime
    cortex_bridge.lie_down(next_wake_min=next_wake_min, **kw)
    when = datetime.fromisoformat(
        cortex_bridge.shell_state_read("tg")["next_wake_at"])
    return (when - datetime.now(when.tzinfo)).total_seconds() / 60


def test_tg_lie_down_clamps_to_next_wake_max(monkeypatch, tmp_path):
    _tg_lie_down_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cortex_bridge, "_shell_kick", lambda shell: True)
    assert 359 < _tg_booked_minutes(9999) <= 360


@pytest.mark.parametrize("given,expected", [
    (117, 55), (118, 180),                # dead zone snaps to the nearer edge
    (55, 55), (180, 180), (360, 360),     # band edges pass through
    (30, 30), (300, 300),                 # inside a band, untouched
])
def test_tg_lie_down_snaps_into_the_two_bands(monkeypatch, tmp_path,
                                              given, expected):
    """The shell path applies the same two legal bands as cortex's cli path:
    anything in the gap between them snaps to the nearer edge."""
    _tg_lie_down_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cortex_bridge, "_shell_kick", lambda shell: True)
    assert expected - 1 < _tg_booked_minutes(given) <= expected


def test_tg_lie_down_human_override_pierces_the_bands(monkeypatch, tmp_path):
    """An explicit human choice reaches the ledger untouched — 90 stays 90
    even though it sits in the unselectable gap."""
    _tg_lie_down_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cortex_bridge, "_shell_kick", lambda shell: True)
    assert 89 < _tg_booked_minutes(90, human_override=True) <= 90


def test_tg_lie_down_band_edges_come_from_cortex_config(monkeypatch, tmp_path):
    """Band edges are config, not constants: a custom [wake] moves the snap."""
    _tg_lie_down_env(monkeypatch, tmp_path,
                     wake="[wake]\nnext_wake_low_max = 20\n"
                          "next_wake_high_min = 100\nnext_wake_max = 200\n")
    monkeypatch.setattr(cortex_bridge, "_shell_kick", lambda shell: True)
    assert 19 < _tg_booked_minutes(55) <= 20
    assert 99 < _tg_booked_minutes(70) <= 100
    assert 199 < _tg_booked_minutes(9999) <= 200


def test_tg_lie_down_survives_a_dead_host(monkeypatch, tmp_path):
    """Host down -> ledger still written, kicked=False, no raise (the host
    picks the ledger up on its next recompute tick)."""
    _tg_lie_down_env(monkeypatch, tmp_path,
                     sock=str(tmp_path / "absent.sock"))
    out = cortex_bridge.lie_down(next_wake_min=10)
    assert out["ok"] is True and out["kicked"] is False
    assert cortex_bridge.shell_state_read("tg")["next_wake_at"]


def test_tg_lie_down_rotate_flags_the_ledger_and_kicks(monkeypatch, tmp_path):
    """rotate=True from a tg-shell window: the host is told to end the window,
    and the wake it booked is written alongside so the fresh session sleeps
    until then. Same semantics as the cli shell's rotate."""
    _tg_lie_down_env(monkeypatch, tmp_path)
    kicks = []
    monkeypatch.setattr(cortex_bridge, "_shell_kick",
                        lambda shell: kicks.append(shell) or True)

    out = cortex_bridge.lie_down(next_wake_min=30, rotate=True)

    assert out["ok"] is True and out["rotate"] is True and out["kicked"] is True
    assert kicks == ["tg"]
    st = cortex_bridge.shell_state_read("tg")
    assert st["rotate_pending"] is True and st["next_wake_at"]


def test_tg_lie_down_without_rotate_leaves_no_flag(monkeypatch, tmp_path):
    _tg_lie_down_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cortex_bridge, "_shell_kick", lambda shell: True)
    out = cortex_bridge.lie_down(next_wake_min=30)
    assert out["rotate"] is False
    assert "rotate_pending" not in cortex_bridge.shell_state_read("tg")


def test_shell_direct_writes_pending_note_and_kicks(monkeypatch, tmp_path):
    """T10 directed kick: the text lands in the ledger, then the host is poked."""
    _force_enabled(monkeypatch, True,
                   extra={"shell_state_dir": str(tmp_path / "shells")})
    kicks = []
    monkeypatch.setattr(cortex_bridge, "_shell_kick",
                        lambda shell: kicks.append(shell) or True)
    out = cortex_bridge.shell_direct("  go check the diary  ")
    assert out == {"ok": True, "shell": "tg", "kicked": True}
    assert kicks == ["tg"]
    assert cortex_bridge.shell_state_read("tg")["pending_note"] == "go check the diary"


def test_shell_direct_rejects_empty_text_without_kicking(monkeypatch, tmp_path):
    _force_enabled(monkeypatch, True,
                   extra={"shell_state_dir": str(tmp_path / "shells")})
    monkeypatch.setattr(cortex_bridge, "_shell_kick",
                        lambda shell: pytest.fail("must not kick"))
    assert cortex_bridge.shell_direct("   ")["ok"] is False
    assert cortex_bridge.shell_state_read("tg") == {}


def test_shell_direct_survives_a_dead_host(monkeypatch, tmp_path):
    """Host down -> kicked False, text still queued for its recompute tick."""
    _force_enabled(monkeypatch, True,
                   extra={"shell_state_dir": str(tmp_path / "shells"),
                          "shell_socket": str(tmp_path / "absent.sock")})
    out = cortex_bridge.shell_direct("wake up")
    assert out["ok"] is True and out["kicked"] is False
    assert cortex_bridge.shell_state_read("tg")["pending_note"] == "wake up"


def test_cli_shell_direct_command(monkeypatch, tmp_path, capsys):
    """mw shell-direct <text> — the slash-command entry point."""
    from marrow import cli
    _force_enabled(monkeypatch, True,
                   extra={"shell_state_dir": str(tmp_path / "shells")})
    monkeypatch.setattr(cortex_bridge, "_shell_kick", lambda shell: True)
    assert cli.main(["shell-direct", "go", "check", "the", "diary"]) == 0
    assert "kicked=True" in capsys.readouterr().out
    assert cortex_bridge.shell_state_read("tg")["pending_note"] == "go check the diary"


def test_shell_kick_wire_format_is_one_shell_line(monkeypatch, tmp_path):
    """The datagram must match synapse_core.scheduler.send_kick: "<shell>\\n"
    over an AF_UNIX stream socket. Real socket, short path (macOS 104-byte cap)."""
    import shutil
    import socket
    import tempfile
    import threading
    d = tempfile.mkdtemp(prefix="mwk", dir="/tmp")
    try:
        path = f"{d}/s.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        got = []

        def _accept():
            conn, _ = srv.accept()
            got.append(conn.recv(64))
            conn.close()

        t = threading.Thread(target=_accept, daemon=True)
        t.start()
        _force_enabled(monkeypatch, True, extra={"shell_socket": path})
        assert cortex_bridge._shell_kick("tg") is True
        t.join(timeout=5)
        srv.close()
        assert got == [b"tg\n"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_shell_kick_without_a_socket_for_that_shell(monkeypatch, tmp_path):
    """The single shell_socket belongs to tg; another shell has none, so the
    kick is skipped instead of poking tg's socket."""
    _force_enabled(monkeypatch, True, extra={"shell_socket": str(tmp_path / "s.sock")})
    assert cortex_bridge._shell_socket_path("tg") == tmp_path / "s.sock"
    assert cortex_bridge._shell_socket_path("wx") is None
    assert cortex_bridge._shell_kick("wx") is False


def test_bad_marrow_cortex_value_is_refused_not_read_as_cli(monkeypatch, tmp_path):
    """A malformed marker must never resolve to cli and claim its ledger."""
    _force_enabled(monkeypatch, True,
                   extra={"shell_state_dir": str(tmp_path / "shells")})
    cortex_bridge._bad_shell_id_warned.clear()
    alerts = []
    monkeypatch.setattr(cortex_bridge, "_warn_bad_shell_id", alerts.append)
    monkeypatch.setenv("MARROW_CORTEX", "../cli")
    assert cortex_bridge._cortex_shell_id() is None
    assert alerts == ["../cli"]
    assert cortex_bridge._shell_enabled() is False
    assert cortex_bridge._cortex_handoff_path() is None
    assert cortex_bridge.wakeup_note_text("/t/x.jsonl") is None
    assert cortex_bridge.lie_down(30)["ok"] is False
    with pytest.raises(ValueError):
        cortex_bridge._shell_state_path()


def test_marrow_cortex_legacy_and_explicit_shell_ids(monkeypatch):
    _force_enabled(monkeypatch, True)
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    assert cortex_bridge._cortex_shell_id() == "cli"
    monkeypatch.setenv("MARROW_CORTEX", "1")
    assert cortex_bridge._cortex_shell_id() == "cli"
    monkeypatch.setenv("MARROW_CORTEX", "TG")
    assert cortex_bridge._cortex_shell_id() == "tg"
    monkeypatch.setenv("MARROW_CORTEX", "wx")
    assert cortex_bridge._cortex_shell_id() == "wx"


def test_cli_lie_down_path_untouched_by_the_shell_route(env, monkeypatch, tmp_path):
    """Regression: the cli shell still spawns cortex.lie_down and writes no
    shell state file."""
    _force_enabled(monkeypatch, True,
                   extra={"venv_python": str(tmp_path / "py"),
                          "repo_root": str(tmp_path / "repo"),
                          "shell_state_dir": str(tmp_path / "shells")})
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    captured = {}

    class _P:
        returncode = 0
        stdout = '{"next_wake": "10:30"}'
        stderr = ""

    monkeypatch.setattr(cortex_bridge.subprocess, "run",
                        lambda cmd, cwd=None, **kw: captured.update(cmd=cmd) or _P())
    out = cortex_bridge.lie_down(next_wake_min=20)
    assert out["next_wake"] == "10:30"
    assert captured["cmd"][1:3] == ["-m", "cortex.lie_down"]
    assert not (tmp_path / "shells").exists()


def test_tool_descriptions_render_clamp_numbers_from_config(monkeypatch, tmp_path):
    """C9: lie_down description renders the two legal bands from cortex.toml at
    register(), never hardcoded."""
    (tmp_path / "cortex.toml").write_text(
        "[wake]\nnext_wake_low_max = 20\nnext_wake_high_min = 100\n"
        "next_wake_max = 200\n")
    monkeypatch.setattr(cortex_bridge.config, "db_path",
                        lambda: str(tmp_path / "marrow.db"))
    _force_enabled(monkeypatch, True)
    m, mt = _fresh_mcp()
    monkeypatch.setattr(cortex_bridge, "_CORTEX", True)
    cortex_bridge.register(mt)
    ld = m._tool_manager._tools["lie_down"].description
    assert ld.count("N=0-20 ∪ 100-200") == 2
    # No stale hardcoded ranges leaked in.
    assert "16-55" not in ld and "N=0-200" not in ld


def test_tool_descriptions_fall_back_to_defaults(monkeypatch, tmp_path):
    """No cortex.toml -> tolerant defaults for both bands."""
    monkeypatch.setattr(cortex_bridge.config, "db_path",
                        lambda: str(tmp_path / "marrow.db"))  # no cortex.toml here
    _force_enabled(monkeypatch, True)
    m, mt = _fresh_mcp()
    monkeypatch.setattr(cortex_bridge, "_CORTEX", True)
    cortex_bridge.register(mt)
    ld = m._tool_manager._tools["lie_down"].description
    assert ld == ('lie_down(next_wake_min=N) [N=0-55 ∪ 180-360]; '
                  'rotate to next window - lie_down(next_wake_min=N, '
                  'rotate=True) [N=0-55 ∪ 180-360, 0=rotate now]')


def test_switch_off_show_context_gated_empty(monkeypatch, tmp_path):
    """The turn_inject 亮牌 helper itself still checks MARROW_CORTEX; with the
    switch off the hook call site never invokes it (call-site gate), and even if
    invoked without MARROW_CORTEX it returns empty."""
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    assert cortex_bridge._cortex_show_context(str(tmp_path / "none.jsonl"), None) == ""


# ── wake v2 (Item 1-3) ────────────────────────────────────────────────────────

def test_wakeup_note_text_reads_file(monkeypatch, tmp_path):
    """wakeup_note_text returns the note file contents (stripped)."""
    (tmp_path / "wakeup_note.md").write_text("## cli\n  do the thing  \n", encoding="utf-8")
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


def test_boot_rules_helpers_removed():
    """The rejected boot_rules SessionStart mechanism is fully gone."""
    assert not hasattr(cortex_bridge, "cortex_boot_rules")
    assert not hasattr(cortex_bridge, "_cortex_boot_rules_path")


# ── shell sleep ledger (ct_wake_log rows per shell) ───────────────────────────

def _wake_log_db(tmp_path):
    """A db carrying the live ct_wake_log shape (cortex owns the migration)."""
    import sqlite3
    db = str(tmp_path / "wake.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE ct_wake_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT NOT NULL, wake INTEGER NOT NULL, dry_run INTEGER NOT NULL, "
        "reasons TEXT, gated_by TEXT, explanation TEXT, tokens INTEGER, "
        "force_slept TEXT, net_tokens INTEGER, "
        "shell TEXT NOT NULL DEFAULT 'cli')")
    conn.commit()
    conn.close()
    return db


def _wake_rows(db):
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT shell, wake, dry_run, reasons, force_slept "
            "FROM ct_wake_log ORDER BY id").fetchall()
    finally:
        conn.close()


def test_tg_lie_down_writes_one_wake_log_row(monkeypatch, tmp_path):
    """T2: a tg shell lie_down lands exactly one ct_wake_log row stamped
    shell='tg' with force_slept empty (a voluntary sleep is no incident)."""
    db = _wake_log_db(tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: db)
    _force_enabled(monkeypatch, True,
                   extra={"shell_state_dir": str(tmp_path / "shells")})
    monkeypatch.setattr(cortex_bridge, "_cortex_toml_section",
                        lambda *a, **k: 240)
    monkeypatch.setattr(cortex_bridge, "_shell_kick", lambda shell: True)
    monkeypatch.setenv("MARROW_CORTEX", "tg")

    out = cortex_bridge.lie_down(30)

    assert out["ok"] is True and out["shell"] == "tg"
    rows = _wake_rows(db)
    assert len(rows) == 1
    shell, wake, dry_run, reasons, force_slept = rows[0]
    assert shell == "tg"
    assert (wake, dry_run, reasons) == (1, 0, "lie_down")
    assert force_slept is None


def test_cli_lie_down_writes_no_shell_row(monkeypatch, tmp_path):
    """T2 regression: the cli path is unchanged — cortex writes its own row, so
    the bridge must not add one here."""
    db = _wake_log_db(tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: db)
    _force_enabled(monkeypatch, True,
                   extra={"shell_state_dir": str(tmp_path / "shells")})
    monkeypatch.setattr(cortex_bridge, "_run_cortex_module",
                        lambda module, args=None: {"ok": True, "stdout": "{}"})
    monkeypatch.setenv("MARROW_CORTEX", "1")

    assert cortex_bridge.lie_down(30)["ok"] is True
    assert _wake_rows(db) == []


def test_shell_sleep_row_survives_missing_table(monkeypatch, tmp_path):
    """Best-effort: a db without ct_wake_log never fails the sleep."""
    db = str(tmp_path / "empty.db")
    import sqlite3
    sqlite3.connect(db).close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    assert cortex_bridge._log_shell_sleep_row("tg") is None
