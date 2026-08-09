"""Shared embed service — one process on the machine holds the bge-m3 model.

Run as `python -m marrow.embedd` (launchd: com.marrow.embedd). Listens on a
unix stream socket, newline-delimited JSON:

    {"op": "embed", "texts": [...]} -> {"ok": true, "vecs": <b64 f32>, "n": N, "dim": D}
    {"op": "ping"}                  -> {"ok": true, "loaded": bool}
    error                           -> {"ok": false, "error": "..."}

The model loads on the first embed request and is dropped again after
[embedd].idle_minutes without one, so idle RSS returns to the OS.

Client side: `client_embed` is used by recall.embed_texts, which falls back to
an in-process model when this service is absent or unreachable. The service
process itself never routes through the socket — it calls the local loader.
"""
from __future__ import annotations

import base64
import fcntl
import gc
import json
import logging
import logging.handlers
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from . import config
from .paths import paths

if TYPE_CHECKING:
    from numpy.typing import NDArray

_ALERT_TYPE = "embedd"
_ALERT_FINGERPRINT = "embedd_service_unreachable"
_STAMP_NAME = "embedd_alert.stamp"
_LOCK_NAME = "embedd.lock"
_SOCK_NAME = "embedd.sock"
_MAX_LINE = 64 * 1024 * 1024
_IS_SERVICE = False


def _cfg() -> dict:
    return config.load().get("embedd", {}) or {}


def enabled() -> bool:
    return bool(_cfg().get("enabled"))


def socket_path() -> Path:
    override = str(_cfg().get("socket_path") or "").strip()
    if override:
        return Path(override).expanduser()
    return paths.state_dir / _SOCK_NAME


def is_service_process() -> bool:
    return _IS_SERVICE


def _logger() -> logging.Logger:
    log = logging.getLogger("marrow.embedd")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    d = Path(config.DATA_DIR) / "logs" / "embedd"
    d.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        d / "embedd.log", when="midnight", backupCount=7, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    log.addHandler(handler)
    log.propagate = False
    return log


# ── protocol ─────────────────────────────────────────────────────────────────

class ServiceAbsent(Exception):
    """No socket file — the service is simply not installed/running."""


class ServiceUnreachable(Exception):
    """Socket file exists but connect/IO failed, or the service returned an error."""


