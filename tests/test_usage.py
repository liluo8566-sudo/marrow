"""Usage rendering + agent-token transcript scan (marrow/usage.py) and the
turn_inject threshold line / cortex lie_down deny guard / cortex handoff
page-turn. The threshold line's `main` figure is WINDOW OCCUPANCY (last
assistant usage totals, hooks._window_tokens_from_transcript — same metric as
statusline `total` and the rotate/fuse thresholds), not cumulative net-spend."""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime

import pytest

from marrow import config, cortex_bridge, hooks, usage


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
# no staleness gating — 5h/7d/cdx always render from kv regardless of age
# --------------------------------------------------------------------------- #

def test_sessionstart_lines_render_regardless_of_age():
    kv = {"five_hour_pct": "5", "seven_day_pct": "50", "today_net_tokens": "1200000"}
    lines = usage.sessionstart_lines(kv)
    assert any(l.startswith("Plan Used: 5h 5%") for l in lines)
    assert "7d 50%" in lines[0]
    assert "Net Token Used today: 1.2M" in lines


def test_threshold_line_renders_5h7d_regardless_of_age():
    kv = {"five_hour_pct": "20", "seven_day_pct": "50"}
    line = usage.threshold_line(70_000, 120_000, kv)
    assert "5h 20%" in line and "7d 50%" in line
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


