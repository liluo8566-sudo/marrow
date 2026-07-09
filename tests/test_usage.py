"""Usage rendering + agent-token transcript scan (marrow/usage.py) and the
turn_inject threshold line / cortex lie_down deny guard / SessionStart handoff
block. The threshold line's `main` figure is WINDOW OCCUPANCY (last assistant
usage totals, hooks._window_tokens_from_transcript — same metric as statusline
`total` and the rotate/fuse thresholds), not cumulative net-spend."""
from __future__ import annotations

import io
import json
import time

import pytest

from marrow import config, hooks, usage


def _assistant(cache_creation=0, output=0, cache_read=0, input_=0):
    return json.dumps({"message": {"role": "assistant", "usage": {
        "input_tokens": input_, "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation, "output_tokens": output}}})


# --------------------------------------------------------------------------- #
# transcript scan
# --------------------------------------------------------------------------- #

def test_agent_tokens_accumulate(tmp_path):
    jl = tmp_path / "s.jsonl"
    jl.write_text("\n".join([
        '{"type":"user","content":"subagent_tokens: 12,000 done"}',
        '{"type":"attachment","content":"subagent_tokens>3000"}',
        _assistant(cache_creation=1),  # assistant line ignored for agent scan
    ]))
    assert usage.agent_tokens_from_transcript(str(jl)) == 15_000


def test_scan_missing_file_zero():
    assert usage.agent_tokens_from_transcript("/no/file") == 0


# --------------------------------------------------------------------------- #
# line renderers
# --------------------------------------------------------------------------- #

def test_sessionstart_lines_full():
    kv = {
        "five_hour_pct": "5", "five_hour_reset_at": "2026-07-08T18:50:00+00:00",
        "seven_day_pct": "50", "cdx_five_hour_pct": "5", "cdx_seven_day_pct": "5",
        "today_net_tokens": "1200000",
    }
    lines = usage.sessionstart_lines(kv)
    assert lines[0].startswith("Plan Used: 5h 5%")
    assert "7d 50%" in lines[0]
    assert "cdx 5h 5% 7d 5%" in lines[0]
    assert lines[1] == "Net Token Used today: 1.2M"


def test_sessionstart_lines_empty_kv():
    assert usage.sessionstart_lines({}) == []


def test_threshold_line_shows_main_occupancy_and_agent():
    kv = {"five_hour_pct": "20", "five_hour_reset_at": "2026-07-08T18:50:00+00:00"}
    line = usage.threshold_line(70_000, 120_000, kv)  # main=occupancy, agent=net
    assert line.startswith("Plan Used: 5h 20%")
    assert "Net Session Token: main 70k agent 120k" in line


# --------------------------------------------------------------------------- #
# turn_inject threshold injection (watermark)
# --------------------------------------------------------------------------- #

def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _ctx(capsys):
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""


def test_threshold_inject_fires_once_per_tier(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    jl = tmp_path / "s.jsonl"
    # occupancy (last assistant usage total) 120k (over 100k start) -> tier 100k.
    # A big cache_read (quiet/cached turn) must NOT deflate this like net-spend did.
    jl.write_text(_assistant(input_=1000, cache_read=110_000,
                              cache_creation=8000, output=1000))
    _stdin(monkeypatch, {"session_id": "sx", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "Net Session Token: main 120k agent 0k" in _ctx(capsys)
    # same tier again -> no re-inject
    _stdin(monkeypatch, {"session_id": "sx", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "Net Session Token" not in _ctx(capsys)


def test_threshold_inject_silent_below_start(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    jl = tmp_path / "s.jsonl"
    jl.write_text(_assistant(input_=40_000, cache_read=10_000,
                              cache_creation=5000, output=5000))  # 60k < 100k
    _stdin(monkeypatch, {"session_id": "sy", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "Net Session Token" not in _ctx(capsys)


def test_threshold_inject_quiet_cache_hit_turn_does_not_deflate(tmp_path, monkeypatch, capsys):
    """Regression: a quiet turn with a huge cache HIT (low net-spend, high
    occupancy) must still report/fire on occupancy — the bug this fix kills had
    `main` computed as cumulative net-spend, which stayed low here while real
    occupancy (statusline `total`) was already over the line."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    jl = tmp_path / "s.jsonl"
    # net-spend here is tiny (creation+output = 1100) but occupancy is 131.2k.
    jl.write_text(_assistant(input_=100, cache_read=130_000,
                              cache_creation=900, output=200))
    _stdin(monkeypatch, {"session_id": "sz", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "Net Session Token: main 131k agent 0k" in _ctx(capsys)


# --------------------------------------------------------------------------- #
# cortex lie_down deny guard
# --------------------------------------------------------------------------- #

def _handoff(tmp_path, monkeypatch, home_name="cortex", content="碎碎念", mtime=None):
    home = tmp_path / home_name
    home.mkdir(parents=True, exist_ok=True)
    hp = home / "handoff.md"
    hp.write_text(content, encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(hp, (mtime, mtime))
    monkeypatch.setattr(hooks, "_cortex_handoff_path", lambda: hp)
    return hp


def _big_transcript(tmp_path, occupancy, spawn_ts="2026-07-08T10:00:00+00:00"):
    jl = tmp_path / "big.jsonl"
    jl.write_text("\n".join([
        json.dumps({"timestamp": spawn_ts, "type": "user"}),
        json.dumps({"message": {"usage": {"input_tokens": occupancy}}}),
    ]))
    return jl


def test_deny_rotate_without_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    jl = _big_transcript(tmp_path, 10_000)
    # no handoff file
    monkeypatch.setattr(hooks, "_cortex_handoff_path", lambda: tmp_path / "none.md")
    inp = {"tool_name": "mcp__marrow__lie_down", "transcript_path": str(jl),
           "tool_input": {"rotate": True}}
    assert hooks._cortex_lie_down_deny(inp) is not None


def test_allow_rotate_with_fresh_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    jl = _big_transcript(tmp_path, 10_000, spawn_ts="2026-07-08T10:00:00+00:00")
    # handoff written after spawn (spawn epoch ~ 2026-07-08 10:00 UTC; use now)
    _handoff(tmp_path, monkeypatch, mtime=time.time())
    inp = {"tool_name": "mcp__marrow__lie_down", "transcript_path": str(jl),
           "tool_input": {"rotate": True}}
    assert hooks._cortex_lie_down_deny(inp) is None


def test_allow_plain_lie_down_small_window(tmp_path, monkeypatch):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    jl = _big_transcript(tmp_path, 10_000)  # under force line, no rotate
    inp = {"tool_name": "mcp__marrow__lie_down", "transcript_path": str(jl),
           "tool_input": {}}
    assert hooks._cortex_lie_down_deny(inp) is None


def test_deny_full_window_without_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    force = config.load()["cortex"]["force_tokens"]
    jl = _big_transcript(tmp_path, force + 1)  # over the 150k fuse line
    monkeypatch.setattr(hooks, "_cortex_handoff_path", lambda: tmp_path / "none.md")
    inp = {"tool_name": "mcp__marrow__lie_down", "transcript_path": str(jl),
           "tool_input": {}}
    assert hooks._cortex_lie_down_deny(inp) is not None


def test_deny_skips_non_cortex(tmp_path, monkeypatch):
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    inp = {"tool_name": "mcp__marrow__lie_down", "tool_input": {"rotate": True}}
    assert hooks._cortex_lie_down_deny(inp) is None


# --------------------------------------------------------------------------- #
# SessionStart cortex handoff block
# --------------------------------------------------------------------------- #

def test_handoff_block_reads_file(tmp_path, monkeypatch):
    hp = _handoff(tmp_path, monkeypatch, content="carry this forward")
    block = hooks._cortex_handoff_block()
    assert "carry this forward" in block


def test_handoff_block_empty_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_cortex_handoff_path", lambda: tmp_path / "none.md")
    assert hooks._cortex_handoff_block() == ""
