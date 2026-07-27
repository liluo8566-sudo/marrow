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
from datetime import datetime, timezone

import pytest

from marrow import config, cortex_bridge, hooks, storage, usage


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


def test_sessionstart_lines_cdx_seven_day_only():
    """Current live shape (5h window gone): only cdx_seven_day_pct in kv."""
    kv = {"cdx_seven_day_pct": "6"}
    lines = usage.sessionstart_lines(kv)
    assert "cdx 7d 6%" in lines[0]


def test_threshold_line_shows_main_occupancy_and_agent():
    kv = {"five_hour_pct": "20", "five_hour_reset_at": "2026-07-08T18:50:00+00:00"}
    line = usage.threshold_line(70_000, 120_000, kv)  # main=occupancy, agent=net
    assert line.startswith("Plan Used: 5h 20%")
    assert "Net Session Token: main 70k agent 120k" in line


# --------------------------------------------------------------------------- #
# read_kv: cdx_* staleness gate (only cdx_*, 5h/7d/net are never gated here)
# --------------------------------------------------------------------------- #

def test_read_kv_drops_stale_cdx_rows_only(monkeypatch, tmp_path):
    db = str(tmp_path / "kv.db")
    monkeypatch.setattr(config, "db_path", lambda: db)
    storage.init_db(db)
    conn = storage.connect(db)
    old = "2020-01-01T00:00:00+00:00"
    with conn:
        conn.executemany(
            "INSERT INTO ct_rate_limit (key, value, updated_at) VALUES (?, ?, ?)",
            [
                ("cdx_seven_day_pct", "6.0", old),
                ("five_hour_pct", "5", old),  # non-cdx: staleness gate must not touch it
            ],
        )
    conn.close()

    kv = usage.read_kv()
    assert "cdx_seven_day_pct" not in kv
    assert kv.get("five_hour_pct") == "5"


def test_read_kv_keeps_fresh_cdx_rows(monkeypatch, tmp_path):
    db = str(tmp_path / "kv2.db")
    monkeypatch.setattr(config, "db_path", lambda: db)
    storage.init_db(db)
    conn = storage.connect(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            "INSERT INTO ct_rate_limit (key, value, updated_at) VALUES (?, ?, ?)",
            ("cdx_seven_day_pct", "6.0", now_iso),
        )
    conn.close()

    assert usage.read_kv().get("cdx_seven_day_pct") == "6.0"


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
# line-count handoff page-turn (T5)
# --------------------------------------------------------------------------- #

_TEMPLATE = "\n".join([
    "# 手帐",
    "## 待办",
    "> ONLY before rotate",
    "## 日志",
    "### 前情",
    "### YYYY-MM-DD",
    "- HH:mm: <one line>",
]) + "\n"


def _handoff_body(todos, log_lines, log_date="2026-07-01"):
    """Build a handoff page with the given todos + activity lines."""
    lines = ["# 手帐", "## 待办", "> ONLY before rotate"]
    lines += todos
    lines += ["## 日志", "### 前情", f"### {log_date}"]
    lines += log_lines
    return "\n".join(lines) + "\n"


def _page_setup(tmp_path, monkeypatch, content, shell="1"):
    """Home dir with handoff-cli.md + template. Routes cortex config here and
    sets the shell env so _cortex_handoff_path resolves handoff-<shell>.md."""
    monkeypatch.setenv("MARROW_CORTEX", shell)
    home = tmp_path / "cortex_home"
    home.mkdir(parents=True, exist_ok=True)
    name = "handoff-tg.md" if shell == "tg" else "handoff-cli.md"
    hp = home / name
    hp.write_text(content, encoding="utf-8")
    (home / "handoff_template.md").write_text(_TEMPLATE, encoding="utf-8")
    real_load = config.load

    def _patched_load():
        cfg = dict(real_load())
        cx = dict(cfg.get("cortex", {}))
        cx["home"] = str(home)
        cx["handoff_archive_dir"] = "handoff_archive"
        cx["handoff_template_file"] = "handoff_template.md"
        cx["handoff_max_lines"] = 150
        cx["handoff_carry_lines"] = 10
        cx["handoff_todo_heading"] = "## 待办"
        cx["handoff_activity_heading"] = "## 日志"
        cx["handoff_carry_heading"] = "### 前情"
        cfg["cortex"] = cx
        return cfg

    monkeypatch.setattr(config, "load", _patched_load)
    return home, hp


