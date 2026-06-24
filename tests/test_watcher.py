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
    def fake_load():
        return {"paths": {"db": str(db), "dashboard": str(dashboard),
                          "db_pages": str(db_pages)},
                "embedding": {"dim": 1024}, "backup": {"keep": 14}}

    monkeypatch.setattr(config, "load", fake_load)
    w = Watcher()
    w._reconcile_boot()
    # 2 blocks inserted across 2 paths (dashboard + db-pages).
    rows = w.conn.execute("SELECT path, block_id FROM md_index").fetchall()
    bids = sorted(r[1] for r in rows)
    assert bids == ["1", "9"]
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
    assert d == tmp_path / "logs" / "watcher"


# ── outcome 3 — watchdog end-to-end round-trip ────────────────────────────

def _wait_for_md_index(db_path: str, predicate, *, timeout: float = 5.0,
                       poll: float = 0.1) -> bool:
    """Poll the md_index table until `predicate(rows)` is True or timeout.

    `rows` is a list of dicts {block_id, content_hash, tombstone_at} from the
    given db path. Returns the truthy value `predicate` returns, or False.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = storage.connect(db_path)
        try:
            rs = [dict(r) for r in conn.execute(
                "SELECT path, block_id, content_hash, tombstone_at "
                "FROM md_index ORDER BY block_id"
            ).fetchall()]
        finally:
            conn.close()
        result = predicate(rs)
        if result:
            return result
        time.sleep(poll)
    return False


def test_watchdog_roundtrip_delete_restore_add(tmp_path, monkeypatch):
    """End-to-end: md edit → debounce → sync_file_observe → md_index state.

    Three transitions, each driven by a real on-disk write, observed by a
    live watchdog Observer:
    1. delete a bullet → tombstone_at set for that block_id, baseline hash
       for surviving blocks unchanged
    2. restore file (cmd+Z equivalent — identical bytes) → tombstone cleared
       on the restored block, baseline matches original content
    3. add a brand-new block → new row inserted with fresh baseline

    Coverage gap before this test: no pytest exercised the full chain
    (watcher.run → Observer thread → on_modified → debounce → sync_file_observe
    → md_index UPSERT). _DEBOUNCE_S is 0.2s so polling within 5s is sufficient.
    """
    from marrow.watcher import Watcher, _DEBOUNCE_S

    db = tmp_path / "t.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: str(db))
    db_pages = tmp_path / "db-pages"
    db_pages.mkdir()
    dashboard = tmp_path / "dashboard.md"
    dashboard.write_text("")

    def fake_load():
        return {"paths": {"db": str(db), "dashboard": str(dashboard),
                          "db_pages": str(db_pages)},
                "embedding": {"dim": 1024}, "backup": {"keep": 14}}

    monkeypatch.setattr(config, "load", fake_load)

    target = db_pages / "page.md"
    initial = (
        "- one <!-- id:a -->\n"
        "- two <!-- id:b -->\n"
    )
    target.write_text(initial, encoding="utf-8")

    w = Watcher()
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    try:
        # Wait for boot full_scan to land both blocks.
        bootstrap = _wait_for_md_index(
            str(db),
            lambda rs: (
                {r["block_id"] for r in rs if r["path"] == str(target.resolve())}
                == {"a", "b"}
                and all(r["tombstone_at"] is None for r in rs
                        if r["path"] == str(target.resolve()))
            ),
            timeout=4.0,
        )
        assert bootstrap, "boot full_scan did not record both blocks as active"

        # Snapshot baseline hashes for later equality checks.
        conn = storage.connect(str(db))
        try:
            base_a = conn.execute(
                "SELECT content_hash FROM md_index "
                "WHERE path=? AND block_id='a'",
                (str(target.resolve()),),
            ).fetchone()["content_hash"]
            base_b = conn.execute(
                "SELECT content_hash FROM md_index "
                "WHERE path=? AND block_id='b'",
                (str(target.resolve()),),
            ).fetchone()["content_hash"]
        finally:
            conn.close()
        assert base_a and base_b and base_a != base_b

        # ── step 1: delete bullet `a` ──
        target.write_text("- two <!-- id:b -->\n", encoding="utf-8")
        deleted = _wait_for_md_index(
            str(db),
            lambda rs: any(
                r["block_id"] == "a"
                and r["path"] == str(target.resolve())
                and r["tombstone_at"] is not None
                for r in rs
            ),
            timeout=4.0,
        )
        assert deleted, "block `a` was not tombstoned after deletion"
        # Surviving block `b` baseline must not have been overwritten —
        # sync_file_observe is the observe-only path.
        conn = storage.connect(str(db))
        try:
            b_after = conn.execute(
                "SELECT content_hash, tombstone_at FROM md_index "
                "WHERE path=? AND block_id='b'",
                (str(target.resolve()),),
            ).fetchone()
        finally:
            conn.close()
        assert b_after["content_hash"] == base_b
        assert b_after["tombstone_at"] is None

        # ── step 2: restore (cmd+Z) — write identical original bytes ──
        target.write_text(initial, encoding="utf-8")
        restored = _wait_for_md_index(
            str(db),
            lambda rs: all(
                r["tombstone_at"] is None
                for r in rs
                if r["path"] == str(target.resolve())
                and r["block_id"] in {"a", "b"}
            ),
            timeout=4.0,
        )
        assert restored, "tombstone on `a` was not cleared after restore"
        # Baseline for `a` must match the original (record_block on
        # tombstone-clear writes the on-disk hash, which equals the original).
        conn = storage.connect(str(db))
        try:
            a_after = conn.execute(
                "SELECT content_hash FROM md_index "
                "WHERE path=? AND block_id='a'",
                (str(target.resolve()),),
            ).fetchone()["content_hash"]
        finally:
            conn.close()
        assert a_after == base_a

        # ── step 3: add a brand-new block `c` ──
        target.write_text(initial + "- three <!-- id:c -->\n",
                          encoding="utf-8")
        added = _wait_for_md_index(
            str(db),
            lambda rs: any(
                r["block_id"] == "c"
                and r["path"] == str(target.resolve())
                and r["content_hash"]
                and r["tombstone_at"] is None
                for r in rs
            ),
            timeout=4.0,
        )
        assert added, "new block `c` was not inserted with a fresh baseline"
    finally:
        w._stop.set()
        t.join(timeout=5)


def test_handler_detail_pages_excluded_from_reindex(tmp_path):
    """db-pages/study/<unit>.md and db-pages/projects/<page>.md must not trigger
    md_index reindex; their index pages (study.md, projects.md) still do."""
    import os
    conn = storage.init_db(str(tmp_path / "t.db"))
    store = MdIndex(conn)
    fired: list[str] = []
    deb = _Debouncer(0.01, lambda p: fired.append(p))

    # Simulate db-pages dir structure
    db_pages = tmp_path / "db-pages"
    db_pages.mkdir()
    (db_pages / "study").mkdir()
    (db_pages / "projects").mkdir()

    h = _MdHandler(store, watched_files=set(),
                   watched_dirs={str(db_pages)},
                   debouncer=deb, log=watcher._setup_logger())

    detail_study = db_pages / "study" / "Biochem.md"
    detail_proj = db_pages / "projects" / "Marrow.md"
    index_study = db_pages / "study.md"
    index_proj = db_pages / "projects.md"

    for f in (detail_study, detail_proj, index_study, index_proj):
        f.write_text("- x <!-- id:1 -->\n")

    h.on_modified(_FakeEvent(str(detail_study)))
    h.on_modified(_FakeEvent(str(detail_proj)))
    h.on_modified(_FakeEvent(str(index_study)))
    h.on_modified(_FakeEvent(str(index_proj)))
    time.sleep(0.1)

    fired_basenames = [os.path.basename(p) for p in fired]
    assert "Biochem.md" not in fired_basenames, "detail study page should be excluded"
    assert "Marrow.md" not in fired_basenames, "detail project page should be excluded"
    assert "study.md" in fired_basenames, "index study page should pass through"
    assert "projects.md" in fired_basenames, "index project page should pass through"
    conn.close()
