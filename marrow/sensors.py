"""OwnTracks-compatible HTTP receiver — geofence transitions -> location state.

POST /pub with the OwnTracks JSON body (any app sending the same shape works).
Transitions append to state/sensors/transitions.jsonl and collapse into
state/sensors/location.json. Off unless [sensors].enabled.
"""
from __future__ import annotations

import base64
import datetime
import hmac
import json
import logging
import logging.handlers
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, repo
from ._atomic import atomic_write
from .paths import paths

_ALERT_TYPE = "sensor_silent"
_FINGERPRINT = "sensor_silent:owntracks"
_WATCHDOG_INTERVAL_S = 3600
_LOCK = threading.Lock()
_BLANK = {"zone": None, "since": None, "seeded": False, "prev": None, "last_seen": None}


def _cfg() -> dict:
    return config.load().get("sensors", {})


def _logger() -> logging.Logger:
    log = logging.getLogger("marrow.sensors")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    d = Path(config.DATA_DIR) / "logs" / "sensors"
    d.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        d / "sensors.log", when="midnight", backupCount=7, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    log.addHandler(handler)
    log.propagate = False
    return log


# ── state files ──────────────────────────────────────────────────────────────

def _state_dir() -> Path:
    d = paths.state_dir / "sensors"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _location_file() -> Path:
    return _state_dir() / "location.json"


def _transitions_file() -> Path:
    return _state_dir() / "transitions.jsonl"


def _now() -> datetime.datetime:
    return datetime.datetime.now(config.get_tz())


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _epoch_iso(tst: object) -> str | None:
    """OwnTracks `tst` (unix seconds) -> local ISO string."""
    try:
        epoch = int(tst)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    utc = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return _iso(utc.astimezone(config.get_tz()))


def _read_state() -> dict:
    try:
        data = json.loads(_location_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_BLANK)
    if not isinstance(data, dict):
        return dict(_BLANK)
    return {**_BLANK, **data}


def _write_state(state: dict) -> None:
    atomic_write(str(_location_file()),
                 json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _current_event(state: dict) -> dict | None:
    """Newest transition rebuilt from state — the value copied into `prev`.

    A leave stores zone=null, so the zone name it left is read back off `prev`
    (the enter that preceded it).
    """
    since = state.get("since")
    if not since:
        return None
    zone = state.get("zone")
    if zone:
        return {"event": "enter", "zone": zone, "ts": since}
    return {"event": "leave", "zone": (state.get("prev") or {}).get("zone"), "ts": since}


# ── payload handling ─────────────────────────────────────────────────────────

def handle_payload(payload: dict) -> dict:
    """Fold one POSTed payload into location.json. Returns the new state."""
    with _LOCK:
        state = _read_state()
        ptype = payload.get("_type")
        event = payload.get("event")
        if ptype == "transition" and event in ("enter", "leave"):
            desc = payload.get("desc") or ""
            ts = _epoch_iso(payload.get("tst")) or _iso(_now())
            cur = _current_event(state)
            dup = (cur is not None and cur["event"] == event
                   and cur["zone"] == desc and cur["ts"] == ts)
            if not dup:
                with _transitions_file().open("a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                state = {
                    "zone": desc if event == "enter" else None,
                    "since": ts,
                    "seeded": False,
                    "prev": cur,
                }
        elif ptype == "location" and not state.get("since"):
            regions = payload.get("inregions") or []
            if isinstance(regions, list) and regions:
                state = {**state, "zone": regions[0], "since": None, "seeded": True}
        state = {**_BLANK, **state, "last_seen": _iso(_now())}
        _write_state(state)
        return state


# ── silent watchdog ──────────────────────────────────────────────────────────

def check_silent(*, now: datetime.datetime | None = None) -> bool:
    """Alert when no POST has landed for silent_alert_hours. True if raised."""
    hours = float(_cfg().get("silent_alert_hours") or 0)
    if hours <= 0:
        return False
    last = _read_state().get("last_seen")
    if not last:
        return False
    try:
        dt = datetime.datetime.fromisoformat(str(last))
    except ValueError:
        return False
    ref = now or _now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ref.tzinfo)
    if (ref - dt).total_seconds() < hours * 3600:
        return False
    repo.add_alert("warn", _ALERT_TYPE, fingerprint=_FINGERPRINT,
                   message=f"no OwnTracks POST for >{hours:g}h", source="sensors.py")
    return True


def _watchdog_loop(stop: threading.Event) -> None:
    log = _logger()
    while not stop.wait(_WATCHDOG_INTERVAL_S):
        try:
            check_silent()
        except Exception:  # noqa: BLE001
            log.exception("silent watchdog failed")


# ── HTTP ─────────────────────────────────────────────────────────────────────

class SensorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "marrow-sensors"

    def do_POST(self) -> None:  # noqa: N802
        if not self._auth_ok():
            self._reply(401, b"unauthorized",
                        {"WWW-Authenticate": 'Basic realm="marrow"'})
            return
        if self.path.split("?")[0].rstrip("/") != "/pub":
            self._reply(404, b"not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            try:
                handle_payload(payload)
            except Exception:  # noqa: BLE001
                _logger().exception("payload handling failed")
        # OwnTracks contract: always 200 with an empty command list.
        self._reply(200, b"[]", {"Content-Type": "application/json"})

    def _auth_ok(self) -> bool:
        cfg = _cfg()
        user = str(cfg.get("auth_user") or "")
        if not user:
            return True
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:  # noqa: BLE001
            return False
        return hmac.compare_digest(decoded, f"{user}:{cfg.get('auth_pass') or ''}")

    def _reply(self, code: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        _logger().info("%s - %s", self.address_string(), fmt % args)


def main() -> int:
    log = _logger()
    cfg = _cfg()
    if not cfg.get("enabled"):
        log.info("sensors disabled — exiting")
        return 0
    bind = str(cfg.get("bind") or "0.0.0.0")
    port = int(cfg.get("port") or 8043)
    _state_dir()
    stop = threading.Event()
    threading.Thread(target=_watchdog_loop, args=(stop,), daemon=True).start()
    server = ThreadingHTTPServer((bind, port), SensorHandler)
    log.info("sensors listening on %s:%s", bind, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        log.info("sensors stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