def test_threshold_inject_db_failure_still_renders_and_advances_watermark(
        tmp_path, monkeypatch, capsys):
    """A kv/DB read failure must degrade to the main/agent segment (no DB
    needed for those) instead of losing the tier silently."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(usage, "read_kv", lambda: (_ for _ in ()).throw(RuntimeError("db busy")))
    jl = tmp_path / "s.jsonl"
    jl.write_text(_assistant(input_=1000, cache_read=110_000,
                              cache_creation=8000, output=1000))
    _stdin(monkeypatch, {"session_id": "sb", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    ctx = _ctx(capsys)
    assert "Net Session Token: main 120k agent 0k" in ctx
    state_file = tmp_path / "state" / "usage_watermark" / "sb"
    assert state_file.read_text().strip() == "100000"


def test_threshold_inject_render_failure_does_not_advance_watermark(
        tmp_path, monkeypatch, capsys):
    """If rendering itself raises (or yields nothing), the watermark must not
    be burned — the tier should still be available next turn."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(usage, "threshold_line",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render fail")))
    jl = tmp_path / "s.jsonl"
    jl.write_text(_assistant(input_=1000, cache_read=110_000,
                              cache_creation=8000, output=1000))
    _stdin(monkeypatch, {"session_id": "sc", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "Net Session Token" not in _ctx(capsys)
    state_file = tmp_path / "state" / "usage_watermark" / "sc"
    assert not state_file.exists()


def test_threshold_inject_agent_tokens_do_not_trigger(tmp_path, monkeypatch, capsys):
    """Agent tokens don't occupy the main window, so a large agent_net with
    main occupancy below threshold_start must NOT fire — the tier/watermark math
    is main-only. agent still shows in the line once main crosses on its own."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    jl = tmp_path / "s.jsonl"
    # main occupancy 60k (< 100k start), agent_net 200k on a user line.
    jl.write_text("\n".join([
        '{"type":"user","content":"subagent_tokens: 200,000"}',
        _assistant(input_=40_000, cache_read=10_000, cache_creation=5000, output=5000),
    ]))
    _stdin(monkeypatch, {"session_id": "sa", "transcript_path": str(jl)})
    assert hooks.main(["turn_inject"]) == 0
    assert "Net Session Token" not in _ctx(capsys)


# --------------------------------------------------------------------------- #
# cortex lie_down deny guard
# --------------------------------------------------------------------------- #

def _handoff(tmp_path, monkeypatch, home_name="cortex", content="handoff-note", mtime=None):
    home = tmp_path / home_name
    home.mkdir(parents=True, exist_ok=True)
    hp = home / "handoff.md"
    hp.write_text(content, encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(hp, (mtime, mtime))
    monkeypatch.setattr(cortex_bridge, "_cortex_handoff_path", lambda: hp)
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
    monkeypatch.setattr(cortex_bridge, "_cortex_handoff_path", lambda: tmp_path / "none.md")
    inp = {"tool_name": "mcp__marrow__lie_down", "transcript_path": str(jl),
           "tool_input": {"rotate": True}}
    assert cortex_bridge._cortex_lie_down_deny(inp) is not None


def test_allow_rotate_with_fresh_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    jl = _big_transcript(tmp_path, 10_000, spawn_ts="2026-07-08T10:00:00+00:00")
    # handoff written after spawn (spawn epoch ~ 2026-07-08 10:00 UTC; use now)
    _handoff(tmp_path, monkeypatch, mtime=time.time())
    inp = {"tool_name": "mcp__marrow__lie_down", "transcript_path": str(jl),
           "tool_input": {"rotate": True}}
    assert cortex_bridge._cortex_lie_down_deny(inp) is None


def test_allow_plain_lie_down_small_window(tmp_path, monkeypatch):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    jl = _big_transcript(tmp_path, 10_000)  # under force line, no rotate
    inp = {"tool_name": "mcp__marrow__lie_down", "transcript_path": str(jl),
           "tool_input": {}}
    assert cortex_bridge._cortex_lie_down_deny(inp) is None


def test_deny_full_window_without_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    force = config.load()["cortex"]["force_tokens"]
    jl = _big_transcript(tmp_path, force + 1)  # over the 150k fuse line
    monkeypatch.setattr(cortex_bridge, "_cortex_handoff_path", lambda: tmp_path / "none.md")
    inp = {"tool_name": "mcp__marrow__lie_down", "transcript_path": str(jl),
           "tool_input": {}}
    assert cortex_bridge._cortex_lie_down_deny(inp) is not None


def test_deny_skips_non_cortex(tmp_path, monkeypatch):
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    inp = {"tool_name": "mcp__marrow__lie_down", "tool_input": {"rotate": True}}
    assert cortex_bridge._cortex_lie_down_deny(inp) is None


# --------------------------------------------------------------------------- #
# _window_spawn_epoch — real cortex transcripts open with timestamp-less
# metadata lines; the timestamp appears further down.
# --------------------------------------------------------------------------- #

def test_spawn_epoch_skips_leading_metadata(tmp_path):
    """First lines are metadata with no timestamp; spawn = the first timestamp
    a few lines down, not the fallback."""
    from datetime import datetime
    c = lambda o: json.dumps(o, separators=(",", ":"))  # compact, like real transcripts
    jl = tmp_path / "meta.jsonl"
    jl.write_text("\n".join([
        c({"type": "last-prompt", "content": "x"}),
        c({"type": "mode"}),
        c({"type": "permission-mode"}),
        c({"type": "file-history-snapshot"}),
        c({"timestamp": "2026-07-08T10:00:00+00:00", "type": "user"}),
        c({"message": {"usage": {"input_tokens": 10}}}),
    ]) + "\n")
    expected = datetime.fromisoformat("2026-07-08T10:00:00+00:00").timestamp()
    assert cortex_bridge._window_spawn_epoch(str(jl)) == expected


def test_spawn_epoch_falls_back_to_birthtime_not_mtime(tmp_path):
    """No timestamp anywhere → birthtime fallback, which must NOT grow as the
    live file is appended to (mtime would)."""
    jl = tmp_path / "no_ts.jsonl"
    jl.write_text(json.dumps({"type": "mode"}) + "\n")
    first = cortex_bridge._window_spawn_epoch(str(jl))
    assert first is not None
    time.sleep(0.02)
    with open(jl, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user"}) + "\n")
    assert cortex_bridge._window_spawn_epoch(str(jl)) == first


def test_spawn_epoch_missing_file_is_none():
    assert cortex_bridge._window_spawn_epoch("/no/such/file.jsonl") is None


def test_allow_rotate_after_metadata_transcript(tmp_path, monkeypatch):
    """Deny-loop regression: a transcript with leading metadata + a spawn line,
    handoff written after that spawn timestamp → guard allows."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = lambda o: json.dumps(o, separators=(",", ":"))  # compact, like real transcripts
    jl = tmp_path / "meta_big.jsonl"
    jl.write_text("\n".join([
        c({"type": "last-prompt"}),
        c({"type": "file-history-snapshot"}),
        c({"timestamp": "2026-07-08T10:00:00+00:00", "type": "user"}),
        c({"message": {"usage": {"input_tokens": 10_000}}}),
    ]) + "\n")
    spawn = datetime.fromisoformat("2026-07-08T10:00:00+00:00").timestamp()
    _handoff(tmp_path, monkeypatch, mtime=spawn + 5)
    inp = {"tool_name": "mcp__marrow__lie_down", "transcript_path": str(jl),
           "tool_input": {"rotate": True}}
    assert cortex_bridge._cortex_lie_down_deny(inp) is None


# --------------------------------------------------------------------------- #
# daily handoff page-turn
# --------------------------------------------------------------------------- #

def _page_turn_setup(tmp_path, monkeypatch, l1_date=None, template="# Title [YYYY-MM-DD]\n\nbody"):
    """Home dir with handoff.md (given L1 date) + a template file. Patches
    config.load() cortex section so home/template/archive_dir resolve here."""
    home = tmp_path / "cortex_home"
    home.mkdir(parents=True, exist_ok=True)
    hp = home / "handoff.md"
    l1 = f"# Title [{l1_date}]" if l1_date else "# Title"
    hp.write_text(f"{l1}\nyesterday's content", encoding="utf-8")
    (home / "handoff_template.md").write_text(template, encoding="utf-8")
    monkeypatch.setattr(cortex_bridge, "_cortex_handoff_path", lambda: hp)
    real_load = config.load

    def _patched_load():
        cfg = real_load()
        cfg = dict(cfg)
        cx = dict(cfg.get("cortex", {}))
        cx["home"] = str(home)
        cx["handoff_archive_dir"] = "handoff_archive"
        cx["handoff_template_file"] = "handoff_template.md"
        cfg["cortex"] = cx
        return cfg

    monkeypatch.setattr(config, "load", _patched_load)
    return home, hp


def test_page_turn_returns_no_content(tmp_path, monkeypatch):
    """SessionStart must not surface handoff content — the page-turn is a pure
    side effect; the user's cortex CLAUDE.md `@handoff.md` import is the read
    path now."""
    home, hp = _page_turn_setup(tmp_path, monkeypatch, l1_date="2026-07-01")
    assert cortex_bridge._cortex_handoff_page_turn_if_stale() is None


def test_page_turn_same_day_noop(tmp_path, monkeypatch):
    today = datetime.now(config.get_tz()).date().isoformat()
    home, hp = _page_turn_setup(tmp_path, monkeypatch, l1_date=today)
    cortex_bridge._cortex_handoff_page_turn_if_stale()
    assert hp.exists()
    assert "yesterday's content" in hp.read_text(encoding="utf-8")
    assert not (home / "handoff_archive").exists()


def test_page_turn_cross_day_archives_and_refreshes(tmp_path, monkeypatch):
    old_mtime = time.time() - 86400
    home, hp = _page_turn_setup(tmp_path, monkeypatch, l1_date="2026-07-01")
    os.utime(hp, (old_mtime, old_mtime))
    cortex_bridge._cortex_handoff_page_turn_if_stale()
    # archive file exists, holds the OLD content
    archived = home / "handoff_archive" / "2026-07-01.md"
    assert archived.exists()
    assert "yesterday's content" in archived.read_text(encoding="utf-8")
    # new file has today's date
    today = datetime.now(config.get_tz()).date().isoformat()
    new_text = hp.read_text(encoding="utf-8")
    assert f"[{today}]" in new_text
    assert "yesterday's content" not in new_text
    # new file mtime is in the past (backdated, not "written this window")
    assert hp.stat().st_mtime <= old_mtime + 1


def test_page_turn_unparsable_date_no_op(tmp_path, monkeypatch):
    home, hp = _page_turn_setup(tmp_path, monkeypatch, l1_date=None)
    cortex_bridge._cortex_handoff_page_turn_if_stale()
    assert hp.exists()
    assert not (home / "handoff_archive").exists()
    assert "yesterday's content" in hp.read_text(encoding="utf-8")


def test_page_turn_collision_suffix(tmp_path, monkeypatch):
    home, hp = _page_turn_setup(tmp_path, monkeypatch, l1_date="2026-07-01")
    archive_dir = home / "handoff_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "2026-07-01.md").write_text("existing", encoding="utf-8")
    cortex_bridge._cortex_handoff_page_turn_if_stale()
    assert (archive_dir / "2026-07-01.md").read_text(encoding="utf-8") == "existing"
    assert (archive_dir / "2026-07-01-2.md").exists()
