"""Presence heartbeat: throttle, config gating, degraded probes, hook wiring."""
from __future__ import annotations

import io
import json
import time
from datetime import datetime, timedelta

import pytest

from marrow import config, hooks, presence
from marrow.paths import paths as _paths

CFG = {"presence": {"enabled": True, "interval_min": 30,
                    "channels": ["cli", "tg", "wx"]},
       "cortex": {"away_idle_min": 30}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_paths, "state_dir", tmp_path / "state")
    monkeypatch.delenv("MARROW_CHANNEL", raising=False)
    monkeypatch.setattr(presence, "_frontmost_app", lambda: "Telegram")
    monkeypatch.setattr(presence, "_idle_seconds", lambda: 12)
    monkeypatch.setattr(config, "load", lambda: json.loads(json.dumps(CFG)))
    return tmp_path


def _cfg(monkeypatch, **presence_over):
    cfg = json.loads(json.dumps(CFG))
    cfg["presence"].update(presence_over)
    monkeypatch.setattr(config, "load", lambda: cfg)
    return cfg


def _write_location(tmp_path, payload):
    d = tmp_path / "state" / "sensors"
    d.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        (d / "location.json").write_text(payload)
    else:
        (d / "location.json").write_text(json.dumps(payload))


def _ago(minutes: int) -> str:
    dt = datetime.now(config.get_tz()) - timedelta(minutes=minutes)
    return dt.isoformat(timespec="seconds")


def _stamp(tmp_path, sid, epoch):
    d = tmp_path / "state" / "presence"
    d.mkdir(parents=True, exist_ok=True)
    (d / sid).write_text(str(int(epoch)))


# ── rendering ────────────────────────────────────────────────────────────────

def test_live_shape_renders_zone_and_duration(env):
    _write_location(env, {"zone": "Home", "since": _ago(200), "seeded": False,
                          "prev": {"event": "leave", "zone": "Deakin",
                                   "ts": _ago(232)},
                          "last_seen": _ago(199)})
    assert presence.render("s1") == "📍 Home (3h20m) · 💻 Active: Telegram"


def test_between_zones_renders_out(env):
    _write_location(env, {"zone": None, "since": _ago(45),
                          "prev": {"event": "leave", "zone": "Deakin",
                                   "ts": _ago(45)}})
    assert presence.render("s1").startswith("📍 out (45m) · ")


@pytest.mark.parametrize("minutes,expected", [
    (45, "45m"), (60, "1h0m"), (135, "2h15m"), (23 * 60 + 59, "23h59m"),
    (24 * 60, "1d0h"), (25 * 60, "1d1h"), (3 * 24 * 60 + 2 * 60, "3d2h"),
])
def test_duration_tiers(minutes, expected):
    assert presence._duration(minutes * 60) == expected


def test_long_stay_renders_days_not_raw_hours(env):
    _write_location(env, {"zone": "Home", "since": _ago(24 * 60 + 23)})
    assert presence.render("s1").startswith("📍 Home (1d0h) · ")


def test_idle_over_threshold_renders_inactive(env, monkeypatch):
    monkeypatch.setattr(presence, "_frontmost_app", lambda: "Safari")
    monkeypatch.setattr(presence, "_idle_seconds", lambda: 25 * 60)
    monkeypatch.setattr(config, "load", lambda: {
        "presence": CFG["presence"], "cortex": {"away_idle_min": 20}})
    assert presence.render("s1") == "💻 Inactive: 25m Safari"


def test_away_idle_min_defaults_to_30_when_absent(env, monkeypatch):
    monkeypatch.setattr(presence, "_idle_seconds", lambda: 25 * 60)
    monkeypatch.setattr(config, "load", lambda: {"presence": CFG["presence"]})
    assert presence.render("s1") == "💻 Active: Telegram"


# ── degraded inputs ──────────────────────────────────────────────────────────

def test_missing_location_file_leaves_activity_only(env):
    assert presence.render("s1") == "💻 Active: Telegram"


def test_corrupt_location_file_invents_nothing(env):
    _write_location(env, "{not json")
    assert presence.render("s1") == "💻 Active: Telegram"


def test_location_without_zone_or_prev_is_omitted(env):
    _write_location(env, {"zone": None, "since": _ago(10), "prev": None})
    assert presence.render("s1") == "💻 Active: Telegram"


def test_osascript_failure_degrades_to_location_only(env, monkeypatch):
    monkeypatch.setattr(presence, "_frontmost_app", lambda: None)
    _write_location(env, {"zone": "Home", "since": _ago(30)})
    assert presence.render("s1") == "📍 Home (30m)"


def test_everything_unknown_renders_empty(env, monkeypatch):
    monkeypatch.setattr(presence, "_frontmost_app", lambda: None)
    monkeypatch.setattr(presence, "_idle_seconds", lambda: None)
    assert presence.render("s1") == ""


