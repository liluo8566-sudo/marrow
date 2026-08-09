"""embedd: protocol round-trip, client fallback paths, idle eviction.

Nothing here loads bge-m3 or touches launchd — the model holder is fed a fake
embedder and the server (when one is started) runs on a tmp socket.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from marrow import embedd, recall


class _FakeEmbedder:
    """Deterministic stand-in for _BgeM3Embedder."""

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return np.array(
            [[float(len(t)) + i for i in range(self.dim)] for t in texts],
            dtype=np.float32,
        )


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def sock_path(monkeypatch):
    # AF_UNIX paths cap at ~104 bytes; pytest's tmp_path is already over it.
    d = Path(tempfile.mkdtemp(prefix="mwemb", dir="/tmp"))
    p = d / "e.sock"
    monkeypatch.setattr(embedd, "socket_path", lambda: p)
    try:
        yield p
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(embedd.paths, "state_dir", d)
    return d


# ── protocol ─────────────────────────────────────────────────────────────────

def test_encode_decode_round_trip():
    msg = {"op": "embed", "texts": ["hello", "世界"]}
    assert embedd.encode_message(msg).endswith(b"\n")
    assert embedd.decode_message(embedd.encode_message(msg)) == msg


def test_pack_unpack_vecs_round_trip():
    vecs = np.arange(6, dtype=np.float32).reshape(2, 3)
    payload = embedd.pack_vecs(vecs)
    assert payload["n"] == 2 and payload["dim"] == 3
    back = embedd.unpack_vecs(payload)
    assert back.dtype == np.float32
    assert np.array_equal(back, vecs)


def test_pack_vecs_promotes_1d():
    payload = embedd.pack_vecs(np.ones(5, dtype=np.float32))
    assert (payload["n"], payload["dim"]) == (1, 5)


def test_unpack_vecs_rejects_size_mismatch():
    payload = embedd.pack_vecs(np.ones((2, 3), dtype=np.float32))
    payload["dim"] = 4
    with pytest.raises(ValueError):
        embedd.unpack_vecs(payload)


def test_decode_message_rejects_non_object():
    with pytest.raises(ValueError):
        embedd.decode_message(b"[1, 2]\n")


# ── live server over a tmp socket ────────────────────────────────────────────

@pytest.fixture()
def server(sock_path, monkeypatch):
    holder = embedd.ModelHolder(idle_seconds=0)
    fake = _FakeEmbedder()
    monkeypatch.setattr(recall, "_ensure_embedder", lambda: fake)
    srv = embedd.serve(sock_path, holder)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv, holder, fake
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


@pytest.mark.live_embedd
def test_client_embed_round_trip(server, monkeypatch):
    _srv, holder, fake = server
    monkeypatch.setattr(embedd, "enabled", lambda: True)
    assert not holder.loaded
    vecs = embedd.client_embed(["ab", "cde"])
    assert vecs.shape == (2, 4)
    assert np.array_equal(vecs, fake.embed(["ab", "cde"]))
    assert holder.loaded


@pytest.mark.live_embedd
def test_ping_reports_loaded_state(server):
    _srv, _holder, _fake = server
    assert embedd.ping() == {"ok": True, "loaded": False}
    embedd.client_embed(["x"])
    assert embedd.ping() == {"ok": True, "loaded": True}


@pytest.mark.live_embedd
def test_server_rejects_unknown_op(server, sock_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(sock_path))
        with s.makefile("rwb") as f:
            f.write(embedd.encode_message({"op": "nope"}))
            f.flush()
            resp = json.loads(f.readline())
    assert resp["ok"] is False and "nope" in resp["error"]


@pytest.mark.live_embedd
def test_server_rejects_bad_texts(server, sock_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(sock_path))
        with s.makefile("rwb") as f:
            f.write(embedd.encode_message({"op": "embed", "texts": [1, 2]}))
            f.flush()
            resp = json.loads(f.readline())
    assert resp["ok"] is False and "list of strings" in resp["error"]


@pytest.mark.live_embedd
def test_serve_replaces_stale_socket_file(sock_path, monkeypatch):
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.write_text("stale")
    holder = embedd.ModelHolder(idle_seconds=0)
    srv = embedd.serve(sock_path, holder)
    try:
        assert sock_path.is_socket()
    finally:
        srv.server_close()


@pytest.mark.live_embedd
def test_recall_embed_texts_uses_service(server, monkeypatch):
    _srv, _holder, fake = server
    monkeypatch.setattr(embedd, "enabled", lambda: True)

    def _no_local():
        raise AssertionError("must not load the model locally")

    monkeypatch.setattr(recall, "_ensure_embedder", _no_local)
    # The server thread keeps its own reference to the fake embedder loaded
    # before the local loader was neutered.
    _srv.holder._model = fake
    vecs = recall.embed_texts(["hi"])
    assert vecs is not None and vecs.shape == (1, 4)


# ── client fallback ──────────────────────────────────────────────────────────

def test_embed_texts_falls_back_silently_when_socket_absent(
    sock_path, state_dir, monkeypatch
):
    monkeypatch.setattr(embedd, "enabled", lambda: True)
    fake = _FakeEmbedder()
    monkeypatch.setattr(recall, "_ensure_embedder", lambda: fake)
    alerts: list = []
    monkeypatch.setattr(embedd, "alert_unreachable", lambda d: alerts.append(d))

    assert not sock_path.exists()
    vecs = recall.embed_texts(["hi"])

    assert vecs is not None and vecs.shape == (1, 4)
    assert fake.calls == [["hi"]]
    assert alerts == []


def test_embed_texts_falls_back_and_alerts_when_socket_dead(
    sock_path, state_dir, monkeypatch
):
    monkeypatch.setattr(embedd, "enabled", lambda: True)
    sock_path.write_text("not a socket")
    fake = _FakeEmbedder()
    monkeypatch.setattr(recall, "_ensure_embedder", lambda: fake)
    alerts: list = []
    monkeypatch.setattr(embedd, "alert_unreachable", lambda d: alerts.append(d))

    vecs = recall.embed_texts(["hi"])

    assert vecs is not None and vecs.shape == (1, 4)
    assert fake.calls == [["hi"]]
    assert len(alerts) == 1


def test_embed_texts_returns_none_when_service_off_and_no_model(monkeypatch):
    monkeypatch.setattr(embedd, "enabled", lambda: False)
    monkeypatch.setattr(recall, "_ensure_embedder", lambda: None)
    assert recall.embed_texts(["hi"]) is None


def test_embed_texts_empty_input_never_touches_service(monkeypatch):
    def _boom():
        raise AssertionError("should not be consulted for empty input")

    monkeypatch.setattr(embedd, "enabled", _boom)
    assert recall.embed_texts([]).shape == (0, 0)


def test_service_process_never_routes_through_socket(monkeypatch):
    monkeypatch.setattr(embedd, "enabled", lambda: True)
    monkeypatch.setattr(embedd, "is_service_process", lambda: True)

    def _boom(_texts):
        raise AssertionError("service must not call itself over the socket")

    monkeypatch.setattr(embedd, "client_embed", _boom)
    fake = _FakeEmbedder()
    monkeypatch.setattr(recall, "_ensure_embedder", lambda: fake)
    assert recall.embed_texts(["hi"]).shape == (1, 4)


# ── alert rate limit ─────────────────────────────────────────────────────────

def test_alert_unreachable_rate_limited(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(embedd, "_cfg", lambda: {"alert_cooldown_hours": 6})
    monkeypatch.setattr(embedd.config, "db_path", lambda: str(tmp_path / "a.db"))
    calls: list = []
    monkeypatch.setattr(
        "marrow.repo.add_alert", lambda *a, **kw: calls.append((a, kw)) or 1,
    )

    assert embedd.alert_unreachable("boom") is True
    assert embedd.alert_unreachable("boom again") is False
    assert len(calls) == 1
    assert (state_dir / "embedd_alert.stamp").exists()


def test_alert_unreachable_fires_again_after_cooldown(
    state_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(embedd, "_cfg", lambda: {"alert_cooldown_hours": 6})
    monkeypatch.setattr(embedd.config, "db_path", lambda: str(tmp_path / "a.db"))
    calls: list = []
    monkeypatch.setattr(
        "marrow.repo.add_alert", lambda *a, **kw: calls.append((a, kw)) or 1,
    )

    assert embedd.alert_unreachable("boom") is True
    stamp = state_dir / "embedd_alert.stamp"
    stale = time.time() - 7 * 3600
    os.utime(stamp, (stale, stale))
    assert embedd.alert_unreachable("boom") is True
    assert len(calls) == 2


# ── idle eviction ────────────────────────────────────────────────────────────

def test_holder_evicts_after_idle(monkeypatch):
    clock = _Clock()
    fake = _FakeEmbedder()
    released: list = []
    monkeypatch.setattr(recall, "_ensure_embedder", lambda: fake)
    monkeypatch.setattr(recall, "_release_embedder", lambda: released.append(1))

    holder = embedd.ModelHolder(idle_seconds=60, clock=clock)
    holder.embed(["x"])
    assert holder.loaded

    clock.t = 59.0
    assert holder.evict_if_idle() is False
    assert holder.loaded

    clock.t = 61.0
    assert holder.evict_if_idle() is True
    assert not holder.loaded
    assert released == [1]

    # Next request reloads.
    holder.embed(["y"])
    assert holder.loaded


def test_holder_never_evicts_when_idle_disabled(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(recall, "_ensure_embedder", lambda: _FakeEmbedder())
    holder = embedd.ModelHolder(idle_seconds=0, clock=clock)
    holder.embed(["x"])
    clock.t = 10_000.0
    assert holder.evict_if_idle() is False
    assert holder.loaded


def test_holder_evict_noop_when_not_loaded():
    holder = embedd.ModelHolder(idle_seconds=1, clock=_Clock())
    assert holder.evict_if_idle() is False


def test_holder_raises_when_model_files_absent(monkeypatch):
    monkeypatch.setattr(recall, "_ensure_embedder", lambda: None)
    holder = embedd.ModelHolder(idle_seconds=0)
    with pytest.raises(RuntimeError):
        holder.embed(["x"])


def test_release_embedder_clears_singleton(monkeypatch):
    monkeypatch.setattr(recall, "_EMBEDDER", _FakeEmbedder())
    recall._release_embedder()
    assert recall._EMBEDDER is None
