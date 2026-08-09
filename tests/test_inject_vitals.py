"""Tests for _vitals_fragment and _phone_app_fragment in marrow.hooks.inject."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from marrow import config
from marrow.hooks.inject import _vitals_fragment, _phone_app_fragment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FRESH_SNAP = {
    "city": "测试市",
    "lon": "20.0000",
    "lat": "10.0000",
    "temperature": "30°C",
    "weather": "局部多云",
    "battery_pct": "56",
    "steps_today": "4868",
    "ts": "",  # filled per-test
}

# Fictional coordinates used throughout.  "家" exercises unicode zone names.
_ZONES = [
    {"name": "家", "lat": 10.0000, "lon": 20.0000, "radius_m": 300},
    {"name": "work", "lat": 10.0100, "lon": 20.0100, "radius_m": 250},
]


def _fresh_ts(offset_s: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=offset_s)
    return dt.isoformat()


def _make_snap(extra: dict | None = None, offset_s: int = 30) -> dict:
    snap = dict(_FRESH_SNAP)
    snap["ts"] = _fresh_ts(offset_s)
    if extra:
        snap.update(extra)
    return snap


def _write_snap(path: Path, snap: dict) -> None:
    path.write_text(json.dumps(snap), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_inject_cfg_cache():
    """Clear the inject module config cache before each test so monkeypatched
    config.load values are always picked up immediately."""
    import marrow.hooks.inject as _inj
    _inj._CFG_CACHE.clear()
    yield
    _inj._CFG_CACHE.clear()


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Patch config so DATA_DIR points to tmp_path and vitals_file is set."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    vf = tmp_path / "vitals.json"
    # Capture original config once before any patching of config.load.
    _orig_cfg = config.load()

    def _make_cfg(zones=None, interval=60, stale=90):
        import copy
        base = copy.deepcopy(_orig_cfg)
        base.setdefault("turn_inject", {})
        base["turn_inject"]["vitals_file"] = str(vf)
        base["turn_inject"]["vitals_interval_min"] = interval
        base["turn_inject"]["vitals_stale_min"] = stale
        base["turn_inject"]["vitals_zones"] = zones if zones is not None else _ZONES
        return base

    return tmp_path, vf, _make_cfg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_off_when_vitals_file_empty(tmp_path, monkeypatch):
    """Returns '' when vitals_file is empty (feature off)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    base = config.load()
    base.setdefault("turn_inject", {})["vitals_file"] = ""
    monkeypatch.setattr(config, "load", lambda: base)
    result = _vitals_fragment("sid1")
    assert result == ""


def test_fresh_line_with_zone_match(isolated, monkeypatch):
    """Zone inside radius → zone label in output; missing key omitted."""
    tmp_path, vf, make_cfg = isolated
    snap = _make_snap()
    _write_snap(vf, snap)
    monkeypatch.setattr(config, "load", lambda: make_cfg())
    result = _vitals_fragment("sid2")
    assert "📍 家" in result
    assert "🔋56%" in result
    assert "30°C" in result
    assert "局部多云" in result
    assert "今日4868步" in result


def test_fresh_line_omits_missing_keys(isolated, monkeypatch):
    """Keys absent from snapshot are silently omitted from the line."""
    tmp_path, vf, make_cfg = isolated
    snap = _make_snap()
    del snap["steps_today"]
    del snap["battery_pct"]
    _write_snap(vf, snap)
    monkeypatch.setattr(config, "load", lambda: make_cfg())
    result = _vitals_fragment("sid3")
    assert "今日" not in result
    assert "🔋" not in result
    assert "📍 家" in result


def test_unknown_zone_renders_coords(isolated, monkeypatch):
    """Coords outside all zones → 外面(lat,lon) label."""
    tmp_path, vf, make_cfg = isolated
    snap = _make_snap()
    snap["lat"] = "40.0000"
    snap["lon"] = "116.0000"
    _write_snap(vf, snap)
    monkeypatch.setattr(config, "load", lambda: make_cfg())
    result = _vitals_fragment("sid4")
    assert "外面(40.0000,116.0000)" in result


def test_stale_renders_warning(isolated, monkeypatch):
    """Age > stale_min → warning line with elapsed time."""
    tmp_path, vf, make_cfg = isolated
    snap = _make_snap(offset_s=6000)  # 100 min old > 90 min stale_min
    _write_snap(vf, snap)
    monkeypatch.setattr(config, "load", lambda: make_cfg(stale=90))
    result = _vitals_fragment("sid5")
    assert "⚠️" in result
    assert "没上报" in result
    # Should still have zone label in stale line
    assert "家" in result


