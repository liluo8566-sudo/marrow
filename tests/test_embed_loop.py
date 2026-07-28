"""EmbedLoop — pending probe, spawn gating, backlog + failure alerts.

The spawn boundary (popen_detach) is patched in every test: no child process
is ever created and the ONNX model is never loaded.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from marrow import (
    config, popen_detach as popen_detach_mod, recall, repo, storage, watcher,
)
from marrow.watcher import EmbedLoop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeChild:
    """subprocess.Popen stand-in. `code=None` = still running."""

    def __init__(self, code: int | None = 0) -> None:
        self.returncode = code

    def poll(self):
        return self.returncode


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: str(path))
    real_load = config.load

    def fake_load():
        cfg = dict(real_load())
        cfg["paths"] = dict(cfg.get("paths", {}), db=str(path))
        return cfg

    monkeypatch.setattr(config, "load", fake_load)
    conn = storage.init_db(str(path))
    yield conn
    conn.close()


@pytest.fixture
def spawns(monkeypatch):
    """Capture every popen_detach call; return the recorded arg lists."""
    calls: list[list[str]] = []

    def fake(args, log_path):
        calls.append(list(args))
        return _FakeChild(0)

    monkeypatch.setattr(popen_detach_mod, "popen_detach", fake)
    return calls


def _loop(conn, **cfg) -> EmbedLoop:
    base = {"enabled": True, "tick_s": 300, "batch": 50, "max_batches": 20,
            "backlog_alert_count": 100, "backlog_alert_hours": 6,
            "fail_alert_streak": 3}
    base.update(cfg)
    loop = EmbedLoop(lambda: conn, cfg=base)
    loop._conn = conn
    return loop


def _add_event(conn, content="hi", created_at=None):
    if created_at is None:
        conn.execute(
            "INSERT INTO events (session_id, timestamp, role, content) "
            "VALUES ('s', '2026-07-28T00:00:00Z', 'user', ?)", (content,))
    else:
        conn.execute(
            "INSERT INTO events (session_id, timestamp, role, content, created_at) "
            "VALUES ('s', '2026-07-28T00:00:00Z', 'user', ?, ?)",
            (content, created_at))
    conn.commit()


def _alerts(conn, atype="embed"):
    return conn.execute(
        "SELECT fingerprint, message, hit_count FROM alerts WHERE type=?",
        (atype,)).fetchall()


# ---------------------------------------------------------------------------
# recall.pending_counts / pending_oldest_event_ts — real schema, real SQL
# ---------------------------------------------------------------------------

def test_pending_counts_empty_db(db):
    counts = recall.pending_counts(db)
    # every lane query must be valid SQL against the real schema
    assert set(counts) == set(recall._LANES)
    assert sum(counts.values()) == 0


def test_pending_counts_sees_unembedded_event(db):
    _add_event(db, "one")
    _add_event(db, "two")
    counts = recall.pending_counts(db)
    assert counts["events"] == 2
    assert sum(counts.values()) == 2


def test_pending_counts_ignores_embedded_rows(db):
    _add_event(db, "one")
    row_id = db.execute("SELECT id FROM events").fetchone()[0]
    db.execute("INSERT INTO events_vec_meta (rowid, embedder_id, dim) "
               "VALUES (?, 'bge-m3', 1024)", (row_id,))
    db.commit()
    assert recall.pending_counts(db)["events"] == 0


def test_pending_counts_respects_cap(db):
    for i in range(5):
        _add_event(db, f"e{i}")
    assert recall.pending_counts(db, cap=2)["events"] == 2


def test_pending_oldest_event_ts(db):
    _add_event(db, "old", created_at="2026-07-01T00:00:00Z")
    _add_event(db, "new", created_at="2026-07-27T00:00:00Z")
    assert recall.pending_oldest_event_ts(db) == "2026-07-01T00:00:00Z"


def test_pending_oldest_event_ts_none_when_clean(db):
    assert recall.pending_oldest_event_ts(db) is None


def test_pending_counts_survives_missing_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert recall.pending_counts(conn) == {}
    assert recall.pending_oldest_event_ts(conn) is None
    conn.close()


# ---------------------------------------------------------------------------
# tick — spawn gating
# ---------------------------------------------------------------------------

def test_tick_spawns_when_pending(db, spawns):
    _add_event(db, "needs embedding")
    loop = _loop(db, batch=7, max_batches=3)
    loop.tick()
    assert len(spawns) == 1
    args = spawns[0]
    assert args[1:3] == ["-m", "marrow.cli"]
    assert args[3] == "embed"
    assert "--batch" in args and args[args.index("--batch") + 1] == "7"
    assert args[args.index("--max-batches") + 1] == "3"


def test_tick_no_spawn_when_nothing_pending(db, spawns):
    loop = _loop(db)
    loop.tick()
    assert spawns == []


def test_tick_skips_while_child_running(db, spawns):
    _add_event(db, "a")
    loop = _loop(db)
    loop.tick()
    assert len(spawns) == 1
    loop._child = _FakeChild(None)  # still running
    loop.tick()
    assert len(spawns) == 1  # overlap guard held


def test_tick_spawns_again_after_child_exits(db, spawns):
    _add_event(db, "a")
    loop = _loop(db)
    loop.tick()
    loop._child = _FakeChild(0)  # finished
    loop.tick()
    assert len(spawns) == 2


# ---------------------------------------------------------------------------
# consecutive-failure alert
# ---------------------------------------------------------------------------

def test_child_failures_alert_on_streak(db, spawns):
    _add_event(db, "a")
    loop = _loop(db, fail_alert_streak=3)
    for _ in range(2):
        loop._child = _FakeChild(1)
        loop.tick()
    assert _alerts(db, "embed") == []
    loop._child = _FakeChild(1)
    loop.tick()
    rows = _alerts(db, "embed")
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == "embed_child_failed"
    assert "3 consecutive" in rows[0]["message"]


def test_child_success_resets_failure_streak(db, spawns):
    _add_event(db, "a")
    loop = _loop(db, fail_alert_streak=3)
    loop._child = _FakeChild(1)
    loop.tick()
    loop._child = _FakeChild(1)
    loop.tick()
    loop._child = _FakeChild(0)
    loop.tick()
    assert loop._fails == 0
    loop._child = _FakeChild(1)
    loop.tick()
    assert _alerts(db, "embed") == []


def test_fail_alert_streak_config_respected(db, spawns):
    _add_event(db, "a")
    loop = _loop(db, fail_alert_streak=1)
    loop._child = _FakeChild(2)
    loop.tick()
    rows = _alerts(db, "embed")
    assert len(rows) == 1 and rows[0]["fingerprint"] == "embed_child_failed"


# ---------------------------------------------------------------------------
# backlog watermark
# ---------------------------------------------------------------------------

def test_backlog_count_silent_on_first_tick_over(db, spawns):
    """A bulk rebuild is over the line on the tick that starts the drain."""
    for i in range(5):
        _add_event(db, f"e{i}")
    loop = _loop(db, backlog_alert_count=3)
    loop.tick()
    assert _alerts(db, "embed") == []
    assert loop._count_over_prev is True


def test_backlog_count_alerts_once_on_second_consecutive_tick(db, spawns):
    for i in range(5):
        _add_event(db, f"e{i}")
    loop = _loop(db, backlog_alert_count=3)
    loop.tick()
    loop._child = None
    loop.tick()
    rows = _alerts(db, "embed")
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == "embed_backlog"
    assert rows[0]["hit_count"] == 1
    loop._child = None
    loop.tick()
    rows = _alerts(db, "embed")
    assert len(rows) == 1
    assert rows[0]["hit_count"] == 1  # not re-fired on later ticks


def test_backlog_count_drained_before_second_tick_stays_silent(db, spawns):
    """Healthy path: the spawn drains the queue between tick 1 and tick 2."""
    for i in range(5):
        _add_event(db, f"e{i}")
    loop = _loop(db, backlog_alert_count=3)
    loop.tick()
    db.execute("DELETE FROM events")
    db.commit()
    loop._child = None
    loop.tick()
    assert _alerts(db, "embed") == []
    assert loop._count_over_prev is False
    assert loop._backlog_alerted is False


def test_backlog_count_streak_restarts_after_drain(db, spawns):
    """After a drain the count rule needs two fresh consecutive ticks again."""
    for i in range(5):
        _add_event(db, f"e{i}")
    loop = _loop(db, backlog_alert_count=3)
    loop.tick()
    db.execute("DELETE FROM events")
    db.commit()
    loop._child = None
    loop.tick()
    for i in range(5):
        _add_event(db, f"r{i}")
    loop._child = None
    loop.tick()
    assert _alerts(db, "embed") == []  # streak restarted
    loop._child = None
    loop.tick()
    assert len(_alerts(db, "embed")) == 1


def test_backlog_alert_rearms_after_backlog_clears(db, spawns):
    for i in range(5):
        _add_event(db, f"e{i}")
    loop = _loop(db, backlog_alert_count=3)
    loop.tick()
    loop._child = None
    loop.tick()
    assert loop._backlog_alerted is True
    db.execute("DELETE FROM events")
    db.commit()
    loop._child = None
    loop.tick()
    assert loop._backlog_alerted is False


def test_backlog_age_alert_is_immediate(db, spawns):
    """The age rule needs no streak — one stale row already proves the stall."""
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - 10 * 3600))
    _add_event(db, "stale", created_at=old)
    loop = _loop(db, backlog_alert_count=1000, backlog_alert_hours=6)
    loop.tick()
    rows = _alerts(db, "embed")
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == "embed_backlog"
    assert "old" in rows[0]["message"]


def test_no_backlog_alert_under_thresholds(db, spawns):
    recent = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - 60))
    _add_event(db, "fresh", created_at=recent)
    loop = _loop(db, backlog_alert_count=100, backlog_alert_hours=6)
    loop.tick()
    assert _alerts(db, "embed") == []


def test_backlog_alert_fires_even_when_spawn_broken(db, monkeypatch):
    for i in range(5):
        _add_event(db, f"e{i}")

    def boom(args, log_path):
        raise OSError("spawn is broken")

    monkeypatch.setattr(popen_detach_mod, "popen_detach", boom)
    loop = _loop(db, backlog_alert_count=3)
    for _ in range(2):  # count rule needs two consecutive ticks over the line
        with pytest.raises(OSError):
            loop.tick()
    rows = _alerts(db, "embed")
    assert len(rows) == 1 and rows[0]["fingerprint"] == "embed_backlog"


def test_safe_tick_swallows_errors(db, monkeypatch):
    def boom(args, log_path):
        raise OSError("spawn is broken")

    monkeypatch.setattr(popen_detach_mod, "popen_detach", boom)
    _add_event(db, "a")
    loop = _loop(db)
    loop._safe_tick()  # must not raise


# ---------------------------------------------------------------------------
# config plumbing + lifecycle
# ---------------------------------------------------------------------------

def test_config_values_read_from_section(db, monkeypatch):
    cfg = {"embed_loop": {"enabled": False, "tick_s": 11, "batch": 3,
                          "max_batches": 4, "backlog_alert_count": 5,
                          "backlog_alert_hours": 2, "fail_alert_streak": 9}}
    monkeypatch.setattr(config, "load", lambda: cfg)
    loop = EmbedLoop(lambda: db)
    assert loop.enabled is False
    assert (loop._tick_s, loop._batch, loop._max_batches) == (11.0, 3, 4)
    assert (loop._backlog_count, loop._backlog_hours) == (5, 2.0)
    assert loop._fail_streak_max == 9


def test_config_defaults_when_section_absent(db, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {})
    loop = EmbedLoop(lambda: db)
    assert loop.enabled is True
    assert (loop._tick_s, loop._batch, loop._max_batches) == (300.0, 50, 20)
    assert (loop._backlog_count, loop._backlog_hours,
            loop._fail_streak_max) == (100, 6.0, 3)


def test_start_stop_runs_boot_tick(db, spawns, tmp_path):
    """Boot tick fires immediately; the thread owns its own connection."""
    _add_event(db, "a")
    ticks = threading.Event()
    loop = _loop(db, tick_s=0.05)
    # _run opens its own conn — sqlite objects are thread-bound.
    loop._conn_factory = lambda: storage.connect(str(tmp_path / "t.db"))
    real_tick = loop.tick

    def counting_tick():
        real_tick()
        ticks.set()

    loop.tick = counting_tick
    loop.start()
    try:
        assert ticks.wait(2.0)
    finally:
        loop._stop.set()
        loop.stop(timeout=2)
    assert len(spawns) >= 1


# ---------------------------------------------------------------------------
# watcher wiring — thread-start failure raises a critical alert, fail-soft
# ---------------------------------------------------------------------------

def _isolate_watcher(tmp_path, monkeypatch, cfg_extra=None):
    """tmp db + db-pages. The stickers dir is pinned globally by the autouse
    conftest fixture, so watcher.run's boot sweep stays off the real vault."""
    db_file = tmp_path / "t.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: str(db_file))
    db_pages = tmp_path / "db-pages"
    db_pages.mkdir()
    cfg = {"paths": {"db": str(db_file), "db_pages": str(db_pages)},
           "embedding": {"dim": 1024}, "backup": {"keep": 14}}
    cfg.update(cfg_extra or {})
    monkeypatch.setattr(config, "load", lambda: cfg)
    return db_file


