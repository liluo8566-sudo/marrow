"""Tests for _vitals_fragment in marrow.hooks.inject."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from marrow import config
from marrow.hooks.inject import _vitals_fragment

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
