"""sensors.py — OwnTracks receiver: payload folding, HTTP contract, watchdog.

No launchd, no real config writes: _state_dir and _cfg are patched per test and
the HTTP tests bind loopback on an ephemeral port.
"""
from __future__ import annotations

import base64
import datetime
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from marrow import config, sensors

T0 = 1753830000  # arbitrary fixed epochs
T1 = T0 + 3600
T2 = T0 + 7200


def _iso(epoch: int) -> str:
    return sensors._epoch_iso(epoch)


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    d = tmp_path / "sensors"
    d.mkdir()
    monkeypatch.setattr(sensors, "_state_dir", lambda: d)
    return d


@pytest.fixture
def cfg(monkeypatch):
    values = {
        "enabled": True, "port": 0, "bind": "127.0.0.1",
        "auth_user": "", "auth_pass": "", "silent_alert_hours": 24,
    }
    monkeypatch.setattr(sensors, "_cfg", lambda: values)
    return values


def _transition(event: str, desc: str, tst: int) -> dict:
    return {"_type": "transition", "event": event, "desc": desc, "tst": tst}


def _lines(state_dir) -> list[dict]:
    f = state_dir / "transitions.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x]


# ── T1: config ───────────────────────────────────────────────────────────────

def test_sensors_section_defaults_off():
    s = config.load()["sensors"]
    assert s["enabled"] is False
    assert s["port"] == 8043
    assert s["bind"] == "0.0.0.0"
    assert s["auth_user"] == "" and s["auth_pass"] == ""
    assert s["silent_alert_hours"] == 24


# ── T2: payload folding ──────────────────────────────────────────────────────

def test_enter_sets_zone_and_appends_transition(state_dir, cfg):
    st = sensors.handle_payload(_transition("enter", "Deakin", T0))
    assert st["zone"] == "Deakin"
    assert st["since"] == _iso(T0)
    assert st["seeded"] is False
    assert st["prev"] is None
    assert st["last_seen"]
    assert len(_lines(state_dir)) == 1


def test_leave_clears_zone_and_keeps_prev_enter(state_dir, cfg):
    sensors.handle_payload(_transition("enter", "Home", T0))
    st = sensors.handle_payload(_transition("leave", "Home", T1))
    assert st["zone"] is None
    assert st["since"] == _iso(T1)
    assert st["prev"] == {"event": "enter", "zone": "Home", "ts": _iso(T0)}
    assert len(_lines(state_dir)) == 2


def test_two_hop_leave_then_enter(state_dir, cfg):
    sensors.handle_payload(_transition("enter", "Home", T0))
    sensors.handle_payload(_transition("leave", "Home", T1))
    st = sensors.handle_payload(_transition("enter", "Deakin", T2))
    assert st["zone"] == "Deakin"
    assert st["since"] == _iso(T2)
    assert st["prev"] == {"event": "leave", "zone": "Home", "ts": _iso(T1)}
    assert len(_lines(state_dir)) == 3


def test_duplicate_transition_ignored(state_dir, cfg):
    sensors.handle_payload(_transition("enter", "Deakin", T0))
    first = json.loads((state_dir / "location.json").read_text())
    st = sensors.handle_payload(_transition("enter", "Deakin", T0))
    assert len(_lines(state_dir)) == 1
    assert st["since"] == first["since"]
    assert st["prev"] is None


def test_location_seeds_zone_on_cold_start(state_dir, cfg):
    st = sensors.handle_payload({"_type": "location", "inregions": ["Deakin"]})
    assert st["zone"] == "Deakin"
    assert st["since"] is None
    assert st["seeded"] is True
    assert _lines(state_dir) == []


def test_location_does_not_override_transition_zone(state_dir, cfg):
    sensors.handle_payload(_transition("enter", "Home", T0))
    st = sensors.handle_payload({"_type": "location", "inregions": ["Deakin"]})
    assert st["zone"] == "Home"
    assert st["since"] == _iso(T0)
    assert st["seeded"] is False


def test_other_type_updates_last_seen_only(state_dir, cfg):
    sensors.handle_payload(_transition("enter", "Home", T0))
    before = json.loads((state_dir / "location.json").read_text())
    st = sensors.handle_payload({"_type": "waypoints"})
    assert st["zone"] == "Home" and st["since"] == before["since"]
    assert st["last_seen"]


# ── T2: HTTP contract ────────────────────────────────────────────────────────

@pytest.fixture
def server(state_dir, cfg):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), sensors.SensorHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _post(srv, path: str, body: dict | None = None, headers: dict | None = None):
    conn = http.client.HTTPConnection(*srv.server_address[:2], timeout=5)
    try:
        conn.request("POST", path, json.dumps(body or {}), headers or {})
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def test_post_pub_returns_empty_command_list(server, state_dir):
    status, body = _post(server, "/pub", _transition("enter", "Deakin", T0))
    assert status == 200
    assert body == b"[]"
    assert json.loads((state_dir / "location.json").read_text())["zone"] == "Deakin"


