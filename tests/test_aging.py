"""Tests for marrow/aging.py — weekly maintenance. No LLM under test.
Each pass: happy path + edge (pinned bypass / no-op empty / boundary)."""
from __future__ import annotations

import sqlite3

import pytest

from marrow import aging, storage


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    yield conn
    conn.close()


def _ins_vocab(conn, key, *, vtype="meme", use_count=0, last_seen=None,
               pinned=0, status="active"):
    conn.execute(
        "INSERT INTO vocab (type, key, use_count, last_seen, pinned, status)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (vtype, key, use_count, last_seen, pinned, status),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _ins_event(conn, content, *, ts="now", sid="s1"):
    if ts == "now":
        conn.execute(
            "INSERT INTO events (session_id, timestamp, role, content) "
            "VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'user', ?)",
            (sid, content),
        )
    else:
        conn.execute(
            "INSERT INTO events (session_id, timestamp, role, content) "
            "VALUES (?, ?, 'user', ?)",
            (sid, ts, content),
        )


def _ins_task(conn, title, *, status="active"):
    conn.execute(
        "INSERT INTO tasks (category, title, status) VALUES ('study', ?, ?)",
        (title, status),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _ins_alert(conn, atype, message, *, resolved=0, age_days=0):
    if age_days == 0:
        conn.execute(
            "INSERT INTO alerts (severity, type, message, resolved) "
            "VALUES ('info', ?, ?, ?)",
            (atype, message, resolved),
        )
    else:
        conn.execute(
            "INSERT INTO alerts (severity, type, message, resolved,"
            " created_at) "
            "VALUES ('info', ?, ?, ?, datetime('now', ? || ' days'))",
            (atype, message, resolved, f"-{age_days}"),
        )


# ── schema v3 vocab columns ──────────────────────────────────────────────────

def test_v3_vocab_columns_present(db):
    cols = {r["name"] for r in db.execute("PRAGMA table_info(vocab)")}
    assert "pinned" in cols
    assert "status" in cols
    assert db.execute("PRAGMA user_version").fetchone()[0] >= 3


# ── enforce_anchor_pins ───────────────────────────────────────────────────────

def test_enforce_anchor_pins_flips_unpinned_anchors(db):
    _ins_vocab(db, "鸭子", pinned=0)
    _ins_vocab(db, "念念", pinned=0)
    _ins_vocab(db, "老公", pinned=1)  # already pinned
    _ins_vocab(db, "随便", pinned=0)  # not anchor
    db.commit()
    flipped = aging.enforce_anchor_pins(db)
    assert flipped == 2
    pinned_keys = {r["key"] for r in db.execute(
        "SELECT key FROM vocab WHERE pinned = 1")}
    assert {"鸭子", "念念", "老公"} <= pinned_keys
    assert "随便" not in pinned_keys


def test_enforce_anchor_pins_idempotent(db):
    _ins_vocab(db, "鸭子", pinned=0)
    db.commit()
    assert aging.enforce_anchor_pins(db) == 1
    assert aging.enforce_anchor_pins(db) == 0


# ── promote_vocab ─────────────────────────────────────────────────────────────

def test_promote_vocab_three_distinct_hits_promotes(db):
    vid = _ins_vocab(db, "marrow", use_count=0, status="dormant")
    for i in range(3):
        _ins_event(db, f"talking about marrow today round {i}", sid=f"s{i}")
    db.commit()
    n = aging.promote_vocab(db)
    assert n == 1
    row = db.execute(
        "SELECT use_count, status, last_seen FROM vocab WHERE id=?", (vid,)
    ).fetchone()
    assert row["use_count"] == 3
    assert row["status"] == "active"
    assert row["last_seen"] is not None


def test_promote_vocab_below_threshold_skips(db):
    vid = _ins_vocab(db, "marrow", use_count=0, status="dormant")
    _ins_event(db, "marrow once", sid="s1")
    _ins_event(db, "marrow twice", sid="s2")
    db.commit()
    n = aging.promote_vocab(db)
    assert n == 0
    row = db.execute(
        "SELECT use_count, status FROM vocab WHERE id=?", (vid,)
    ).fetchone()
    assert row["use_count"] == 0
    assert row["status"] == "dormant"


def test_promote_vocab_skips_pinned(db):
    vid = _ins_vocab(db, "鸭子", pinned=1, status="dormant", use_count=0)
    for i in range(5):
        _ins_event(db, f"鸭子 says hi {i}", sid=f"s{i}")
    db.commit()
    n = aging.promote_vocab(db)
    assert n == 0
    row = db.execute(
        "SELECT use_count, status FROM vocab WHERE id=?", (vid,)
    ).fetchone()
    assert row["use_count"] == 0
    assert row["status"] == "dormant"


def test_promote_vocab_ignores_old_events(db):
    vid = _ins_vocab(db, "ancient", use_count=0)
    for i in range(5):
        _ins_event(db, f"ancient ref {i}",
                   ts="2026-01-01T00:00:00Z", sid=f"s{i}")
    db.commit()
    assert aging.promote_vocab(db) == 0
    row = db.execute(
        "SELECT use_count FROM vocab WHERE id=?", (vid,)
    ).fetchone()
    assert row["use_count"] == 0


def test_promote_vocab_no_events_noop(db):
    _ins_vocab(db, "lonely", status="dormant")
    db.commit()
    assert aging.promote_vocab(db) == 0


# ── demote_vocab ──────────────────────────────────────────────────────────────

def test_demote_vocab_old_last_seen_demotes(db):
    vid = _ins_vocab(
        db, "stale", pinned=0, status="active",
        last_seen="2020-01-01T00:00:00Z",
    )
    db.commit()
    n = aging.demote_vocab(db)
    assert n == 1
    row = db.execute(
        "SELECT status FROM vocab WHERE id=?", (vid,)
    ).fetchone()
    assert row["status"] == "dormant"


def test_demote_vocab_skips_pinned(db):
    vid = _ins_vocab(
        db, "鸭子", pinned=1, status="active",
        last_seen="2020-01-01T00:00:00Z",
    )
    db.commit()
    assert aging.demote_vocab(db) == 0
    row = db.execute(
        "SELECT status FROM vocab WHERE id=?", (vid,)
    ).fetchone()
    assert row["status"] == "active"


def test_demote_vocab_skips_recent(db):
    _ins_vocab(
        db, "fresh", pinned=0, status="active",
        last_seen="2026-05-20T00:00:00Z",
    )
    db.commit()
    assert aging.demote_vocab(db) == 0


def test_demote_vocab_skips_null_last_seen(db):
    _ins_vocab(db, "unseen", pinned=0, status="active", last_seen=None)
    db.commit()
    assert aging.demote_vocab(db) == 0


def test_demote_vocab_already_dormant_noop(db):
    _ins_vocab(
        db, "old", pinned=0, status="dormant",
        last_seen="2020-01-01T00:00:00Z",
    )
    db.commit()
    assert aging.demote_vocab(db) == 0


# ── archive_tasks ─────────────────────────────────────────────────────────────

def test_archive_tasks_no_mention_archives(db):
    tid = _ins_task(db, "forgotten thing", status="active")
    db.commit()
    n = aging.archive_tasks(db)
    assert n == 1
    row = db.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["status"] == "archived"


def test_archive_tasks_recent_mention_keeps_active(db):
    tid = _ins_task(db, "active project", status="active")
    _ins_event(db, "working on active project today", sid="s1")
    db.commit()
    n = aging.archive_tasks(db)
    assert n == 0
    row = db.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["status"] == "active"


def test_archive_tasks_old_mention_archives(db):
    tid = _ins_task(db, "stale project", status="active")
    _ins_event(db, "stale project ref",
               ts="2026-01-01T00:00:00Z", sid="s1")
    db.commit()
    n = aging.archive_tasks(db)
    assert n == 1
    row = db.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["status"] == "archived"


def test_archive_tasks_skips_already_archived(db):
    _ins_task(db, "done thing", status="archived")
    db.commit()
    assert aging.archive_tasks(db) == 0


def test_archive_tasks_empty_noop(db):
    assert aging.archive_tasks(db) == 0


# ── confirm_milestone_alerts ──────────────────────────────────────────────────

def test_confirm_milestone_alerts_old_unresolved_confirmed(db):
    _ins_alert(db, "milestone_added", "added X", resolved=0, age_days=10)
    db.commit()
    n = aging.confirm_milestone_alerts(db)
    assert n == 1
    row = db.execute(
        "SELECT resolved, resolved_at FROM alerts WHERE message='added X'"
    ).fetchone()
    assert row["resolved"] == 1
    assert row["resolved_at"] is not None


def test_confirm_milestone_alerts_recent_skipped(db):
    _ins_alert(db, "milestone_added", "added Y", resolved=0, age_days=3)
    db.commit()
    assert aging.confirm_milestone_alerts(db) == 0


def test_confirm_milestone_alerts_skips_other_types(db):
    _ins_alert(db, "routine", "daily failed", resolved=0, age_days=10)
    db.commit()
    assert aging.confirm_milestone_alerts(db) == 0


def test_confirm_milestone_alerts_skips_already_resolved(db):
    _ins_alert(db, "milestone_added", "added Z", resolved=1, age_days=10)
    db.commit()
    assert aging.confirm_milestone_alerts(db) == 0


# ── main entrypoint ───────────────────────────────────────────────────────────

def _route_init_db(monkeypatch, p):
    """Route aging.storage.init_db() to a fixed path without recursion."""
    real = storage.init_db
    monkeypatch.setattr(
        aging.storage, "init_db",
        lambda path=None: real(p),
    )


def test_main_runs_clean_on_empty_db(db, monkeypatch, capsys):
    p = db.execute("PRAGMA database_list").fetchone()["file"]
    db.close()
    _route_init_db(monkeypatch, p)
    aging.main()
    cap = capsys.readouterr()
    assert "promoted=0" in cap.err
    assert "archived=0" in cap.err


def test_main_writes_audit_log(db, monkeypatch):
    p = db.execute("PRAGMA database_list").fetchone()["file"]
    db.close()
    _route_init_db(monkeypatch, p)
    aging.main()
    fresh = sqlite3.connect(p)
    fresh.row_factory = sqlite3.Row
    try:
        row = fresh.execute(
            "SELECT target_table, action, summary FROM audit_log "
            "WHERE target_table='aging' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["action"] == "weekly"
        assert "promoted=" in row["summary"]
    finally:
        fresh.close()


def test_main_force_pins_anchors_each_pass(db, monkeypatch):
    p = db.execute("PRAGMA database_list").fetchone()["file"]
    _ins_vocab(db, "鸭子", pinned=0)
    db.commit()
    db.close()
    _route_init_db(monkeypatch, p)
    aging.main()
    fresh = sqlite3.connect(p)
    fresh.row_factory = sqlite3.Row
    try:
        row = fresh.execute(
            "SELECT pinned FROM vocab WHERE key='鸭子'"
        ).fetchone()
        assert row["pinned"] == 1
    finally:
        fresh.close()
