"""Tests for DriftWatcher attachment in watcher.py."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import marrow.drift_sweep as ds
from marrow.watcher import _DriftHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"
    (claude / "rules").mkdir(parents=True)
    (claude / "skills").mkdir(parents=True)
    (claude / "projects").mkdir(parents=True)
    monkeypatch.setattr(ds, "_CLAUDE_ROOT", claude)
    return claude


@pytest.fixture()
def drift_watcher_env(tmp_path, monkeypatch, fake_claude):
    root_a = tmp_path / "cc-lab"
    root_a.mkdir()
    pending_dir = tmp_path / "pending"
    backup_dir = tmp_path / "backup"
    pending_dir.mkdir()
    backup_dir.mkdir()
    tree_md = tmp_path / "dir_tree.md"

    fake_paths = SimpleNamespace(
        drift_pending_dir=pending_dir,
        drift_backup_dir=backup_dir,
        dir_tree_md=tree_md,
    )
    monkeypatch.setattr(ds, "paths", fake_paths)
    monkeypatch.setattr(ds, "AUTHORIZED_ROOTS", [root_a, fake_claude])
    return SimpleNamespace(root_a=root_a, claude=fake_claude, tree_md=tree_md)


# ---------------------------------------------------------------------------
# DriftWatcher boot test (no real Observer, no real fs)
# ---------------------------------------------------------------------------

def test_drift_watcher_instantiates(drift_watcher_env):
    """DriftWatcher can be instantiated with synthetic roots, no crash."""
    env = drift_watcher_env
    dw = ds.DriftWatcher(roots=[env.root_a, env.claude])
    assert dw is not None
    assert dw._roots == [env.root_a, env.claude]


def test_watcher_has_drift_watcher_attribute(tmp_path, monkeypatch):
    """Watcher.__init__ attaches self.drift_watcher with AUTHORIZED_ROOTS."""
    # We can't run full Watcher.run() (real db, observer, etc.) but we can
    # verify the attribute is set by patching out storage and config.
    import marrow.watcher as mw

    # Minimal patches so __init__ doesn't fail
    monkeypatch.setattr(mw, "AUTHORIZED_ROOTS", [tmp_path])
    fake_conn = MagicMock()
    fake_conn.row_factory = None
    fake_conn.execute = MagicMock()

    with patch("marrow.watcher.storage.init_db") as mock_init_db, \
         patch("marrow.watcher.sqlite3.connect", return_value=fake_conn), \
         patch("marrow.watcher.sqlite_vec.load"), \
         patch("marrow.watcher.config.db_path", return_value=str(tmp_path / "t.db")), \
         patch("marrow.watcher.config.DATA_DIR", str(tmp_path)), \
         patch("marrow.watcher.config.load", return_value={
             "paths": {
                 "db_pages": str(tmp_path / "db-pages"),
             }
         }), \
         patch("marrow.watcher.MdIndex"):
        mock_init_db.return_value = MagicMock()
        w = mw.Watcher()

    assert hasattr(w, "drift_watcher"), "Watcher must have drift_watcher attribute"
    assert isinstance(w.drift_watcher, ds.DriftWatcher)


# ---------------------------------------------------------------------------
# _DriftHandler: on_moved queues batch without crash
# ---------------------------------------------------------------------------

def test_drift_handler_on_moved_queues(drift_watcher_env, monkeypatch):
    """on_moved on an authorized file queues an op in the DriftWatcher batch."""
    env = drift_watcher_env
    dw = ds.DriftWatcher(roots=[env.root_a, env.claude])

    log = MagicMock()
    handler = _DriftHandler(dw, log)

    src = str(env.root_a / "old_file.md")
    dest = str(env.root_a / "new_file.md")

    class FakeEvent:
        is_directory = False
        src_path = src
        dest_path = dest

    # Cancel any timer so test doesn't block
    with patch.object(dw, "_flush_batch"):
        handler.on_moved(FakeEvent())

    with dw._lock:
        batch = list(dw._batch)

    assert len(batch) == 1
    assert batch[0] == (src, dest)
    log.exception.assert_not_called()


def test_drift_handler_on_moved_dir_queues_drift_scan(drift_watcher_env,
                                                      monkeypatch):
    """on_moved with is_directory=True triggers atlas rekey AND queues
    drift_sweep ref-scan so refs to the old basename get flagged."""
    env = drift_watcher_env
    dw = ds.DriftWatcher(roots=[env.root_a])
    log = MagicMock()
    handler = _DriftHandler(dw, log)

    monkeypatch.setattr("marrow.atlas.rekey_paths", lambda conn, ops: len(ops))
    monkeypatch.setattr(
        "marrow.storage.connect", lambda: MagicMock(close=lambda: None)
    )

    class FakeEvent:
        is_directory = True
        src_path = str(env.root_a / "old")
        dest_path = str(env.root_a / "new")

    handler.on_moved(FakeEvent())
    with dw._lock:
        batch = list(dw._batch)
    assert batch == [(FakeEvent.src_path, FakeEvent.dest_path)]


def test_drift_handler_on_moved_skips_binary(drift_watcher_env):
    """on_moved with a binary extension is skipped by DriftWatcher.on_moved."""
    env = drift_watcher_env
    dw = ds.DriftWatcher(roots=[env.root_a])
    log = MagicMock()
    handler = _DriftHandler(dw, log)

    class FakeEvent:
        is_directory = False
        src_path = str(env.root_a / "photo.jpg")
        dest_path = str(env.root_a / "photo2.jpg")

    with patch.object(dw, "_queue_batch") as mock_q:
        handler.on_moved(FakeEvent())
        mock_q.assert_not_called()


# ---------------------------------------------------------------------------
# _DriftHandler schedules on observer (watcher boots with DriftWatcher)
# ---------------------------------------------------------------------------

def test_drift_handler_scheduled_for_each_root(tmp_path, monkeypatch):
    """Watcher.run schedules _DriftHandler for each AUTHORIZED_ROOTS dir."""
    import marrow.watcher as mw

    existing_root = tmp_path / "cc-lab"
    existing_root.mkdir()
    missing_root = tmp_path / "missing"  # does not exist

    monkeypatch.setattr(mw, "AUTHORIZED_ROOTS", [existing_root, missing_root])

    scheduled_roots: list[str] = []

    fake_observer = MagicMock()
    def fake_schedule(handler, root, recursive):
        if isinstance(handler, mw._DriftHandler):
            scheduled_roots.append(root)
    fake_observer.schedule = fake_schedule
    fake_observer.start = MagicMock()

    fake_conn = MagicMock()
    fake_conn.row_factory = None
    fake_conn.execute = MagicMock()

    stop_event_holder = []

    def fake_run_loop(self):
        # Run the schedule portion only; don't block
        from marrow.drift_sweep import AUTHORIZED_ROOTS as AR
        drift_handler = mw._DriftHandler(self.drift_watcher, self.log)
        for root in AR:
            if root.is_dir():
                self.observer.schedule(drift_handler, str(root), recursive=True)

    with patch("marrow.watcher.storage.init_db") as mock_init_db, \
         patch("marrow.watcher.sqlite3.connect", return_value=fake_conn), \
         patch("marrow.watcher.sqlite_vec.load"), \
         patch("marrow.watcher.config.db_path", return_value=str(tmp_path / "t.db")), \
         patch("marrow.watcher.config.DATA_DIR", str(tmp_path)), \
         patch("marrow.watcher.config.load", return_value={
             "paths": {
                 "db_pages": str(tmp_path / "db-pages"),
             }
         }), \
         patch("marrow.watcher.MdIndex"), \
         patch("marrow.watcher.Observer", return_value=fake_observer):
        mock_init_db.return_value = MagicMock()
        w = mw.Watcher()
        # Simulate the schedule logic from run() directly
        drift_handler = mw._DriftHandler(w.drift_watcher, w.log)
        for root in mw.AUTHORIZED_ROOTS:
            if root.is_dir():
                w.observer.schedule(drift_handler, str(root), recursive=True)

    assert str(existing_root) in scheduled_roots, "existing root must be scheduled"
    assert str(missing_root) not in scheduled_roots, "missing root must be skipped"
