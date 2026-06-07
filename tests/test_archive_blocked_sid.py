"""Defensive gate: archive_events must drop rows for session_block=archive sids.

Covers:
- blocked sid: no rows inserted even when block written after events exist
- cleared sid: rows allowed after session_block=cleared
- unblocked sid: normal insert path unaffected
- mixed batch: blocked and unblocked sids in same rows list
- _sid_is_blocked: last-write-wins (cleared after archived -> not blocked)
"""
from __future__ import annotations

import pytest

from marrow import config, storage
from marrow.repo import _sid_is_blocked, archive_events


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    conn = storage.init_db(path)
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: path)
    return path


def _block(conn, sid: str, status: str = "archive") -> None:
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'session_block', ?)",
            (sid, status),
        )


def _make_rows(sid: str, n: int = 2) -> list[dict]:
    return [
        {
            "session_id": sid,
            "timestamp": f"2026-06-07T10:{i:02d}:00Z",
            "role": "user",
            "content": f"msg {i}",
        }
        for i in range(n)
    ]


def _event_count(conn, sid: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM events WHERE session_id=?", (sid,)
    ).fetchone()[0]


# ── _sid_is_blocked unit tests ────────────────────────────────────────────────

def test_sid_is_blocked_absent(db):
    conn = storage.connect(db)
    assert _sid_is_blocked(conn, "no-such-sid") is False
    conn.close()


def test_sid_is_blocked_archive(db):
    conn = storage.connect(db)
    _block(conn, "sid-a", "archive")
    assert _sid_is_blocked(conn, "sid-a") is True
    conn.close()


def test_sid_is_blocked_cleared(db):
    conn = storage.connect(db)
    _block(conn, "sid-b", "archive")
    _block(conn, "sid-b", "cleared")  # last row wins
    assert _sid_is_blocked(conn, "sid-b") is False
    conn.close()


def test_sid_is_blocked_last_write_wins_archive(db):
    conn = storage.connect(db)
    _block(conn, "sid-c", "cleared")
    _block(conn, "sid-c", "archive")  # re-blocked
    assert _sid_is_blocked(conn, "sid-c") is True
    conn.close()


# ── archive_events gate tests ─────────────────────────────────────────────────

def test_archive_events_blocked_sid_inserts_nothing(db):
    """Core regression: rows for a blocked sid must not land in events."""
    conn = storage.connect(db)
    sid = "blocked-sid-001"
    _block(conn, sid, "archive")
    result = archive_events(conn, _make_rows(sid))
    assert result == 0
    assert _event_count(conn, sid) == 0
    conn.close()


def test_archive_events_block_written_after_prior_insert(db):
    """Simulate historical-residue scenario: block arrives after events already
    in DB. archive_events called again (e.g. from _pre_archive_jsonl) must not
    add more rows. The pre-existing rows are left alone (not in scope here)."""
    conn = storage.connect(db)
    sid = "late-block-sid"
    # First archive run — no block yet
    rows = _make_rows(sid, n=2)
    n1 = archive_events(conn, rows)
    assert n1 == 2

    # Block written later
    _block(conn, sid, "archive")

    # Second archive call (e.g. re-run or _pre_archive_jsonl on same rows)
    n2 = archive_events(conn, rows)
    assert n2 == 0  # idempotent + blocked: no new inserts
    assert _event_count(conn, sid) == 2  # prior rows untouched (not removed)
    conn.close()


def test_archive_events_unblocked_sid_inserts_normally(db):
    conn = storage.connect(db)
    sid = "normal-sid"
    result = archive_events(conn, _make_rows(sid, n=3))
    assert result == 3
    assert _event_count(conn, sid) == 3
    conn.close()


def test_archive_events_cleared_sid_inserts_normally(db):
    conn = storage.connect(db)
    sid = "cleared-sid"
    _block(conn, sid, "archive")
    _block(conn, sid, "cleared")
    result = archive_events(conn, _make_rows(sid, n=2))
    assert result == 2
    assert _event_count(conn, sid) == 2
    conn.close()


def test_archive_events_mixed_batch(db):
    """Blocked and unblocked sids in one rows list: only unblocked land."""
    conn = storage.connect(db)
    sid_ok = "mixed-ok"
    sid_blocked = "mixed-blocked"
    _block(conn, sid_blocked, "archive")

    rows = _make_rows(sid_ok, n=2) + _make_rows(sid_blocked, n=3)
    result = archive_events(conn, rows)

    assert result == 2
    assert _event_count(conn, sid_ok) == 2
    assert _event_count(conn, sid_blocked) == 0
    conn.close()


def test_archive_events_blocked_no_audit_row(db):
    """Fully-blocked archive call (n=0) must not emit a phantom insert audit row."""
    conn = storage.connect(db)
    sid = "no-audit-sid"
    _block(conn, sid, "archive")
    archive_events(conn, _make_rows(sid))
    audit = conn.execute(
        "SELECT * FROM audit_log WHERE action='insert' AND target_id=?", (sid,)
    ).fetchall()
    assert len(audit) == 0
    conn.close()