def test_throttle_second_call_within_interval(isolated, monkeypatch):
    """Second call within interval → returns ''."""
    tmp_path, vf, make_cfg = isolated
    snap = _make_snap()
    _write_snap(vf, snap)
    monkeypatch.setattr(config, "load", lambda: make_cfg(interval=60))
    first = _vitals_fragment("sid6")
    assert first  # emitted
    second = _vitals_fragment("sid6")
    assert second == ""  # throttled


def test_throttle_zone_change_injects_despite_interval(isolated, monkeypatch):
    """Zone change overrides interval throttle."""
    tmp_path, vf, make_cfg = isolated
    snap = _make_snap()
    _write_snap(vf, snap)
    monkeypatch.setattr(config, "load", lambda: make_cfg(interval=60))
    first = _vitals_fragment("sid7")
    assert first  # emitted at 家

    # Now move to work zone.
    snap2 = _make_snap()
    snap2["lat"] = "10.0100"
    snap2["lon"] = "20.0100"
    _write_snap(vf, snap2)
    second = _vitals_fragment("sid7")
    assert "work" in second  # zone changed → inject


def test_throttle_interval_elapsed_injects(isolated, monkeypatch):
    """After interval elapses, inject again."""
    tmp_path, vf, make_cfg = isolated
    snap = _make_snap()
    _write_snap(vf, snap)
    monkeypatch.setattr(config, "load", lambda: make_cfg(interval=60))
    first = _vitals_fragment("sid8")
    assert first

    # Manually backdate the state file so interval appears elapsed.
    state_file = tmp_path / "state" / "vitals_inject" / "sid8"
    old_ts = time.time() - 3700  # >60 min ago
    state_file.write_text(json.dumps({"ts": old_ts, "zone": "家"}), encoding="utf-8")

    second = _vitals_fragment("sid8")
    assert second  # interval elapsed


# ---------------------------------------------------------------------------
# _phone_app_fragment format tests (live path, was: _last_app_segment)
# These redirect the old _last_app_segment unit tests to the live path.
# Each uses a unique sid so the new-ping gate never blocks the first call.
# ---------------------------------------------------------------------------

def _make_ping(app: str, offset_s: int) -> dict:
    dt = datetime.now(timezone.utc) - timedelta(seconds=offset_s)
    return {"app": app, "event": "open", "ts": dt.isoformat()}


def _make_pings_cfg(pings_path: Path) -> dict:
    return {"pings_file": str(pings_path)}


def _pings_cfg(tmp_path, pf_path, monkeypatch):
    """Monkeypatch config so _phone_app_fragment uses tmp_path state + given pings file."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    base = config.load()
    base.setdefault("turn_inject", {})["pings_file"] = str(pf_path)
    monkeypatch.setattr(config, "load", lambda: base)


def test_pings_off_when_key_unset(tmp_path, monkeypatch):
    """Returns '' when pings_file is absent from config."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    base = config.load()
    base.setdefault("turn_inject", {})["pings_file"] = ""
    monkeypatch.setattr(config, "load", lambda: base)
    result = _phone_app_fragment("sid_pings_unset")
    assert result == ""


