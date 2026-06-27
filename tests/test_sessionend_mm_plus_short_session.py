"""Tests for force_sessionend bypass of short_session threshold gate.

Test A: short session with force_sessionend audit row must NOT skip.
Test B: force_sessionend after ok row makes _already_done return False.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from marrow import config, storage


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db, tmp_path


def _insert_events(db: str, sid: str, count: int, role: str = "user") -> None:
    conn = storage.connect(db)
    with conn:
        for i in range(count):
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content)"
                " VALUES (?, ?, ?, ?)",
                (sid, f"2026-05-23T10:{i:02d}:00Z", role, f"msg {i}"),
            )
    conn.close()


def _insert_extract_audit(db: str, sid: str, summary: str) -> None:
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', ?)",
            (sid, summary),
        )
    conn.close()


def _insert_force_audit(db: str, sid: str, summary: str = "mm_plus_flag") -> None:
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'force_sessionend', ?)",
            (sid, summary),
        )
    conn.close()


# ── Test A: force flag bypasses short_session threshold ──────────────────────

def test_force_flag_bypasses_short_session_threshold(db_env, monkeypatch):
    """Short session (≤threshold events) with force_sessionend must NOT early-exit
    with skip:short_session — must reach LLM extraction."""
    db, tmp_path = db_env
    sid = "S1"

    # Get threshold from config (default 3)
    from marrow import sessionend_async
    cfg = config.load() if hasattr(config, "load") else {}
    threshold = (cfg.get("sessionend", {}).get("skip_turn_threshold", 3)
                 if cfg else 3)

    # Insert exactly threshold events — would normally trigger short_session skip
    _insert_events(db, sid, count=threshold, role="user")

    _insert_force_audit(db, sid)

    llm_called = []

    def fake_llm_call(*a, **kw):
        llm_called.append(1)
        return "echo: done"

    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.side_effect = fake_llm_call
        rc = sessionend_async.main(["--sid", sid])

    # LLM must have been called (pipeline reached extraction)
    assert llm_called, (
        "LLMClient.call was never invoked — short_session gate fired "
        "despite force_sessionend being present"
    )
    # Audit trail must not show skip:short_session as the final row
    conn = storage.connect(db)
    try:
        rows = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE action='sessionend_extract' AND target_id=?"
            " ORDER BY id",
            (sid,),
        ).fetchall()
    finally:
        conn.close()
    summaries = [r["summary"] for r in rows]
    assert not any(s.startswith("skip:short_session") for s in summaries), (
        f"skip:short_session appeared in audit trail: {summaries!r}"
    )


# ── Test B: force flag after ok row overrides _already_done ──────────────────

def test_already_done_false_when_force_flag_after_ok(db_env):
    """_already_done returns False when force_sessionend is newer than ok,
    even if a prior ok,user_count=N row exists."""
    db, _ = db_env
    sid = "S2"
    from marrow import sessionend_async

    n = 10
    _insert_events(db, sid, count=n, role="user")
    # First: matching ok row (would make _already_done return True normally)
    _insert_extract_audit(db, sid, f"ok,user_count={n}")
    _insert_force_audit(db, sid)

    conn = storage.connect(db)
    try:
        result = sessionend_async._already_done(conn, sid)
    finally:
        conn.close()

    assert result is False, (
        "_already_done returned True despite force_sessionend being newer than ok"
    )


# ── Test C: force marker is consumed after a successful extraction ───────────

def test_force_flag_consumed_after_ok_row(db_env):
    """Once _run_extraction writes a fresh ok,user_count=N row, the prior
    force flag must no longer bypass the threshold gate on the next short
    session_end for the same sid."""
    db, _ = db_env
    sid = "S3"
    from marrow import sessionend_async

    _insert_events(db, sid, count=2, role="user")
    _insert_force_audit(db, sid)
    # Simulate the start stamp + successful extraction the first mm+ run writes.
    _insert_extract_audit(db, sid, "start")
    _insert_extract_audit(db, sid, "ok,user_count=2")

    conn = storage.connect(db)
    try:
        result = sessionend_async._has_force_sessionend(conn, sid)
    finally:
        conn.close()

    assert result is False, (
        "_has_force_sessionend still True after ok row landed — marker not consumed, "
        "next short session_end will be force-rerun forever"
    )


# ── Test D: 'start' stamp does not hide a fresh force marker ─────────────────

def test_force_flag_visible_through_start_row(db_env):
    """The start row is inserted before the threshold gate; the helper must
    look past it to honour force_sessionend written just before the run."""
    db, _ = db_env
    sid = "S4"
    from marrow import sessionend_async

    _insert_events(db, sid, count=2, role="user")
    _insert_force_audit(db, sid)
    _insert_extract_audit(db, sid, "start")  # stamped by sessionend_async itself

    conn = storage.connect(db)
    try:
        result = sessionend_async._has_force_sessionend(conn, sid)
    finally:
        conn.close()

    assert result is True, (
        "_has_force_sessionend False despite a fresh force_sessionend row"
    )
