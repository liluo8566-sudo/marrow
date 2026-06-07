"""Tests for the rewritten marker-based sessionstart_catchup.

Each test covers one state in the _classify 7-state decision table, plus
alert idempotency and live-ppid started_at mismatch.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
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


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago_ts(seconds: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _insert_lifecycle(db: str, sid: str, action: str, summary: str = "",
                      occurred_at: str | None = None) -> None:
    conn = storage.connect(db)
    with conn:
        if occurred_at:
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary, occurred_at)"
                " VALUES ('events', ?, ?, ?, ?)",
                (sid, action, summary, occurred_at),
            )
        else:
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('events', ?, ?, ?)",
                (sid, action, summary),
            )
    conn.close()


def _insert_extract(db: str, sid: str, summary: str) -> None:
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', ?)",
            (sid, summary),
        )
    conn.close()


def _insert_user_events(db: str, sid: str, count: int) -> None:
    conn = storage.connect(db)
    with conn:
        for i in range(count):
            ts = (datetime.now(timezone.utc)
                  .strftime("%Y-%m-%dT%H:%M:%SZ"))
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content)"
                " VALUES (?, ?, 'user', ?)",
                (sid, ts, f"msg {i}"),
            )
    conn.close()


def _connect(db: str):
    return storage.connect(db)


# ── helpers for _classify ─────────────────────────────────────────────────────

def _classify(db: str, sid: str, live_ppids: set[int]):
    from marrow import sessionstart_catchup
    conn = storage.connect(db)
    try:
        return sessionstart_catchup._classify(conn, sid, live_ppids)
    finally:
        conn.close()


# ── 7 state tests ─────────────────────────────────────────────────────────────

def test_classify_state1_active_ppid_skips(db_env):
    """State 1: ppid in live_ppids -> skip (session still active)."""
    db, _ = db_env
    sid = "s1-active"
    _insert_lifecycle(db, sid, "session_lifecycle:start", "ppid=12345,source=cc,started_at=1000")
    result = _classify(db, sid, live_ppids={12345})
    assert result == "skip"


def test_classify_state2_end_ok_grew_spawns(db_env):
    """State 2: lifecycle:end + ok,user_count=5 + events.user_count=10 -> spawn."""
    db, _ = db_env
    sid = "s2-grew"
    _insert_lifecycle(db, sid, "session_lifecycle:end")
    _insert_extract(db, sid, "ok,user_count=5")
    _insert_user_events(db, sid, 10)
    result = _classify(db, sid, live_ppids=set())
    assert result == "spawn"


def test_classify_state3_end_ok_covered_skips(db_env):
    """State 3: lifecycle:end + ok,user_count=10 + events.user_count=10 -> skip."""
    db, _ = db_env
    sid = "s3-covered"
    _insert_lifecycle(db, sid, "session_lifecycle:end")
    _insert_extract(db, sid, "ok,user_count=10")
    _insert_user_events(db, sid, 10)
    result = _classify(db, sid, live_ppids=set())
    assert result == "skip"


def test_classify_state4_end_no_ok_within_grace_skips(db_env, monkeypatch):
    """State 4: lifecycle:end + no ok + elapsed < 5min -> skip (async still running)."""
    db, _ = db_env
    sid = "s4-grace"
    # lifecycle:end just now -> within grace period
    _insert_lifecycle(db, sid, "session_lifecycle:end")
    result = _classify(db, sid, live_ppids=set())
    assert result == "skip"


def test_classify_state5_end_no_ok_past_grace_spawns(db_env):
    """State 5: lifecycle:end + no ok + elapsed >= 5min -> spawn (async died)."""
    db, _ = db_env
    sid = "s5-async-died"
    # Insert end marker with timestamp 10min ago
    old_ts = _ago_ts(600)
    _insert_lifecycle(db, sid, "session_lifecycle:end", occurred_at=old_ts)
    result = _classify(db, sid, live_ppids=set())
    assert result == "spawn"


def test_classify_state6_no_end_dead_ppid_spawns(db_env):
    """State 6: no lifecycle:end + ppid dead -> spawn (endhook didn't fire)."""
    db, _ = db_env
    sid = "s6-no-end"
    _insert_lifecycle(db, sid, "session_lifecycle:start", "ppid=99999,source=cc,started_at=1000")
    # ppid 99999 not in live_ppids -> dead
    result = _classify(db, sid, live_ppids=set())
    assert result == "spawn"


def test_classify_state7_no_markers_in_events_spawns(db_env):
    """State 7: no marker rows + sid in events -> spawn (cc died before hooks)."""
    db, _ = db_env
    sid = "s7-no-markers"
    _insert_user_events(db, sid, 5)
    result = _classify(db, sid, live_ppids=set())
    assert result == "spawn"


# ── alert + classify-exemption tests ─────────────────────────────────────────

def test_no_silent_death_alert_ever(db_env):
    """Contract: catchup MUST NOT emit speculative 'silent_death' alerts.
    Even with start>30min + dead ppid + no end + no extract + no archive,
    catchup either spawns sessionend_async or stays silent. Operator-visible
    alerts come only from operational failure (catchup_spawn_failed)."""
    db, _ = db_env
    sid = "would-be-silent-sid"
    old_ts = _ago_ts(31 * 60)
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=88888,source=cc,started_at=1000", occurred_at=old_ts)
    _insert_user_events(db, sid, 5)

    with patch("marrow.sessionstart_catchup.popen_detach_lazy"):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()

    conn = storage.connect(db)
    try:
        ad = conn.execute(
            "SELECT 1 FROM audit_log WHERE action='alert' AND target_id=?"
            " AND summary LIKE 'silent_death%' LIMIT 1",
            (sid,),
        ).fetchone()
        al = conn.execute(
            "SELECT 1 FROM alerts WHERE type='silent_death' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert ad is None, "audit_log silent_death markers must not be written"
    assert al is None, "alerts table silent_death rows must not be written"


def test_classify_user_archived_skips(db_env):
    """Regression: session_block / manual_skip means Lumi explicitly closed
    the sid. _classify must return skip — these are NOT spawn candidates."""
    db, _ = db_env
    old_ts = _ago_ts(31 * 60)
    for sid, action, summary in (
        ("clsblk-archive", "session_block", "archive"),
        ("clsmsk-skip", "manual_skip", "skip"),
        ("clsmsk-bridge", "manual_skip", "bridge_owns"),
    ):
        _insert_lifecycle(db, sid, "session_lifecycle:start",
                          "ppid=33333,source=cc,started_at=1000", occurred_at=old_ts)
        _insert_lifecycle(db, sid, action, summary)
        _insert_user_events(db, sid, 5)
        assert _classify(db, sid, set()) == "skip", \
            f"user-archived sid {sid} must classify as skip"


def test_classify_session_block_cleared_runs(db_env):
    """Latest-row semantics: session_block:archive followed by session_block:cleared
    means mm+ unblocked the sid. Catchup must NOT skip — fall through normally
    (here: dead-ppid + no end -> spawn)."""
    db, _ = db_env
    sid = "block-cleared-sid"
    old_ts = _ago_ts(31 * 60)
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=99001,source=cc,started_at=1000", occurred_at=old_ts)
    _insert_lifecycle(db, sid, "session_block", "archive")
    _insert_lifecycle(db, sid, "session_block", "cleared")
    _insert_user_events(db, sid, 5)
    assert _classify(db, sid, set()) == "spawn"


def test_classify_manual_skip_cleared_runs(db_env):
    """Same latest-row semantics for manual_skip:skip_cleared."""
    db, _ = db_env
    sid = "msk-cleared-sid"
    old_ts = _ago_ts(31 * 60)
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=99002,source=cc,started_at=1000", occurred_at=old_ts)
    _insert_lifecycle(db, sid, "manual_skip", "skip")
    _insert_lifecycle(db, sid, "manual_skip", "skip_cleared")
    _insert_user_events(db, sid, 5)
    assert _classify(db, sid, set()) == "spawn"


def test_classify_worktree_end_marker_skips(db_env):
    """Worktree session_end writes lifecycle:end summary='worktree=1'. This
    is a completed close path with no extract by design. Catchup must skip."""
    db, _ = db_env
    sid = "wt-end-sid"
    old_ts = _ago_ts(60 * 60)
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=99003,source=cc,started_at=1000", occurred_at=old_ts)
    _insert_lifecycle(db, sid, "session_lifecycle:end", "worktree=1",
                      occurred_at=_ago_ts(30 * 60))
    _insert_user_events(db, sid, 5)
    assert _classify(db, sid, set()) == "skip"


def test_classify_mm_minus_blocked_end_marker_skips(db_env):
    """mm- close path writes lifecycle:end summary='mm_minus_blocked'. Same
    contract as worktree=1 — completed close, no extract expected."""
    db, _ = db_env
    sid = "mm-end-sid"
    old_ts = _ago_ts(60 * 60)
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=99004,source=cc,started_at=1000", occurred_at=old_ts)
    _insert_lifecycle(db, sid, "session_lifecycle:end", "mm_minus_blocked",
                      occurred_at=_ago_ts(30 * 60))
    _insert_user_events(db, sid, 5)
    assert _classify(db, sid, set()) == "skip"


def test_classify_inflight_extract_start_skips(db_env):
    """sessionend_extract:start row newer than end_row means sessionend_async
    is currently running (LLM tail can exceed 5min grace). Catchup must NOT
    double-spawn — let the in-flight async finish."""
    db, _ = db_env
    sid = "inflight-sid"
    old_ts = _ago_ts(60 * 60)
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=99005,source=cc,started_at=1000", occurred_at=old_ts)
    _insert_lifecycle(db, sid, "session_lifecycle:end", "",
                      occurred_at=_ago_ts(20 * 60))
    _insert_extract(db, sid, "start")
    _insert_user_events(db, sid, 5)
    assert _classify(db, sid, set()) == "skip"


def test_catchup_spawn_failed_alert(db_env):
    """Operational alert: popen_detach_lazy raising during spawn produces a
    single type-level catchup_spawn_failed alert listing the failing sids."""
    db, _ = db_env
    sid = "spawn-fail-sid"
    old_ts = _ago_ts(31 * 60)
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=22222,source=cc,started_at=1000", occurred_at=old_ts)
    _insert_user_events(db, sid, 5)

    with patch("marrow.sessionstart_catchup.popen_detach_lazy",
               side_effect=OSError("fork rejected")):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()

    conn = storage.connect(db)
    try:
        rows = conn.execute(
            "SELECT fingerprint, message FROM alerts"
            " WHERE type='catchup' AND resolved=0"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, f"expected exactly 1 catchup alert, got {len(rows)}"
    assert rows[0]["fingerprint"] == "catchup_spawn_failed"
    assert sid[:8] in rows[0]["message"]


def test_classify_legacy_ok_without_lifecycle_end_skips(db_env):
    """Regression: a sid processed before the lifecycle plan deployment has
    sessionend_extract:ok but NO session_lifecycle:end. _classify used to
    skip the ok check entirely on the 'no end' path and fall through to
    spawn -> historical sids piled into pending and crowded out new ones
    past MAX_FIRE. Must read as skip (already done)."""
    db, _ = db_env
    sid = "legacy-ok-sid"
    _insert_extract(db, sid, "ok")  # bare legacy ok
    _insert_user_events(db, sid, count=7)
    assert _classify(db, sid, set()) == "skip"


def test_classify_new_ok_without_lifecycle_end_skips_when_covered(db_env):
    """cc reaping the hook between archive_events and the end-marker write
    leaves an `ok,user_count=N` row but no lifecycle:end. Still skip when
    events have not grown beyond N."""
    db, _ = db_env
    sid = "ok-no-end-covered"
    _insert_extract(db, sid, "ok,user_count=10")
    _insert_user_events(db, sid, count=10)
    assert _classify(db, sid, set()) == "skip"


def test_classify_new_ok_without_lifecycle_end_spawns_when_grew(db_env):
    """Same path but events grew past N -> spawn for incremental rerun."""
    db, _ = db_env
    sid = "ok-no-end-grew"
    _insert_extract(db, sid, "ok,user_count=10")
    _insert_user_events(db, sid, count=15)
    assert _classify(db, sid, set()) == "spawn"


def test_live_cc_ppids_trusts_os_kill_over_started_at(db_env, monkeypatch):
    """os.kill is the primary liveness signal; started_at mismatch alone
    does NOT exclude a ppid from live. This guards against the locale-bug
    regression where audit_log markers stored fallback started_at values
    that never match the real process start time -> live cc misjudged as
    dead -> handover clobbered."""
    db, _ = db_env
    sid = "mismatch-sid"
    # started_at=1000 (epoch 1970) but the live process started recently -> mismatch
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=55555,source=cc,started_at=1000")

    def fake_kill(pid, sig):
        pass  # process alive

    monkeypatch.setattr("marrow.sessionstart_catchup.os.kill", fake_kill)

    from marrow import sessionstart_catchup
    conn = storage.connect(db)
    try:
        live = sessionstart_catchup._live_cc_ppids(conn)
    finally:
        conn.close()
    assert 55555 in live, "os.kill success must mark ppid live even when started_at differs"


def test_live_cc_ppids_excludes_dead_ppid(db_env, monkeypatch):
    """os.kill failure means the process is gone -> excluded from live."""
    db, _ = db_env
    sid = "dead-sid"
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=66666,source=cc,started_at=1779700000")

    def fake_kill(pid, sig):
        raise ProcessLookupError("no such process")

    monkeypatch.setattr("marrow.sessionstart_catchup.os.kill", fake_kill)

    from marrow import sessionstart_catchup
    conn = storage.connect(db)
    try:
        live = sessionstart_catchup._live_cc_ppids(conn)
    finally:
        conn.close()
    assert 66666 not in live