def test_pings_off_when_key_empty(tmp_path, monkeypatch):
    """Returns '' when pings_file is an empty string."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    base = config.load()
    base.setdefault("turn_inject", {})["pings_file"] = ""
    monkeypatch.setattr(config, "load", lambda: base)
    result = _phone_app_fragment("sid_pings_empty")
    assert result == ""


def test_pings_recent_minutes(tmp_path, monkeypatch):
    """Ping from 5 minutes ago → '📱 {app} 5m前'."""
    pf = tmp_path / "pings.json"
    pf.write_text(json.dumps([_make_ping("小红书", 300)]), encoding="utf-8")
    _pings_cfg(tmp_path, pf, monkeypatch)
    result = _phone_app_fragment("sid_pings_min")
    assert "📱 小红书 5m前" == result


def test_pings_recent_seconds(tmp_path, monkeypatch):
    """Ping from 30 seconds ago → '📱 {app} 刚刚'."""
    pf = tmp_path / "pings.json"
    pf.write_text(json.dumps([_make_ping("微信", 30)]), encoding="utf-8")
    _pings_cfg(tmp_path, pf, monkeypatch)
    result = _phone_app_fragment("sid_pings_sec")
    assert "📱 微信 刚刚" == result


def test_pings_hours_old(tmp_path, monkeypatch):
    """Ping from 3 hours ago → '📱 {app} 3h前'."""
    pf = tmp_path / "pings.json"
    pf.write_text(json.dumps([_make_ping("抖音", 10800)]), encoding="utf-8")
    _pings_cfg(tmp_path, pf, monkeypatch)
    result = _phone_app_fragment("sid_pings_hr")
    assert "📱 抖音 3h前" == result


def test_pings_empty_file(tmp_path, monkeypatch):
    """Empty array → ''."""
    pf = tmp_path / "pings.json"
    pf.write_text("[]", encoding="utf-8")
    _pings_cfg(tmp_path, pf, monkeypatch)
    result = _phone_app_fragment("sid_pings_empty_file")
    assert result == ""


def test_pings_malformed_entry_best_valid_wins(tmp_path, monkeypatch):
    """Malformed entries are skipped; max-by-ts valid entry is used.

    Previously: last-by-position (wrong). Now: max-by-ts.
    telegram at -600s is the only valid entry, so it must win.
    """
    pf = tmp_path / "pings.json"
    entries = [
        _make_ping("telegram", 600),          # valid, 10m ago
        {"app": "", "event": "open", "ts": datetime.now(timezone.utc).isoformat()},  # empty app
        {"app": "微博", "event": "open", "ts": "not-a-date"},                         # bad ts
    ]
    pf.write_text(json.dumps(entries), encoding="utf-8")
    _pings_cfg(tmp_path, pf, monkeypatch)
    result = _phone_app_fragment("sid_pings_malformed")
    assert "📱 telegram 10m前" == result


def test_pings_missing_file(tmp_path, monkeypatch):
    """Non-existent pings file → ''."""
    _pings_cfg(tmp_path, tmp_path / "nonexistent.json", monkeypatch)
    result = _phone_app_fragment("sid_pings_missing")
    assert result == ""


def test_pings_segment_not_in_vitals_line(isolated, monkeypatch, tmp_path):
    """📱 segment must NOT appear inside _vitals_fragment (decoupled)."""
    _tmp, vf, make_cfg = isolated
    snap = _make_snap()
    _write_snap(vf, snap)
    pf = tmp_path / "pings.json"
    pf.write_text(json.dumps([_make_ping("YouTube", 120)]), encoding="utf-8")
    cfg = make_cfg()
    cfg["turn_inject"]["pings_file"] = str(pf)
    monkeypatch.setattr(config, "load", lambda: cfg)
    result = _vitals_fragment("sid_pings_decoupled")
    assert "📱" not in result


def test_stray_space_keys_parsed(isolated, monkeypatch):
    """Snapshot with stray-space keys (e.g. ' lat') is handled correctly."""
    tmp_path, vf, make_cfg = isolated
    snap = _make_snap()
    # Re-key with stray spaces as some producers emit.
    stray = {f" {k}": v for k, v in snap.items()}
    _write_snap(vf, stray)
    monkeypatch.setattr(config, "load", lambda: make_cfg())
    result = _vitals_fragment("sid9")
    assert "📍 家" in result
    assert "🔋56%" in result


# ---------------------------------------------------------------------------
# _phone_app_fragment gate-behavior tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_isolated(tmp_path, monkeypatch):
    """Patch config so DATA_DIR points to tmp_path and pings_file is set."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    pf = tmp_path / "pings.json"
    _orig_cfg = config.load()

    def _make_cfg(pings_path=None):
        import copy
        base = copy.deepcopy(_orig_cfg)
        base.setdefault("turn_inject", {})
        base["turn_inject"]["pings_file"] = str(pings_path or pf)
        return base

    return tmp_path, pf, _make_cfg