def test_probes_run_concurrently(env, monkeypatch):
    delay = 0.3

    def _slow_app():
        time.sleep(delay)
        return "Safari"

    def _slow_idle():
        time.sleep(delay)
        return 0

    monkeypatch.setattr(presence, "_frontmost_app", _slow_app)
    monkeypatch.setattr(presence, "_idle_seconds", _slow_idle)
    start = time.monotonic()
    assert presence._activity_piece(30) == "💻 Active: Safari"
    elapsed = time.monotonic() - start
    assert elapsed < delay * 1.6


def test_probe_exception_degrades_instead_of_raising(env, monkeypatch):
    def _boom():
        raise RuntimeError("wedged")

    monkeypatch.setattr(presence, "_idle_seconds", _boom)
    _write_location(env, {"zone": "Home", "since": _ago(30)})
    assert presence.render("s1") == "📍 Home (30m)"


def test_probes_are_never_spawned_for_real(env, monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(presence.subprocess, "run", _boom)
    assert presence.render("s1") == "💻 Active: Telegram"


# ── throttle ─────────────────────────────────────────────────────────────────

def test_fresh_session_injects_on_first_turn(env):
    assert presence.render("brand-new") == "💻 Active: Telegram"
    assert (env / "state" / "presence" / "brand-new").exists()


def test_second_turn_inside_window_is_silent(env):
    assert presence.render("s1")
    assert presence.render("s1") == ""


def test_turn_past_the_window_injects_again(env):
    assert presence.render("s1")
    _stamp(env, "s1", time.time() - 31 * 60)
    assert presence.render("s1") == "💻 Active: Telegram"


def test_no_catch_up_backlog_after_long_silence(env):
    _stamp(env, "s1", time.time() - 5 * 3600)
    assert presence.render("s1")
    assert presence.render("s1") == ""


def test_stamp_advances_even_when_nothing_is_knowable(env, monkeypatch):
    monkeypatch.setattr(presence, "_frontmost_app", lambda: None)
    monkeypatch.setattr(presence, "_idle_seconds", lambda: None)
    assert presence.render("s1") == ""
    assert (env / "state" / "presence" / "s1").exists()


# ── config gating ────────────────────────────────────────────────────────────

def test_disabled_injects_nothing(env, monkeypatch):
    _cfg(monkeypatch, enabled=False)
    assert presence.render("s1") == ""
    assert not (env / "state" / "presence" / "s1").exists()


def test_channel_absent_from_list_is_silent(env, monkeypatch):
    _cfg(monkeypatch, channels=["tg", "wx"])
    assert presence.render("s1") == ""


def test_listed_channel_injects(env, monkeypatch):
    monkeypatch.setenv("MARROW_CHANNEL", "tg")
    _cfg(monkeypatch, channels=["tg"])
    assert presence.render("s1") == "💻 Active: Telegram"


def test_interval_zero_injects_every_turn(env, monkeypatch):
    _cfg(monkeypatch, interval_min=0)
    assert presence.render("s1")
    assert presence.render("s1") == "💻 Active: Telegram"


def test_empty_sid_is_silent(env):
    assert presence.render("") == ""


# ── hook wiring ──────────────────────────────────────────────────────────────

def _run_turn_inject(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    hooks.turn_inject()
    raw = out.getvalue()
    if not raw.strip():
        return ""
    return json.loads(raw)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("channel", ["cli", "wx"])
def test_turn_inject_carries_presence_on_both_paths(tmp_path, monkeypatch, channel):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MARROW_CHANNEL", channel)
    monkeypatch.setattr(presence, "render", lambda sid: "📍 Home (1h) · 💻 Active: iTerm2")
    out = _run_turn_inject(monkeypatch, {"session_id": "w1",
                                         "transcript_path": "/t/x.jsonl",
                                         "prompt": "hi"})
    assert "📍 Home (1h) · 💻 Active: iTerm2" in out


@pytest.mark.parametrize("channel", ["cli", "wx"])
def test_presence_crash_never_breaks_the_turn(tmp_path, monkeypatch, channel):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("MARROW_CHANNEL", channel)

    def _boom(sid):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(presence, "render", _boom)
    out = _run_turn_inject(monkeypatch, {"session_id": "w1",
                                         "transcript_path": "/t/x.jsonl",
                                         "prompt": "hi"})
    assert "probe exploded" not in out
    if channel == "cli":
        assert "# Context" in out


def test_subagent_transcript_gets_no_presence(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(presence, "render", lambda sid: "📍 Home (1h)")
    out = _run_turn_inject(monkeypatch, {"session_id": "w1",
                                         "transcript_path": "/x/tasks/a.jsonl",
                                         "prompt": "hi"})
    assert out == ""
