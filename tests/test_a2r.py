"""A2r revision — imp boost + source tag, care inject, agent guard, install dedup."""
from __future__ import annotations

import io
import json

import pytest

from marrow import config, hooks, install, recall, storage, tl_writer


# ── recall: staged imp boost + [tl]/[event] source tag ───────────────────────

def test_imp_boost_staged():
    tbl = recall._IMP_BOOST_DEFAULT
    assert recall._imp_boost(None, tbl) == 0.0
    assert recall._imp_boost(1, tbl) == 0.0
    assert recall._imp_boost(2, tbl) == 0.0
    assert recall._imp_boost(3, tbl) == pytest.approx(0.02)
    assert recall._imp_boost(4, tbl) == pytest.approx(0.035)
    assert recall._imp_boost(5, tbl) == pytest.approx(0.05)
    assert recall._imp_boost(9, tbl) == pytest.approx(0.05)  # cap at tail


@pytest.fixture()
def conn(tmp_path):
    c = storage.init_db(str(tmp_path / "a2r.db"))
    yield c
    c.close()


def test_recall_tags_tl_vs_event(conn):
    # a plain event and a tl row sharing an FTS keyword
    conn.execute("INSERT INTO events (session_id, timestamp, role, content)"
                 " VALUES ('s1', '2026-07-01T00:00:00Z', 'user', 'kangaroo picnic')")
    conn.execute("INSERT INTO events (session_id, timestamp, role, content, imp)"
                 " VALUES ('s2', '2026-07-01T00:00:00Z', 'tl', '【N 愉悦·5】kangaroo picnic', 5)")
    conn.commit()
    hits = recall.recall_fusion(conn, "kangaroo", limit=10)
    tags = {h.get("source_tag") for h in hits if h.get("id")}
    assert "tl" in tags and "event" in tags
    # the imp-5 tl row outscores the plain event
    by_role = {h["role"]: h["score"] for h in hits if h.get("role") in ("tl", "user")}
    assert by_role.get("tl", 0) > by_role.get("user", 0)


# ── turn_inject: care directive from config ──────────────────────────────────

def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def test_turn_inject_emits_care_text(monkeypatch, capsys):
    monkeypatch.delenv("MARROW_CHANNEL", raising=False)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/x/a.jsonl"})
    hooks.turn_inject()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "Care first" in ctx  # from config.default.toml [turn_inject].care_text


# ── B8: anti-late-night kickout nudge (turn_inject) ──────────────────────────

def _freeze_melb(monkeypatch, hour, minute):
    """Freeze hooks.datetime.now() + config.get_tz() to a fixed Melbourne
    wall-clock instant, independent of the test env's default config
    (core.timezone defaults to Asia/Shanghai in config.default.toml)."""
    from datetime import datetime as _datetime
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")

    class _Fake:
        @classmethod
        def now(cls, tz=None):
            return _datetime(2026, 7, 8, hour, minute, tzinfo=melb)

    monkeypatch.setattr(hooks, "datetime", _Fake)
    monkeypatch.setattr(config, "get_tz", lambda: melb)


def test_kickout_cli_wind_down_window(monkeypatch, capsys):
    _freeze_melb(monkeypatch, 21, 45)
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    monkeypatch.delenv("MARROW_CHANNEL", raising=False)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/x/a.jsonl"})
    hooks.turn_inject()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "9点半啦" in ctx


def test_kickout_cli_leave_window(monkeypatch, capsys):
    _freeze_melb(monkeypatch, 22, 30)
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    monkeypatch.delenv("MARROW_CHANNEL", raising=False)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/x/a.jsonl"})
    hooks.turn_inject()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "该回卧室了" in ctx


def test_kickout_cli_daytime_no_nudge(monkeypatch, capsys):
    _freeze_melb(monkeypatch, 14, 0)
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    monkeypatch.delenv("MARROW_CHANNEL", raising=False)
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/x/a.jsonl"})
    hooks.turn_inject()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "9点半啦" not in ctx
    assert "该回卧室了" not in ctx


def test_kickout_cortex_immune(monkeypatch, capsys):
    _freeze_melb(monkeypatch, 21, 45)
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/x/a.jsonl"})
    hooks.turn_inject()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "9点半啦" not in ctx


def test_kickout_wx_quiet_window(monkeypatch, capsys):
    _freeze_melb(monkeypatch, 23, 30)
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    monkeypatch.setenv("MARROW_CHANNEL", "wx")
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/x/a.jsonl"})
    hooks.turn_inject()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "老婆该睡了" in ctx


def test_kickout_wx_evening_no_nudge(monkeypatch, capsys):
    _freeze_melb(monkeypatch, 21, 45)
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    monkeypatch.setenv("MARROW_CHANNEL", "wx")
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": "/x/a.jsonl"})
    hooks.turn_inject()
    captured = capsys.readouterr().out
    assert captured == ""  # wx skips the time stamp too; no kickout in this window


# ── agent_guard: burst protection ────────────────────────────────────────────

def test_agent_guard_denies_general_purpose(monkeypatch):
    _stdin(monkeypatch, {"tool_name": "Agent",
                         "tool_input": {"subagent_type": "general-purpose"}})
    assert hooks.agent_guard() == 2


def test_agent_guard_allows_named_agent(monkeypatch):
    _stdin(monkeypatch, {"tool_name": "Agent",
                         "tool_input": {"subagent_type": "Explore", "model": "haiku"}})
    assert hooks.agent_guard() == 0


def test_agent_guard_ignores_non_agent(monkeypatch):
    _stdin(monkeypatch, {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert hooks.agent_guard() == 0


# ── install: register_hooks idempotent + turn-inject absorb ───────────────────

def _marrow_cmds(settings, event, needle):
    out = []
    for g in settings["hooks"].get(event, []):
        for h in g.get("hooks", []):
            if needle in h.get("command", ""):
                out.append(h["command"])
    return out


def test_register_hooks_idempotent(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    # seed with a legacy global turn-inject.sh + a foreign hook to preserve
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "/x/turn-inject.sh"}]},
        {"hooks": [{"type": "command", "command": "/x/foreign.sh"}]},
    ]}}))
    monkeypatch.setattr(install, "_SETTINGS", settings)
    install.register_hooks()
    install.register_hooks()  # second run must not duplicate
    s = json.loads(settings.read_text())
    # exactly one marrow turn_inject + one user_prompt_submit + one agent_guard
    assert len(_marrow_cmds(s, "UserPromptSubmit", "marrow.hooks turn_inject")) == 1
    assert len(_marrow_cmds(s, "UserPromptSubmit", "marrow.hooks user_prompt_submit")) == 1
    assert len(_marrow_cmds(s, "PreToolUse", "marrow.hooks agent_guard")) == 1
    # legacy turn-inject.sh absorbed (removed); foreign hook preserved
    assert _marrow_cmds(s, "UserPromptSubmit", "turn-inject.sh") == []
    assert _marrow_cmds(s, "UserPromptSubmit", "foreign.sh") == ["/x/foreign.sh"]