def encode_message(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def decode_message(line: bytes) -> dict:
    obj = json.loads(line.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("message is not an object")
    return obj


def pack_vecs(vecs: NDArray[np.float32]) -> dict:
    arr = np.ascontiguousarray(vecs, dtype=np.float32)
    if arr.ndim != 2:
        arr = arr.reshape(1, -1)
    return {
        "vecs": base64.b64encode(arr.tobytes()).decode("ascii"),
        "n": int(arr.shape[0]),
        "dim": int(arr.shape[1]),
    }


def unpack_vecs(payload: dict) -> NDArray[np.float32]:
    raw = base64.b64decode(payload["vecs"])
    n = int(payload["n"])
    dim = int(payload["dim"])
    arr = np.frombuffer(raw, dtype=np.float32)
    if arr.size != n * dim:
        raise ValueError(f"vec payload size {arr.size} != {n}*{dim}")
    return arr.reshape(n, dim).copy()


# ── client ───────────────────────────────────────────────────────────────────

def client_embed(texts: list[str]) -> NDArray[np.float32]:
    """Embed via the service. Raises ServiceAbsent / ServiceUnreachable."""
    cfg = _cfg()
    path = socket_path()
    if not path.exists():
        raise ServiceAbsent(str(path))
    connect_timeout = float(cfg.get("connect_timeout_s") or 0.5)
    read_timeout = float(cfg.get("read_timeout_s") or 120)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(connect_timeout)
        sock.connect(str(path))
        sock.settimeout(read_timeout)
        with sock, sock.makefile("rwb") as f:
            f.write(encode_message({"op": "embed", "texts": list(texts)}))
            f.flush()
            line = f.readline(_MAX_LINE)
    except (OSError, socket.timeout) as e:
        raise ServiceUnreachable(f"{path}: {e}") from e
    if not line:
        raise ServiceUnreachable(f"{path}: empty response")
    try:
        resp = decode_message(line)
    except (ValueError, UnicodeDecodeError) as e:
        raise ServiceUnreachable(f"{path}: bad response ({e})") from e
    if not resp.get("ok"):
        raise ServiceUnreachable(f"{path}: {resp.get('error')}")
    try:
        return unpack_vecs(resp)
    except (KeyError, ValueError) as e:
        raise ServiceUnreachable(f"{path}: bad vec payload ({e})") from e


def ping() -> dict | None:
    """{'ok': True, 'loaded': bool} or None when the service is not answering."""
    path = socket_path()
    if not path.exists():
        return None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(float(_cfg().get("connect_timeout_s") or 0.5))
        sock.connect(str(path))
        with sock, sock.makefile("rwb") as f:
            f.write(encode_message({"op": "ping"}))
            f.flush()
            line = f.readline(_MAX_LINE)
        return decode_message(line) if line else None
    except (OSError, socket.timeout, ValueError, UnicodeDecodeError):
        return None


def _stamp_path() -> Path:
    return paths.state_dir / _STAMP_NAME


def alert_unreachable(detail: str) -> bool:
    """Warn alert for a dead-but-present socket, at most once per cooldown.

    Returns True when an alert was emitted. Never raises — a failed embed must
    not become a failed caller.
    """
    cooldown = float(_cfg().get("alert_cooldown_hours") or 6) * 3600.0
    stamp = _stamp_path()
    now = time.time()
    try:
        if stamp.exists() and (now - stamp.stat().st_mtime) < cooldown:
            return False
    except OSError:
        pass
    try:
        from . import repo
        repo.add_alert(
            "warn", _ALERT_TYPE, _ALERT_FINGERPRINT,
            source="embedd.py",
            message=f"embed service socket present but unusable: {detail}",
            db=config.db_path(),
        )
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(int(now)), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


# ── model holder ─────────────────────────────────────────────────────────────

class ModelHolder:
    """Owns the single in-process embedder and evicts it once idle."""

    def __init__(self, idle_seconds: float, clock=time.monotonic) -> None:
        self._idle = idle_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._model = None
        self._last_used = clock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def embed(self, texts: list[str]) -> NDArray[np.float32]:
        with self._lock:
            if self._model is None:
                from . import recall
                self._model = recall._ensure_embedder()
                if self._model is None:
                    raise RuntimeError("bge-m3 model files not found")
            self._last_used = self._clock()
            try:
                return self._model.embed(texts)
            finally:
                self._last_used = self._clock()

    def evict_if_idle(self) -> bool:
        """Drop the model when idle past the threshold. True when evicted."""
        if self._idle <= 0:
            return False
        with self._lock:
            if self._model is None:
                return False
            if (self._clock() - self._last_used) < self._idle:
                return False
            self._model = None
        from . import recall
        recall._release_embedder()
        gc.collect()
        return True


def _evictor(holder: ModelHolder, stop: threading.Event, tick_s: float) -> None:
    while not stop.wait(tick_s):
        try:
            if holder.evict_if_idle():
                _logger().info("model evicted after idle")
        except Exception:  # noqa: BLE001
            _logger().exception("evictor tick failed")


# ── server ───────────────────────────────────────────────────────────────────

class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        holder: ModelHolder = self.server.holder
        while True:
            line = self.rfile.readline(_MAX_LINE)
            if not line:
                return
            try:
                req = decode_message(line)
            except (ValueError, UnicodeDecodeError) as e:
                self._send({"ok": False, "error": f"bad request: {e}"})
                return
            op = req.get("op")
            if op == "ping":
                self._send({"ok": True, "loaded": holder.loaded})
                continue
            if op != "embed":
                self._send({"ok": False, "error": f"unknown op: {op!r}"})
                continue
            texts = req.get("texts")
            if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                self._send({"ok": False, "error": "texts must be a list of strings"})
                continue
            if not texts:
                self._send({"ok": True, **pack_vecs(np.zeros((0, 0), dtype=np.float32))})
                continue
            try:
                vecs = holder.embed(texts)
            except Exception as e:  # noqa: BLE001
                _logger().exception("embed failed")
                self._send({"ok": False, "error": f"{type(e).__name__}: {e}"})
                continue
            self._send({"ok": True, **pack_vecs(vecs)})

    def _send(self, obj: dict) -> None:
        try:
            self.wfile.write(encode_message(obj))
            self.wfile.flush()
        except OSError:
            pass


class EmbeddServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, holder: ModelHolder) -> None:
        self.holder = holder
        super().__init__(path, _Handler)


def serve(path: Path, holder: ModelHolder) -> EmbeddServer:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = EmbeddServer(str(path), holder)
    path.chmod(0o600)
    return server


def main() -> int:
    global _IS_SERVICE
    log = _logger()
    cfg = _cfg()
    if not cfg.get("enabled"):
        log.info("embedd disabled — exiting")
        return 0
    lock_path = Path(config.DATA_DIR) / _LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.info("another embedd instance holds the lock — exiting")
            return 0
        _IS_SERVICE = True
        idle_s = float(cfg.get("idle_minutes") or 30) * 60.0
        tick_s = max(5.0, min(60.0, idle_s / 4 if idle_s > 0 else 60.0))
        holder = ModelHolder(idle_s)
        stop = threading.Event()
        threading.Thread(
            target=_evictor, args=(holder, stop, tick_s), daemon=True,
        ).start()
        path = socket_path()
        server = serve(path, holder)
        log.info("embedd listening on %s idle_minutes=%s", path, idle_s / 60.0)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            server.server_close()
            path.unlink(missing_ok=True)
            _IS_SERVICE = False
            log.info("embedd stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
