"""Smoke test: SyncLoop + AtlasSweepLoop + UsageSnapshotLoop start/stop cleanly."""
from __future__ import annotations

import sqlite3
import time

from marrow.sync_loop import AtlasSweepLoop, SyncLoop, SyncTarget, UsageSnapshotLoop


def _factory(tmp_path):
    db = str(tmp_path / "smoke.db")
    def _mk() -> sqlite3.Connection:
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c
    return _mk


def test_sync_loop_conn_factory_start_stop(tmp_path):
    md = tmp_path / "t.md"
    md.write_text("x")
    t = SyncTarget(
        name="smoke",
        md_path=str(md),
        db_mtime_fn=lambda c: None,
        render_fn=lambda c: None,
        has_md_to_db=False,
    )
    loop = SyncLoop(_factory(tmp_path), [t], tick_s=10.0)
    loop.start()
    time.sleep(0.05)
    loop.stop(timeout=2.0)
    assert loop._thread is not None
    assert not loop._thread.is_alive()


def test_atlas_sweep_loop_conn_factory_start_stop(tmp_path, monkeypatch):
    # Monkeypatch drift_sweep.AUTHORIZED_ROOTS to an empty list so sweep_once
    # has nothing to walk — just verifies no crash.
    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [])

    sweep = AtlasSweepLoop(_factory(tmp_path), tick_s=10.0)
    sweep.start()
    time.sleep(0.05)
    sweep.stop(timeout=2.0)
    assert sweep._thread is not None
    assert not sweep._thread.is_alive()


def test_usage_snapshot_loop_start_stop(monkeypatch):
    # fetch_and_write stubbed — this is a lifecycle smoke test, not a network call.
    from marrow import usage_snapshot
    monkeypatch.setattr(usage_snapshot, "fetch_and_write", lambda: None)

    loop = UsageSnapshotLoop(tick_s=10.0)
    loop.start()
    time.sleep(0.05)
    loop.stop(timeout=2.0)
    assert loop._thread is not None
    assert not loop._thread.is_alive()


def test_usage_snapshot_loop_survives_error(monkeypatch):
    """A raised UsageSnapshotError (or any exception) inside the tick must not
    kill the thread — it should be caught and logged, loop keeps running."""
    from marrow import usage_snapshot

    def _boom():
        raise usage_snapshot.UsageSnapshotError("no token")
    monkeypatch.setattr(usage_snapshot, "fetch_and_write", _boom)

    loop = UsageSnapshotLoop(tick_s=10.0)
    loop.start()
    time.sleep(0.05)
    assert loop._thread.is_alive()  # still alive despite the boot-tick error
    loop.stop(timeout=2.0)
    assert not loop._thread.is_alive()