def test_unknown_path_404(server):
    status, _ = _post(server, "/nope", {"_type": "location"})
    assert status == 404


def test_bad_auth_401_and_good_auth_200(server, cfg, state_dir):
    cfg["auth_user"] = "u"
    cfg["auth_pass"] = "p"
    status, _ = _post(server, "/pub", {"_type": "location"})
    assert status == 401
    bad = base64.b64encode(b"u:wrong").decode()
    status, _ = _post(server, "/pub", {"_type": "location"},
                      {"Authorization": f"Basic {bad}"})
    assert status == 401
    good = base64.b64encode(b"u:p").decode()
    status, body = _post(server, "/pub", _transition("enter", "Home", T0),
                         {"Authorization": f"Basic {good}"})
    assert status == 200 and body == b"[]"


def test_malformed_body_still_200(server, state_dir):
    conn = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    try:
        conn.request("POST", "/pub", "{not json")
        r = conn.getresponse()
        assert r.status == 200 and r.read() == b"[]"
    finally:
        conn.close()


def test_main_exits_when_disabled(monkeypatch, state_dir):
    monkeypatch.setattr(sensors, "_cfg", lambda: {"enabled": False})

    def _boom(*_a, **_k):
        raise AssertionError("server must not start when disabled")

    monkeypatch.setattr(sensors, "ThreadingHTTPServer", _boom)
    assert sensors.main() == 0


# ── T4: silent watchdog ──────────────────────────────────────────────────────

@pytest.fixture
def alerts(monkeypatch):
    calls = []
    monkeypatch.setattr(sensors.repo, "add_alert",
                        lambda *a, **k: calls.append((a, k)) or 1)
    return calls


def _seed_last_seen(hours_ago: float) -> None:
    ref = sensors._now() - datetime.timedelta(hours=hours_ago)
    sensors._write_state({**sensors._BLANK, "zone": "Deakin",
                          "since": _iso(T0), "last_seen": sensors._iso(ref)})


def test_watchdog_alerts_when_silent(state_dir, cfg, alerts):
    _seed_last_seen(25)
    assert sensors.check_silent() is True
    assert len(alerts) == 1
    args, kwargs = alerts[0]
    assert args[:2] == ("warn", "sensor_silent")
    assert kwargs["fingerprint"] == "sensor_silent:owntracks"
    assert kwargs["source"] == "sensors.py"
    assert "24h" in kwargs["message"]


def test_watchdog_quiet_when_fresh(state_dir, cfg, alerts):
    _seed_last_seen(1)
    assert sensors.check_silent() is False
    assert alerts == []


def test_watchdog_quiet_without_state(state_dir, cfg, alerts):
    assert sensors.check_silent() is False
    assert alerts == []


def test_watchdog_disabled_by_zero_hours(state_dir, cfg, alerts):
    cfg["silent_alert_hours"] = 0
    _seed_last_seen(99)
    assert sensors.check_silent() is False
    assert alerts == []


# ── T3: install gating ───────────────────────────────────────────────────────

def test_sensors_plist_registered_and_gated(monkeypatch):
    from marrow import install
    assert ("mw-sensors.plist", "com.marrow.sensors") in install._ALL_PLISTS
    assert (install._REPO_ROOT / "deploy" / "mw-sensors.plist").exists()
    monkeypatch.setattr(config, "load", lambda: {"sensors": {"enabled": False}})
    assert install._plist_gate_open("com.marrow.sensors") is False
    monkeypatch.setattr(config, "load", lambda: {"sensors": {"enabled": True}})
    assert install._plist_gate_open("com.marrow.sensors") is True
    assert install._plist_gate_open("com.marrow.watcher") is True


def test_install_plists_skips_sensors_when_disabled(monkeypatch, tmp_path):
    from marrow import install
    monkeypatch.setattr(install, "_LAUNCH_AGENTS", tmp_path / "agents")
    monkeypatch.setattr(install, "_CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(install, "_uid", lambda: "501")
    monkeypatch.setattr(install, "_launchctl", lambda *a: (0, ""))
    monkeypatch.setattr(config, "load", lambda: {"sensors": {"enabled": False}})
    assert install.install_plists() is True
    written = {p.name for p in (tmp_path / "agents").iterdir()}
    assert "com.marrow.sensors.plist" not in written
    assert "com.marrow.watcher.plist" in written


def test_install_plists_writes_sensors_when_enabled(monkeypatch, tmp_path):
    from marrow import install
    monkeypatch.setattr(install, "_LAUNCH_AGENTS", tmp_path / "agents")
    monkeypatch.setattr(install, "_CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(install, "_uid", lambda: "501")
    monkeypatch.setattr(install, "_launchctl", lambda *a: (0, ""))
    monkeypatch.setattr(config, "load", lambda: {"sensors": {"enabled": True}})
    assert install.install_plists() is True
    text = (tmp_path / "agents" / "com.marrow.sensors.plist").read_text()
    assert "marrow.sensors" in text
    assert "__VENV_PYTHON__" not in text