def test_watcher_alerts_when_embed_thread_start_fails(tmp_path, monkeypatch):
    _isolate_watcher(tmp_path, monkeypatch)

    class _Boom:
        enabled = True

        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise RuntimeError("no threads left")

    monkeypatch.setattr(watcher, "EmbedLoop", _Boom)
    alerts: list[tuple] = []
    monkeypatch.setattr(repo, "add_alert",
                        lambda *a, **kw: alerts.append((a, kw)))

    w = watcher.Watcher()
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline and not alerts:
            time.sleep(0.1)
    finally:
        w._stop.set()
        t.join(timeout=5)
    fps = [a[0][2] for a in alerts]
    assert "watcher_thread_start_failed" in fps
    sev = [a[0][0] for a in alerts if a[0][2] == "watcher_thread_start_failed"]
    assert sev == ["critical"]
    assert w._embed_loop is None  # fail-soft: watcher kept running without it


def test_watcher_skips_disabled_embed_loop(tmp_path, monkeypatch):
    _isolate_watcher(tmp_path, monkeypatch,
                     {"embed_loop": {"enabled": False}})
    started: list[str] = []

    class _Stub(EmbedLoop):
        def start(self):
            started.append("x")

    monkeypatch.setattr(watcher, "EmbedLoop", _Stub)
    w = watcher.Watcher()
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    try:
        time.sleep(0.8)
    finally:
        w._stop.set()
        t.join(timeout=5)
    assert started == []
    assert w._embed_loop is None
