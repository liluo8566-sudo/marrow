"""watcher — debouncer, handler routing, boot reconcile."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from marrow import config, storage, watcher
from marrow.md_index import MdIndex
from marrow.watcher import _Debouncer, _MdHandler, Watcher


class _FakeEvent:
    def __init__(self, src, *, is_dir=False, dst=None):
        self.src_path = src
        self.is_directory = is_dir
        self.dest_path = dst


def test_debouncer_coalesces_repeats():
    calls: list[tuple] = []

    def fire(p):
        calls.append((p, time.time()))

    d = _Debouncer(0.05, fire)
    for _ in range(5):
        d.trigger("k", "p1")
        time.sleep(0.01)
    time.sleep(0.15)
    assert len(calls) == 1


def test_debouncer_distinct_keys_fire_independently():
    calls: list[str] = []
    d = _Debouncer(0.05, lambda p: calls.append(p))
    d.trigger("a", "pa")
    d.trigger("b", "pb")
    time.sleep(0.15)
    assert sorted(calls) == ["pa", "pb"]


def test_debouncer_flush_cancels_pending():
    calls: list[str] = []
    d = _Debouncer(0.2, lambda p: calls.append(p))
    d.trigger("k", "p")
    d.flush()
    time.sleep(0.3)
    assert calls == []


def test_handler_ignores_non_md(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    store = MdIndex(conn)
    fired: list[str] = []
    deb = _Debouncer(0.0, lambda p: fired.append(p))
    h = _MdHandler(store, watched_files=set(), watched_dirs={str(tmp_path)},
                   debouncer=deb, log=watcher._setup_logger())
    h.on_modified(_FakeEvent(str(tmp_path / "x.txt")))
    time.sleep(0.05)
    assert fired == []
    conn.close()


def test_handler_dir_mode_fires_for_md(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    store = MdIndex(conn)
    md = tmp_path / "a.md"
    md.write_text("- x <!-- id:1 -->\n")
    fired: list[str] = []
    deb = _Debouncer(0.01, lambda p: fired.append(p))
    h = _MdHandler(store, watched_files=set(), watched_dirs={str(tmp_path)},
                   debouncer=deb, log=watcher._setup_logger())
    h.on_modified(_FakeEvent(str(md)))
    time.sleep(0.1)
    assert fired == [str(md.resolve())]
    conn.close()


def test_handler_file_mode_filters_to_watched_set(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    store = MdIndex(conn)
    watched = tmp_path / "watched.md"
    other = tmp_path / "other.md"
    watched.write_text("- x <!-- id:1 -->\n")
    other.write_text("- y <!-- id:2 -->\n")
    fired: list[str] = []
    deb = _Debouncer(0.01, lambda p: fired.append(p))
    h = _MdHandler(store, watched_files={str(watched.resolve())},
                   watched_dirs=set(), debouncer=deb,
                   log=watcher._setup_logger())
    h.on_modified(_FakeEvent(str(other)))
    h.on_modified(_FakeEvent(str(watched)))
    time.sleep(0.1)
    assert fired == [str(watched.resolve())]
    conn.close()


def test_handler_dispatches_on_create_move_delete(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    store = MdIndex(conn)
    fired: list[str] = []
    deb = _Debouncer(0.01, lambda p: fired.append(p))
    h = _MdHandler(store, watched_files=set(), watched_dirs={str(tmp_path)},
                   debouncer=deb, log=watcher._setup_logger())
    a = str((tmp_path / "a.md").resolve())
    b = str((tmp_path / "b.md").resolve())
    h.on_created(_FakeEvent(a))
    h.on_moved(_FakeEvent(a, dst=b))
    h.on_deleted(_FakeEvent(b))
    time.sleep(0.1)
    # a fires (create + move-from), b fires (move-to + delete) — debouncer
    # coalesces per-key so a -> 1, b -> 1.
    assert sorted(fired) == sorted([a, b])
    conn.close()


def test_watcher_boot_reconcile_syncs(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: str(db))
    db_pages = tmp_path / "db-pages"
    db_pages.mkdir()
    (db_pages / "x.md").write_text("- a <!-- id:1 -->\n")
    dashboard = tmp_path / "dashboard.md"
    dashboard.write_text("- d <!-- id:9 -->\n")
    (tmp_path / "handover.md").write_text("- h <!-- id:7 -->\n")

    def fake_load():
        return {"paths": {"db": str(db), "dashboard": str(dashboard),
                          "db_pages": str(db_pages)},
                "embedding": {"dim": 1024}, "backup": {"keep": 14}}

    monkeypatch.setattr(config, "load", fake_load)
    w = Watcher()
    w._reconcile_boot()
    # 3 blocks inserted across 3 paths.
    rows = w.conn.execute("SELECT path, block_id FROM md_index").fetchall()
    bids = sorted(r[1] for r in rows)
    assert bids == ["1", "7", "9"]
    w.conn.close()


def test_watcher_live_modify_triggers_sync(tmp_path, monkeypatch):
    """End-to-end smoke: spin a real Observer briefly, write a file, see DB."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: str(db))
    db_pages = tmp_path / "db-pages"
    db_pages.mkdir()
    dashboard = tmp_path / "dashboard.md"
    dashboard.write_text("")
    (tmp_path / "handover.md").write_text("")

    def fake_load():
        return {"paths": {"db": str(db), "dashboard": str(dashboard),
                          "db_pages": str(db_pages)},
                "embedding": {"dim": 1024}, "backup": {"keep": 14}}

    monkeypatch.setattr(config, "load", fake_load)

    w = Watcher()
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    try:
        time.sleep(0.6)  # let boot + observer attach
        target = db_pages / "live.md"
        target.write_text("- live <!-- id:55 -->\n")
        # Poll the db for up to 4s for the row to appear.
        deadline = time.time() + 4.0
        seen = False
        while time.time() < deadline:
            conn = storage.connect(str(db))
            row = conn.execute(
                "SELECT 1 FROM md_index WHERE block_id='55'"
            ).fetchone()
            conn.close()
            if row is not None:
                seen = True
                break
            time.sleep(0.2)
        assert seen, "live modify did not propagate to md_index"
    finally:
        w._stop.set()
        t.join(timeout=5)


def test_logs_dir_created(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    d = watcher._logs_dir()
    assert d.exists() and d.is_dir()
    assert d == tmp_path / "logs"