def test_page_turn_returns_no_content(tmp_path, monkeypatch):
    body = _handoff_body(["[] a"], [f"- x{i}" for i in range(200)])
    home, hp = _page_setup(tmp_path, monkeypatch, body)
    assert cortex_bridge._cortex_handoff_page_turn_if_stale() is None


def test_page_turn_under_cap_noop(tmp_path, monkeypatch):
    body = _handoff_body(["[] keep"], [f"- x{i}" for i in range(140)])
    assert len(body.splitlines()) < 150
    home, hp = _page_setup(tmp_path, monkeypatch, body)
    cortex_bridge._cortex_handoff_page_turn_if_stale()
    assert hp.exists()
    assert "[] keep" in hp.read_text(encoding="utf-8")
    assert not (home / "handoff_archive").exists()


def test_page_turn_over_cap_archives_and_carries(tmp_path, monkeypatch):
    todos = ["[] survive", "[x] done", "[ ] survive2"]
    logs = [f"- HH:mm: line{i}" for i in range(160)]
    body = _handoff_body(todos, logs)
    assert len(body.splitlines()) > 150
    home, hp = _page_setup(tmp_path, monkeypatch, body)
    cortex_bridge._cortex_handoff_page_turn_if_stale()

    # archive: range name, holds the whole old page
    archived = home / "handoff_archive" / f"cli-2026-07-01~{datetime.now(config.get_tz()).strftime('%m-%d')}.md"
    assert archived.exists()
    assert "line0" in archived.read_text(encoding="utf-8")

    new_text = hp.read_text(encoding="utf-8")
    # unchecked todos survive, checked dropped
    assert "[] survive" in new_text
    assert "[ ] survive2" in new_text
    assert "[x] done" not in new_text
    # last 10 activity lines carried under 前情
    assert "line159" in new_text
    assert "line149" not in new_text


def test_page_turn_single_day_archive_name(tmp_path, monkeypatch):
    logs = [f"- l{i}" for i in range(160)]
    today = datetime.now(config.get_tz()).date().isoformat()
    body = _handoff_body([], logs, log_date=today)
    home, hp = _page_setup(tmp_path, monkeypatch, body)
    cortex_bridge._cortex_handoff_page_turn_if_stale()
    assert (home / "handoff_archive" / f"cli-{today}.md").exists()


def test_page_turn_collision_suffix(tmp_path, monkeypatch):
    logs = [f"- l{i}" for i in range(160)]
    today = datetime.now(config.get_tz()).date().isoformat()
    body = _handoff_body([], logs, log_date=today)
    home, hp = _page_setup(tmp_path, monkeypatch, body)
    archive_dir = home / "handoff_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / f"cli-{today}.md").write_text("existing", encoding="utf-8")
    cortex_bridge._cortex_handoff_page_turn_if_stale()
    assert (archive_dir / f"cli-{today}.md").read_text(encoding="utf-8") == "existing"
    assert (archive_dir / f"cli-{today}-2.md").exists()


def test_page_turn_legacy_migration(tmp_path, monkeypatch):
    """First cli run: legacy handoff.md migrates to handoff-cli.md before any
    page-turn check."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    home = tmp_path / "cortex_home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "handoff.md").write_text(_handoff_body(["[] t"], ["- l"]), encoding="utf-8")
    (home / "handoff_template.md").write_text(_TEMPLATE, encoding="utf-8")
    real_load = config.load

    def _patched_load():
        cfg = dict(real_load())
        cx = dict(cfg.get("cortex", {}))
        cx["home"] = str(home)
        cfg["cortex"] = cx
        return cfg

    monkeypatch.setattr(config, "load", _patched_load)
    cortex_bridge._cortex_handoff_page_turn_if_stale()
    assert (home / "handoff-cli.md").exists()
    assert not (home / "handoff.md").exists()


def test_page_turn_post_migration_is_noop(tmp_path, monkeypatch):
    """Legacy handoff.md gone + handoff-cli.md present (steady state): the
    migration branch is skipped and an under-cap page is left byte-untouched."""
    body = _handoff_body(["[] t"], ["- l"])
    home, hp = _page_setup(tmp_path, monkeypatch, body)
    before = hp.stat().st_mtime
    cortex_bridge._cortex_handoff_page_turn_if_stale()
    assert hp.read_text(encoding="utf-8") == body
    assert hp.stat().st_mtime == before
    assert not (home / "handoff.md").exists()
    assert not (home / "handoff_archive").exists()
