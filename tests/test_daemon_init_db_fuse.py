"""Tests for the init_db lock fuse: busy_ms param + _init_db_guarded helper."""
from __future__ import annotations

import sqlite3
import time

import pytest

from marrow import daemon, storage


@pytest.fixture()
def tmp_db(tmp_path):
    db = str(tmp_path / "fuse_test.db")
    storage.init_db(db).close()
    return db


@pytest.fixture()
def empty_db(tmp_path):
    """A fresh DB path with no schema (for normal init_db tests)."""
    return str(tmp_path / "empty.db")


def _hold_lock(db: str):
    """Return a connection holding a write transaction on db."""
    conn = sqlite3.connect(db)
    conn.execute("BEGIN IMMEDIATE")
    return conn


# ── (a) busy_ms makes init_db raise promptly when DB is locked ────────────────

def test_init_db_raises_promptly_on_locked_db(tmp_db):
    holder = _hold_lock(tmp_db)
    try:
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            storage.init_db(tmp_db, busy_ms=100).close()
        elapsed = time.monotonic() - t0
        # busy_ms=100 → should time out in ~0.1s, well under 2s
        assert elapsed < 2.0, f"init_db stalled {elapsed:.2f}s (expected < 2s)"
    finally:
        holder.close()


# ── (b) _init_db_guarded returns without raising on a locked DB ───────────────

def test_init_db_guarded_does_not_raise_on_locked_db(tmp_db, monkeypatch):
    monkeypatch.setattr(daemon, "_DB", tmp_db)
    holder = _hold_lock(tmp_db)
    try:
        # Must return (not raise) even though DB is locked
        daemon._init_db_guarded()
    finally:
        holder.close()


# ── (c) normal init_db (no busy_ms) works on a free DB ───────────────────────

def test_init_db_no_busy_ms_works_on_free_db(empty_db):
    conn = storage.init_db(empty_db)
    try:
        # Verify schema was created — alerts table is a reliable sentinel
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'"
        ).fetchone()
        assert row is not None, "alerts table missing after init_db"
    finally:
        conn.close()


# ── (d) _init_db_guarded writes an alert row after a lock contention ─────────
# Simulate init_db raising locked, but leave DB free so the alert write lands.

def test_init_db_guarded_writes_alert_on_locked_db(tmp_db, monkeypatch):
    monkeypatch.setattr(daemon, "_DB", tmp_db)

    # Simulate init_db raising OperationalError("database is locked") while
    # the actual DB remains accessible for the subsequent alert insert.
    def _fake_init_db(path, *, busy_ms=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(daemon.storage, "init_db", _fake_init_db)

    daemon._init_db_guarded()

    conn = sqlite3.connect(tmp_db)
    try:
        row = conn.execute(
            "SELECT severity, type, message, source FROM alerts"
            " WHERE type='daemon_init_db_locked' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "expected an alert row after simulated lock contention"
    assert row[0] == "warn"
    assert "locked" in row[2].lower()
    assert row[3] == "daemon.py"