def test_app_fragment_off_when_key_unset(tmp_path, monkeypatch):
    """Returns '' when pings_file is absent from config."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    base = config.load()
    base.setdefault("turn_inject", {})["pings_file"] = ""
    monkeypatch.setattr(config, "load", lambda: base)
    result = _phone_app_fragment("sidA1")
    assert result == ""


def test_app_fragment_first_turn_emits(app_isolated, monkeypatch):
    """First turn (no state file) → emits if a ping exists."""
    tmp_path, pf, make_cfg = app_isolated
    pf.write_text(json.dumps([_make_ping("微信", 30)]), encoding="utf-8")
    monkeypatch.setattr(config, "load", lambda: make_cfg())
    result = _phone_app_fragment("sidA2")
    assert "📱 微信 刚刚" == result


def test_app_fragment_same_ping_second_turn_returns_empty(app_isolated, monkeypatch):
    """Second call with same ping ts → '' (gated out)."""
    tmp_path, pf, make_cfg = app_isolated
    pf.write_text(json.dumps([_make_ping("微信", 30)]), encoding="utf-8")
    monkeypatch.setattr(config, "load", lambda: make_cfg())
    first = _phone_app_fragment("sidA3")
    assert first  # emitted
    second = _phone_app_fragment("sidA3")
    assert second == ""  # same ping, gated


def test_app_fragment_new_ping_emits_again(app_isolated, monkeypatch):
    """Newer ping ts → emits again despite prior stamp."""
    tmp_path, pf, make_cfg = app_isolated
    ts1 = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    pf.write_text(json.dumps([{"app": "抖音", "event": "open", "ts": ts1}]), encoding="utf-8")
    monkeypatch.setattr(config, "load", lambda: make_cfg())
    first = _phone_app_fragment("sidA4")
    assert "📱 抖音" in first

    # Write a newer ping.
    ts2 = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    pf.write_text(json.dumps([
        {"app": "抖音", "event": "open", "ts": ts1},
        {"app": "小红书", "event": "open", "ts": ts2},
    ]), encoding="utf-8")
    second = _phone_app_fragment("sidA4")
    assert "📱 小红书" in second  # new ping → emit


def test_app_fragment_no_pings_returns_empty(app_isolated, monkeypatch):
    """Empty pings array → '' (first turn too)."""
    tmp_path, pf, make_cfg = app_isolated
    pf.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(config, "load", lambda: make_cfg())
    result = _phone_app_fragment("sidA5")
    assert result == ""


def test_app_fragment_missing_file_returns_empty(app_isolated, monkeypatch):
    """Non-existent pings file → ''."""
    tmp_path, pf, make_cfg = app_isolated
    monkeypatch.setattr(config, "load", lambda: make_cfg(pings_path=tmp_path / "missing.json"))
    result = _phone_app_fragment("sidA6")
    assert result == ""


# ---------------------------------------------------------------------------
# B1: naive timestamps treated as UTC
# ---------------------------------------------------------------------------

def test_vitals_naive_ts_treated_as_utc(isolated, monkeypatch):
    """B1: a naive timestamp in the snapshot must not be misread as local time.

    When the tracker writes a naive UTC timestamp (no +HH:MM suffix), the
    fragment must treat it as UTC, not as local time. A snapshot stamped
    30 seconds ago (naive UTC) must render as fresh, not stale.
    """
    tmp_path, vf, make_cfg = isolated
    snap = _make_snap(offset_s=30)
    # Strip the +00:00 suffix to make the timestamp naive.
    snap["ts"] = snap["ts"].replace("+00:00", "").replace("Z", "")
    _write_snap(vf, snap)
    monkeypatch.setattr(config, "load", lambda: make_cfg(stale=90))
    result = _vitals_fragment("sid_naive_ts")
    # A naive ts 30s ago treated as UTC → fresh → no ⚠️ stale warning.
    assert "⚠️" not in result
    assert "📍" in result


def test_pings_naive_ts_treated_as_utc(tmp_path, monkeypatch):
    """B1: naive ping timestamp must be treated as UTC when computing age."""
    pf = tmp_path / "pings.json"
    dt_naive = (datetime.now(timezone.utc) - timedelta(seconds=300)).replace(tzinfo=None)
    pf.write_text(json.dumps([{"app": "小红书", "event": "open",
                                "ts": dt_naive.isoformat()}]), encoding="utf-8")
    _pings_cfg(tmp_path, pf, monkeypatch)
    result = _phone_app_fragment("sid_naive_ping")
    # 300s = 5 minutes; naive treated as UTC → "5m前" not "1x h前" or "⚠"
    assert "📱 小红书 5m前" == result


# ---------------------------------------------------------------------------
# B5: max-by-ts picks the latest ping regardless of array order
# ---------------------------------------------------------------------------

def test_pings_max_by_ts_not_last_by_position(tmp_path, monkeypatch):
    """B5: the newest ping by timestamp wins, even if it appears earlier in the array."""
    pf = tmp_path / "pings.json"
    ts_newer = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    ts_older = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    # Newer entry appears FIRST; last-by-position would pick the older one.
    entries = [
        {"app": "小红书", "event": "open", "ts": ts_newer},
        {"app": "抖音", "event": "open", "ts": ts_older},
    ]
    pf.write_text(json.dumps(entries), encoding="utf-8")
    _pings_cfg(tmp_path, pf, monkeypatch)
    result = _phone_app_fragment("sid_max_ts")
    assert "📱 小红书 1m前" == result
