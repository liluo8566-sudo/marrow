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


# ── alert tests ───────────────────────────────────────────────────────────────

def test_silent_death_alert_written(db_env):
    """lifecycle:start 31min ago, ppid dead, no end -> alert row written."""
    db, _ = db_env
    sid = "alert-sid"
    old_ts = _ago_ts(31 * 60)
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=88888,source=cc,started_at=1000", occurred_at=old_ts)
    _insert_user_events(db, sid, 5)

    with patch("marrow.sessionstart_catchup.popen_detach_lazy"):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()

    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE action='alert' AND target_id=? AND summary LIKE 'silent_death_no_end:%' LIMIT 1",
            (sid,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "silent death alert should be written"
    assert sid in row["summary"]


def test_alert_idempotent(db_env):
    """Running catchup twice for a silent death -> only one alert row."""
    db, _ = db_env
    sid = "idem-alert-sid"
    old_ts = _ago_ts(31 * 60)
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=77777,source=cc,started_at=1000", occurred_at=old_ts)
    _insert_user_events(db, sid, 5)

    with patch("marrow.sessionstart_catchup.popen_detach_lazy"):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()
        sessionstart_catchup.main()

    conn = storage.connect(db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) c FROM audit_log"
            " WHERE action='alert' AND target_id=? AND summary LIKE 'silent_death_no_end:%'",
            (sid,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1, f"expected 1 alert row, got {count}"


def test_silent_death_writes_to_alerts_table(db_env):
    """Regression: silent_death must also write to the `alerts` table so the
    dashboard 'Alerts' section surfaces it. Prior bug: only audit_log got
    the row, dashboard read alerts table and showed 'none' while sessions
    silently dropped."""
    db, _ = db_env
    sid = "dash-alert-sid"
    old_ts = _ago_ts(31 * 60)
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=66666,source=cc,started_at=1000", occurred_at=old_ts)
    _insert_user_events(db, sid, 5)

    with patch("marrow.sessionstart_catchup.popen_detach_lazy"):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()

    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT severity, type, message, fingerprint FROM alerts"
            " WHERE type='silent_death' AND resolved=0"
            " AND fingerprint='silent_death' LIMIT 1",
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "silent_death must surface in alerts table"
    assert row["severity"] == "warn"
    assert sid[:8] in row["message"], "aggregated message must list the offending sid prefix"


def test_silent_death_extract_row_exempts_alert(db_env):
    """Regression: cc SIGKILL on the hook process group can land between the
    lifecycle:end INSERT and the popen_detach in hooks.session_end, so the
    end marker never gets written even though sessionend_async survived and
    wrote its extract row. The session is NOT silently dead. Catchup must
    not alert when any sessionend_extract row exists for the sid."""
    db, _ = db_env
    sid = "extract-exempts-sid"
    old_ts = _ago_ts(31 * 60)
    _insert_lifecycle(db, sid, "session_lifecycle:start",
                      "ppid=55555,source=cc,started_at=1000", occurred_at=old_ts)
    _insert_user_events(db, sid, 5)
    _insert_extract(db, sid, "skip:short_session,user_count=0")

    with patch("marrow.sessionstart_catchup.popen_detach_lazy"):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()

    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT id FROM alerts WHERE type='silent_death' AND resolved=0 LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is None, "extract row must exempt sid from silent_death alert"


def test_silent_death_fingerprint_collapses_multiple_sids(db_env):
    """Regression: fingerprint used to embed sid[:8] so every dead sid spawned
    its own row, flooding the dashboard. Now fingerprint is type-level so N
    silent sids in one window produce exactly 1 alert row that lists them."""
    db, _ = db_env
    old_ts = _ago_ts(31 * 60)
    sids = ["multi-fp-a-aaa", "multi-fp-b-bbb", "multi-fp-c-ccc"]
    for sid in sids:
        _insert_lifecycle(db, sid, "session_lifecycle:start",
                          "ppid=44444,source=cc,started_at=1000", occurred_at=old_ts)
        _insert_user_events(db, sid, 5)

    with patch("marrow.sessionstart_catchup.popen_detach_lazy"):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()

    conn = storage.connect(db)
    try:
        rows = conn.execute(
            "SELECT message FROM alerts WHERE type='silent_death' AND resolved=0"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, f"expected 1 aggregated alert row, got {len(rows)}"
    msg = rows[0]["message"]
    for sid in sids:
        assert sid[:8] in msg, f"sid {sid[:8]} missing from aggregated message"


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
