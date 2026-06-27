"""Tests for popen_detach, sessionend_async, and sessionstart_catchup.

Run: python -m pytest tests/test_sessionend_async.py -q
Manual live test requires PYTEST_RUN_MANUAL=1 (see test_pingpong_live_isolation_in_hook_context).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from marrow import config, sessionend_writers, storage  # noqa: F401
from marrow.popen_detach import popen_detach


# ── shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db, tmp_path


def _insert_events(db: str, sid: str, count: int, role: str = "user",
                   recent: bool = False) -> None:
    """Insert events. Use recent=True to get timestamps within 24h window."""
    import datetime as _dt
    conn = storage.connect(db)
    with conn:
        for i in range(count):
            if recent:
                ts = (_dt.datetime.now(_dt.timezone.utc)
                      - _dt.timedelta(minutes=30 + i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                ts = f"2026-05-23T10:{i:02d}:00Z"
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content)"
                " VALUES (?, ?, ?, ?)",
                (sid, ts, role, f"msg {i}"),
            )
    conn.close()


def _audit_rows(db: str, sid: str) -> list[dict]:
    conn = storage.connect(db)
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'sessionend_extract' AND target_id = ?",
            (sid,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Unit 1: popen_detach ──────────────────────────────────────────────────────

def test_popen_detach_obeys_contract(tmp_path):
    """Child output goes to log; process launched with start_new_session."""
    log = tmp_path / "test.log"
    cmd = [sys.executable, "-c",
           "import sys; sys.stdout.write('hi'); sys.stdout.flush()"]
    p = popen_detach(cmd, log_path=log)
    # Wait briefly for child to complete.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if p.poll() is not None:
            break
        time.sleep(0.05)
    # Log content must contain child stdout.
    assert log.exists(), "log file not created"
    content = log.read_bytes()
    assert b"hi" in content, f"expected 'hi' in log, got {content!r}"
    # Verify start_new_session: child pgid differs from parent pgid.
    try:
        child_pgid = os.getpgid(p.pid)
    except ProcessLookupError:
        # Already exited; pgid check not possible but presence of output proves it ran.
        return
    parent_pgid = os.getpgrp()
    assert child_pgid != parent_pgid, "child pgid matches parent — start_new_session=True not effective"


# ── Unit 2: sessionend_async ─────────────────────────────────────────────────

def test_sessionend_skip_gate_short_session(db_env, monkeypatch):
    """≤5 user events → skip:short_session; LLMClient.call never invoked."""
    db, _ = db_env
    _insert_events(db, "test-short", count=3, role="user")

    call_count = []

    def boom(*a, **kw):
        call_count.append(1)
        raise AssertionError("LLMClient.call must not be invoked for short sessions")

    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.side_effect = boom
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", "test-short"])

    assert rc == 0
    rows = _audit_rows(db, "test-short")
    assert rows[0]["summary"] == "start"
    assert rows[1]["summary"].startswith("skip:short_session")
    assert not call_count


def test_sessionend_async_writes_ok_audit(db_env):
    """10 user events + mocked LLM response -> audit_log summary='ok,user_count=10'."""
    db, _ = db_env
    _insert_events(db, "test-long", count=10, role="user")

    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.return_value = "echo: 测试 done"
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", "test-long"])

    assert rc == 0
    rows = _audit_rows(db, "test-long")
    assert rows[0]["summary"] == "start"
    assert rows[-1]["summary"] == "ok,user_count=10"


def test_sessionend_async_idempotent(db_env):
    """Second run with existing ok audit row exits 0 without extra DB writes."""
    db, _ = db_env
    _insert_events(db, "test-idem", count=10, role="user")
    # Seed the ok row manually.
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', 'test-idem', 'sessionend_extract', 'ok')",
        )
    conn.close()

    call_count = []

    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.side_effect = lambda *a, **kw: call_count.append(1)
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", "test-idem"])

    assert rc == 0
    assert not call_count, "idempotent path must not call LLM again"
    rows = _audit_rows(db, "test-idem")
    assert len(rows) == 1  # no duplicate row added


def test_sessionend_async_writes_skip_locked_when_flock_busy(db_env, monkeypatch):
    """Lock contention exits 0 but leaves an audit trail for catchup visibility."""
    db, _ = db_env
    sid = "locked-sid"
    from marrow import sessionend_async

    def locked(*_args, **_kwargs):
        raise BlockingIOError

    monkeypatch.setattr(sessionend_async.fcntl, "flock", locked)
    rc = sessionend_async.main(["--sid", sid])

    assert rc == 0
    rows = _audit_rows(db, sid)
    assert len(rows) == 1
    assert rows[0]["summary"] == "skip:locked"


def test_sessionend_async_clears_stale_skip_when_events_grew(db_env):
    """Silent-death regression: cc fires session_end mid-flush, only a partial
    slice of events on disk → sessionend_async writes skip:short_session. Then
    the real session ends with 41 events. Re-running with grown event count
    must drop the stale skip and process normally."""
    db, _ = db_env
    sid = "stale-skip-sid"
    # Phase 1: partial archive — 3 user events, below threshold.
    _insert_events(db, sid, count=3, role="user")
    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.side_effect = AssertionError(
            "must not call LLM for short session")
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", sid])
    assert rc == 0
    rows = _audit_rows(db, sid)
    assert rows[0]["summary"] == "start"
    assert rows[1]["summary"].startswith("skip:short_session")

    # Phase 2: real archive lands — bump events past threshold.
    _insert_events(db, sid, count=20, role="user")
    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.return_value = "echo done"
        rc2 = sessionend_async.main(["--sid", sid])
    assert rc2 == 0
    summaries = [r["summary"] for r in _audit_rows(db, sid)]
    # Stale skip dropped, reset trail logged, real run completed.
    assert not any(s.startswith("skip:short_session") for s in summaries)
    assert "reset:stale_skip" in summaries
    assert summaries[-1].startswith("ok,user_count=")


def test_catchup_retries_sid_when_events_grew_past_skip(db_env, monkeypatch):
    """Catchup-side mirror: a sid with lifecycle:end + ok,user_count=3 but 20
    events now must spawn (state 2: resumed, grew past last ok)."""
    db, _ = db_env
    sid = "grown-sid"
    _insert_lifecycle_marker(db, sid, "session_lifecycle:end")
    _insert_audit(db, sid, "sessionend_extract", "ok,user_count=3")
    _insert_events(db, sid, count=20, role="user", recent=True)

    spawned: list[list[str]] = []
    with patch("marrow.sessionstart_catchup.popen_detach_lazy",
               side_effect=lambda a, log_path: spawned.append(list(a))):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()

    fired = {args[args.index("--sid") + 1] for args in spawned}
    assert sid in fired, "resumed sid should be re-fired when events grew past ok count"


def test_catchup_keeps_skipping_genuinely_done_sids(db_env, monkeypatch):
    """A sid with lifecycle:end + ok,user_count=20 and still 20 events stays skipped."""
    db, _ = db_env
    sid = "stays-skipped-sid"
    _insert_lifecycle_marker(db, sid, "session_lifecycle:end")
    _insert_audit(db, sid, "sessionend_extract", "ok,user_count=20")
    _insert_events(db, sid, count=20, role="user", recent=True)

    spawned: list[list[str]] = []
    with patch("marrow.sessionstart_catchup.popen_detach_lazy",
               side_effect=lambda a, log_path: spawned.append(list(a))):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()
    fired = {args[args.index("--sid") + 1] for args in spawned}
    assert sid not in fired


def test_sessionend_async_writes_fail_audit_on_exception(db_env):
    """Single merged LLM call raises → final summary='fail:llm=RuntimeError',
    rc=1. One call = one fail path."""
    db, _ = db_env
    _insert_events(db, "test-fail", count=10, role="user")

    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.side_effect = RuntimeError("boom")
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", "test-fail"])

    assert rc == 1
    rows = _audit_rows(db, "test-fail")
    assert rows[0]["summary"] == "start"
    assert rows[-1]["summary"] == "fail:llm=RuntimeError: boom"


def _write_extract_row(db: str, sid: str, summary: str) -> None:
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', ?)",
            (sid, summary),
        )
    conn.close()


def test_retry_failed_alert_silent_on_first_fail(db_env):
    """Contract: a one-off fail (prior_fails=0) MUST NOT alert. Only the
    SECOND failure (catchup retry also blew up) crosses the threshold.
    Prevents single-shot noise — matches Lumi's spec."""
    db, _ = db_env
    from marrow import sessionend_async
    sid = "first-fail-only"
    _write_extract_row(db, sid, "start")
    conn = storage.connect(db)
    sessionend_async._write_final_audit(conn, sid, "fail:RuntimeError")
    conn.close()

    conn = storage.connect(db)
    try:
        alert = conn.execute(
            "SELECT id FROM alerts WHERE type='sessionend_async' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert alert is None, "first fail must stay silent"


def test_retry_failed_alert_fires_on_second_fail(db_env):
    """Second fail with prior_fails>=1 → critical alert, type-level
    fingerprint 'sessionend_async_retry_failed'."""
    db, _ = db_env
    from marrow import sessionend_async
    sid = "second-fail-fires"
    # Seed prior failure history.
    _write_extract_row(db, sid, "start")
    _write_extract_row(db, sid, "fail:RuntimeError")
    _write_extract_row(db, sid, "start")
    conn = storage.connect(db)
    sessionend_async._write_final_audit(conn, sid, "fail:OperationalError")
    conn.close()

    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT severity, fingerprint, message FROM alerts"
            " WHERE type='sessionend_async' AND resolved=0"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "second fail must alert"
    assert row["severity"] == "critical"
    assert row["fingerprint"] == "sessionend_async_retry_failed"
    assert sid[:8] in row["message"]
    assert "catchup retry also failed" in row["message"]


def test_retry_failed_alert_collapses_multi_sid_to_one_row(db_env):
    """Type-level fingerprint dedup: N distinct sids that each cross threshold
    must produce exactly 1 alert row (hit_count bumps), not N rows. Prevents
    the dashboard flood Lumi saw with per-sid fingerprints."""
    db, _ = db_env
    from marrow import sessionend_async
    sids = ["multi-a-aaaa", "multi-b-bbbb", "multi-c-cccc"]
    for sid in sids:
        _write_extract_row(db, sid, "start")
        _write_extract_row(db, sid, "fail:Boom")
        _write_extract_row(db, sid, "start")
        conn = storage.connect(db)
        sessionend_async._write_final_audit(conn, sid, "fail:Boom2")
        conn.close()

    conn = storage.connect(db)
    try:
        rows = conn.execute(
            "SELECT message, hit_count FROM alerts"
            " WHERE type='sessionend_async' AND resolved=0"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, f"expected 1 collapsed row, got {len(rows)}"
    assert rows[0]["hit_count"] >= len(sids), \
        f"hit_count must reflect each sid's failure, got {rows[0]['hit_count']}"
    # Latest message wins → must reference one of the sids.
    assert any(s[:8] in rows[0]["message"] for s in sids)


# ── Unit 3: sessionstart_catchup ─────────────────────────────────────────────

def _write_real_jsonl(path: Path, sid: str) -> None:
    """Minimal real-manual cc transcript: opus model + a user turn."""
    import json as _json
    lines = [
        _json.dumps({
            "type": "user", "sessionId": sid,
            "timestamp": "2026-05-24T10:00:00Z",
            "message": {"role": "user", "content": "hello"},
        }),
        _json.dumps({
            "type": "assistant", "sessionId": sid,
            "timestamp": "2026-05-24T10:00:01Z",
            "message": {"role": "assistant", "model": "claude-opus-4-7",
                        "content": [{"type": "text", "text": "hi"}]},
        }),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _insert_lifecycle_marker(db: str, sid: str, action: str, summary: str = "") -> None:
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, ?, ?)",
            (sid, action, summary),
        )
    conn.close()


def _insert_audit(db: str, sid: str, action: str, summary: str) -> None:
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, ?, ?)",
            (sid, action, summary),
        )
    conn.close()


def test_catchup_picks_pending_sids(db_env, monkeypatch):
    """Marker-based catchup: done sid skips; no-end+dead ppid spawns; end+ok skips;
    end+ok+grew spawns; end+no ok+5min spawns; active (live ppid) skips."""
    db, _ = db_env

    # sid_done: lifecycle:end + ok,user_count=10, still 10 events -> skip (state 3)
    sid_done = "aaaaaaaa-done"
    _insert_lifecycle_marker(db, sid_done, "session_lifecycle:end")
    _insert_audit(db, sid_done, "sessionend_extract", "ok,user_count=10")
    _insert_events(db, sid_done, count=10, role="user", recent=True)

    # sid_pending: in events table, no markers -> spawn (state 7)
    sid_pending = "bbbbbbbb-pending"
    _insert_events(db, sid_pending, count=8, role="user", recent=True)

    # sid_grew: lifecycle:end + ok,user_count=5, now 15 events -> spawn (state 2)
    sid_grew = "cccccccc-grew"
    _insert_lifecycle_marker(db, sid_grew, "session_lifecycle:end")
    _insert_audit(db, sid_grew, "sessionend_extract", "ok,user_count=5")
    _insert_events(db, sid_grew, count=15, role="user", recent=True)

    monkeypatch.setattr("marrow.sessionstart_catchup.MAX_FIRE", 5)

    spawned: list[list[str]] = []

    def fake_popen(args, log_path):  # noqa: ARG001
        spawned.append(list(args))

    with patch("marrow.sessionstart_catchup.popen_detach_lazy", side_effect=fake_popen):
        from marrow import sessionstart_catchup
        rc = sessionstart_catchup.main()

    assert rc == 0
    fired = {args[args.index("--sid") + 1] for args in spawned}
    assert sid_pending in fired
    assert sid_grew in fired
    assert sid_done not in fired


def test_catchup_cap_caps_at_max_fire(db_env, monkeypatch):
    """3 pending sids but MAX_FIRE=2 -> only 2 spawn."""
    db, _ = db_env

    sids = ["sid-oldest", "sid-mid", "sid-newest"]
    for sid in sids:
        _insert_events(db, sid, count=8, role="user", recent=True)  # all pending (state 7)

    monkeypatch.setattr("marrow.sessionstart_catchup.MAX_FIRE", 2)

    spawned: list[list[str]] = []
    with patch("marrow.sessionstart_catchup.popen_detach_lazy",
               side_effect=lambda a, log_path: spawned.append(list(a))):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()

    assert len(spawned) == 2


# ── Unit 4: hooks integration ─────────────────────────────────────────────────

def test_sessionend_hook_fires_async_popen(db_env, monkeypatch, tmp_path):
    """session_end() must call popen_detach once with the sessionend_async command."""
    import io
    import json

    db, _ = db_env

    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "sid-hook-test",
        "timestamp": "2026-05-23T10:00:00Z",
        "message": {"role": "user", "content": "hello from hook test"},
    }))

    spawned: list[list[str]] = []

    def fake_popen(args, log_path):  # noqa: ARG001
        spawned.append(list(args))

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "sid-hook-test", "transcript_path": str(jl),
         "cwd": "/repo/path"})))
    monkeypatch.setattr(config, "dashboard_path",
                        lambda: str(tmp_path / "dashboard.md"))
    monkeypatch.setattr(config, "db_pages_path",
                        lambda: str(tmp_path / "db-pages"))
    monkeypatch.setattr(config, "db_pages_state_path",
                        lambda: str(tmp_path / "db_state"))
    monkeypatch.setattr(config, "sub_pages_path",
                        lambda: str(tmp_path / "db-pages"))
    monkeypatch.setattr(config, "sub_pages_state_path",
                        lambda: str(tmp_path / "db_state"))

    with patch("marrow.hooks.popen_detach_lazy", side_effect=fake_popen):
        from marrow import hooks
        rc = hooks.session_end()

    assert rc == 0
    # Filter to sessionend_async calls (session_start also fires catchup).
    async_calls = [c for c in spawned if "sessionend_async" in " ".join(c)]
    assert len(async_calls) == 1, f"expected 1 async spawn, got: {spawned}"
    assert "--sid" in async_calls[0]
    idx = async_calls[0].index("--sid") + 1
    assert async_calls[0][idx] == "sid-hook-test"
    # cwd from hook input threads through to sessionend_async for git_log.
    assert "--cwd" in async_calls[0]
    cidx = async_calls[0].index("--cwd") + 1
    assert async_calls[0][cidx] == "/repo/path"


# ── Segment writers: schema-v2 persistence ────────────────────────────────────

def test_seg_digest_writes_session_digests_row(db_env):
    """DIGEST segment extracts marker body and persists into session_digests."""
    db, _ = db_env
    from marrow import sessionend_async
    conn = storage.connect(db)
    try:
        raw = "===DIGEST===\n今天和念念聊了很久。\n===END===\n"
        n = sessionend_writers.seg_digest(conn, raw, "sid-d1", "2026-05-23")
        assert n == 1
        row = conn.execute(
            "SELECT sid, date, text FROM session_digests"
        ).fetchone()
        assert row["sid"] == "sid-d1"
        assert row["date"] == "2026-05-23"
        assert "念念" in row["text"]
    finally:
        conn.close()


def test_seg_digest_replace_on_resave(db_env):
    """Re-writing the same sid REPLACES the row (idempotent on sid)."""
    db, _ = db_env
    from marrow import sessionend_async
    conn = storage.connect(db)
    try:
        sessionend_writers.seg_digest(
            conn, "===DIGEST===\nfirst\n===END===", "sid-r1", "2026-05-23")
        sessionend_writers.seg_digest(
            conn, "===DIGEST===\nsecond\n===END===", "sid-r1", "2026-05-23")
        rows = conn.execute("SELECT text FROM session_digests").fetchall()
        assert len(rows) == 1
        assert rows[0]["text"] == "second"
    finally:
        conn.close()


def test_seg_digest_no_marker_falls_back_to_whole_raw(db_env):
    """Fence-less raw is stored whole — DIGEST call's entire reply is the digest."""
    db, _ = db_env
    from marrow import sessionend_async
    conn = storage.connect(db)
    try:
        n = sessionend_writers.seg_digest(conn, "no markers here", "sid-x",
                                         "2026-05-23")
        assert n == 1
        rows = conn.execute(
            "SELECT text FROM session_digests WHERE sid='sid-x'").fetchall()
        assert [r["text"] for r in rows] == ["no markers here"]
    finally:
        conn.close()


def test_seg_digest_trailing_end_fence_stripped(db_env):
    """Raw ending with bare ===END=== (no open fence) stores body without it."""
    db, _ = db_env
    conn = storage.connect(db)
    try:
        n = sessionend_writers.seg_digest(
            conn, "digest body\n===END===", "sid-y", "2026-05-23")
        assert n == 1
        rows = conn.execute(
            "SELECT text FROM session_digests WHERE sid='sid-y'").fetchall()
        assert [r["text"] for r in rows] == ["digest body"]
    finally:
        conn.close()


def test_seg_digest_empty_raw_returns_zero(db_env):
    db, _ = db_env
    conn = storage.connect(db)
    try:
        n = sessionend_writers.seg_digest(conn, "  ===END===  ", "sid-z",
                                         "2026-05-23")
        assert n == 0
        rows = conn.execute("SELECT * FROM session_digests").fetchall()
        assert rows == []
    finally:
        conn.close()


def test_seg_affect_persists_reconcile_prev_text(db_env):
    """affect.reconcile_prev_text holds the model's CN phrase (N/A → NULL)."""
    db, _ = db_env
    from marrow import sessionend_async
    conn = storage.connect(db)
    try:
        # Seed an unresolved prior so reconcile_ref links (same day).
        conn.execute(
            "INSERT INTO affect (date, ep, valence, arousal, importance,"
            " label, source, unresolved)"
            " VALUES ('2026-05-23', 1, 0.2, 0.7, 4, '焦虑',"
            " 'sessionend_async', 1)")
        conn.commit()
        raw = (
            "===AFFECT===\n"
            "[{\"ep\": 1, \"valence\": 0.7, \"arousal\": 0.4,"
            " \"importance\": 3, \"label\": \"释然\", \"entities\": [],"
            " \"event_hint\": \"\", \"unresolved\": 0,"
            " \"reconcile_prev\": \"和好了\"}]\n"
            "===END===\n"
        )
        n = sessionend_writers.seg_affect(conn, raw, "sid-a1", "2026-05-23")
        assert n == 1
        row = conn.execute(
            "SELECT reconcile_prev_text, reconcile_ref FROM affect"
            " WHERE date='2026-05-23' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["reconcile_prev_text"] == "和好了"
        assert row["reconcile_ref"] is not None
    finally:
        conn.close()


def test_seg_affect_na_reconcile_prev_text_is_null(db_env):
    db, _ = db_env
    from marrow import sessionend_async
    conn = storage.connect(db)
    try:
        raw = (
            "===AFFECT===\n"
            "[{\"ep\": 1, \"valence\": 0.5, \"arousal\": 0.3,"
            " \"importance\": 2, \"label\": \"平静\", \"entities\": [],"
            " \"event_hint\": \"\", \"unresolved\": 0,"
            " \"reconcile_prev\": \"N/A\"}]\n"
            "===END===\n"
        )
        sessionend_writers.seg_affect(conn, raw, "sid-a2", "2026-05-23")
        row = conn.execute(
            "SELECT reconcile_prev_text FROM affect WHERE date='2026-05-23'"
        ).fetchone()
        assert row["reconcile_prev_text"] is None
    finally:
        conn.close()


def test_seg_affect_persists_description(db_env):
    """affect.description holds the model's short anchor phrase."""
    db, _ = db_env
    from marrow import sessionend_async
    conn = storage.connect(db)
    try:
        raw = (
            "===AFFECT===\n"
            "[{\"ep\": 1, \"valence\": 0.85, \"arousal\": 0.6,"
            " \"importance\": 4, \"label\": \"雀跃\","
            " \"description\": \"拿到 HD\", \"entities\": [],"
            " \"event_hint\": \"\", \"unresolved\": 0,"
            " \"reconcile_prev\": \"N/A\"}]\n"
            "===END===\n"
        )
        n = sessionend_writers.seg_affect(conn, raw, "sid-d1", "2026-05-23")
        assert n == 1
        row = conn.execute(
            "SELECT description FROM affect WHERE date='2026-05-23'"
        ).fetchone()
        assert row["description"] == "拿到 HD"
    finally:
        conn.close()


def test_seg_affect_description_falls_back_to_label(db_env):
    """Missing description → fall back to label, still persist non-null."""
    db, _ = db_env
    from marrow import sessionend_async
    conn = storage.connect(db)
    try:
        raw = (
            "===AFFECT===\n"
            "[{\"ep\": 1, \"valence\": 0.5, \"arousal\": 0.3,"
            " \"importance\": 2, \"label\": \"平静\", \"entities\": [],"
            " \"event_hint\": \"\", \"unresolved\": 0,"
            " \"reconcile_prev\": \"N/A\"}]\n"
            "===END===\n"
        )
        sessionend_writers.seg_affect(conn, raw, "sid-d2", "2026-05-23")
        row = conn.execute(
            "SELECT description FROM affect WHERE date='2026-05-23'"
        ).fetchone()
        assert row["description"] == "平静"
    finally:
        conn.close()


def test_seg_task_cand_writes_tasks_table(db_env):
    """New-task row (no id) writes to `tasks` table."""
    db, _ = db_env
    conn = storage.connect(db)
    try:
        raw = (
            "===TASK===\n"
            "[{\"title\": \"Ship 2.5c\", \"category\": \"Project\","
            " \"status\": \"active\","
            " \"due\": null, \"note\": \"\"}]\n"
            "===END===\n"
        )
        n = sessionend_writers.seg_task_cand(conn, raw)
        assert n == 1
        row = conn.execute(
            "SELECT title, status, category FROM tasks WHERE title='Ship 2.5c'"
        ).fetchone()
        assert row is not None
        assert row["status"] == "active"
        assert row["category"] == "Project"
    finally:
        conn.close()


def test_seg_task_cand_category_whitelist(db_env):
    """Unknown / missing / lowercase categories fall back to Others or canonical."""
    db, _ = db_env
    conn = storage.connect(db)
    try:
        raw = (
            "===TASK===\n"
            "[{\"title\": \"flu vac\", \"category\": \"daily\","
            " \"status\": \"active\", \"due\": null, \"note\": \"\"},"
            " {\"title\": \"random thing\", \"category\": \"banana\","
            " \"status\": \"active\", \"due\": null, \"note\": \"\"},"
            " {\"title\": \"no cat field\","
            " \"status\": \"active\", \"due\": null, \"note\": \"\"}]\n"
            "===END===\n"
        )
        n = sessionend_writers.seg_task_cand(conn, raw)
        assert n == 3
        rows = {r["title"]: r["category"] for r in conn.execute(
            "SELECT title, category FROM tasks"
            " WHERE title IN ('flu vac','random thing','no cat field')"
        ).fetchall()}
        assert rows["flu vac"] == "Daily"
        assert rows["random thing"] == "Others"
        assert rows["no cat field"] == "Others"
    finally:
        conn.close()


def test_seg_task_cand_tick_by_id(db_env):
    """v2 id-tick: a reworded active task still ticks via {id,status:done}."""
    db, _ = db_env
    conn = storage.connect(db)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO tasks (category, title, status)"
                " VALUES ('Study', 'Uni-370 essay draft', 'active')")
            tid = cur.lastrowid
        # Sonnet refers to the id, never the (reworded) title.
        raw = (f"===TASK===\n[{{\"id\": {tid}, \"status\": \"done\"}}]\n"
               "===END===\n")
        n = sessionend_writers.seg_task_cand(conn, raw)
        assert n == 1
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "done"
    finally:
        conn.close()


def test_seg_task_cand_tick_unknown_id_noop(db_env):
    """Tick row for an absent / already-done id is a safe no-op."""
    db, _ = db_env
    conn = storage.connect(db)
    try:
        raw = "===TASK===\n[{\"id\": 9999, \"status\": \"done\"}]\n===END===\n"
        n = sessionend_writers.seg_task_cand(conn, raw)
        assert n == 0
    finally:
        conn.close()


# ── Digest log prune ─────────────────────────────────────────────────────────

def test_prune_digest_logs_keeps_newest_three(tmp_path, monkeypatch):
    """Write 5 fake dated digest log files; prune keeps newest 3."""
    import datetime as _dt
    from marrow import sessionend_writers, config as _config

    monkeypatch.setattr(_config, "DATA_DIR", tmp_path)

    log_dir = tmp_path / "logs" / "digest"
    log_dir.mkdir(parents=True)

    now = _dt.datetime.now(_dt.timezone.utc)

    # Create 5 files with mtimes spread over 5 days (oldest first).
    files = []
    for days_ago in range(4, -1, -1):  # 4,3,2,1,0 days ago
        day = (now - _dt.timedelta(days=days_ago)).strftime("%Y-%m-%d")
        f = log_dir / f"digest-{day}.log"
        f.write_text(f"entry for {day}", encoding="utf-8")
        # Set mtime to match the date.
        mtime = (now - _dt.timedelta(days=days_ago)).timestamp()
        import os
        os.utime(f, (mtime, mtime))
        files.append((days_ago, f))

    # Call prune directly.
    sessionend_writers._prune_digest_logs()

    surviving = sorted(log_dir.glob("digest-*.log"))
    # Should keep the 3 newest (days_ago 0, 1, 2); delete days_ago 3 and 4.
    assert len(surviving) == 3, (
        f"expected 3 files, got {[f.name for f in surviving]}")
    # The two oldest must be gone.
    assert not files[0][1].exists(), "oldest (4d ago) should be pruned"
    assert not files[1][1].exists(), "second-oldest (3d ago) should be pruned"
    # Today and yesterday always kept.
    assert files[4][1].exists(), "today must survive"
    assert files[3][1].exists(), "yesterday must survive"


# ── Manual: live isolation test ───────────────────────────────────────────────

@pytest.mark.manual
@pytest.mark.skipif(
    not os.environ.get("PYTEST_RUN_MANUAL"),
    reason="live LLM test; set PYTEST_RUN_MANUAL=1 to run",
)
def test_pingpong_live_isolation_in_hook_context(db_env):
    """Live ping-pong against real sonnet with _ISOLATION flags active.

    Verifies the CN body containing a PreToolUse-trigger-style string passes
    through without the global prompt-guard.py hook blocking it.

    How to invoke:
        PYTEST_RUN_MANUAL=1 python -m pytest tests/test_sessionend_async.py \\
            -k test_pingpong_live_isolation_in_hook_context -s

    Expected: rc=0, audit_log summary='ok', no LLMError raised.
    Requires: claude CLI on PATH, valid OAuth session.
    """
    db, _ = db_env
    _insert_events(db, "live-ping-sid", count=10, role="user")

    from marrow import sessionend_async
    rc = sessionend_async.main(["--sid", "live-ping-sid"])
    assert rc == 0
    rows = _audit_rows(db, "live-ping-sid")
    assert rows and rows[-1]["summary"].startswith("ok,user_count=")


# ── two-call flow (TASK_AFFECT + DIGEST) ────────────────────────────────────

def test_sessionend_two_calls_routes_to_three_writers(db_env):
    """TASK_AFFECT + DIGEST → 3 segment writers + per-segment audit + final 'ok'.
    client.call invoked twice; writers: task_cand + affect from call1, digest from call2."""
    db, _ = db_env
    _insert_events(db, "test-combined", count=10, role="user")

    task_affect_raw = (
        "===TASK===\n"
        "[{\"title\": \"refactor sessionend\", \"category\": \"Project\","
        " \"status\": \"active\", \"due\": null, \"note\": \"\"}]\n"
        "===END===\n"
        "===AFFECT===\n"
        "[{\"ep\": 1, \"valence\": 0.8, \"arousal\": 0.5,"
        " \"importance\": 3, \"label\": \"愉悦\","
        " \"description\": \"refactor 通过\", \"entities\": [],"
        " \"event_hint\": \"\", \"unresolved\": 0,"
        " \"reconcile_prev\": \"N/A\"}]\n"
        "===END===\n"
    )
    digest_raw = "===DIGEST===\nRefactored sessionend to 2 calls.\n===END===\n"

    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.side_effect = [task_affect_raw, digest_raw]
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", "test-combined"])

    assert rc == 0
    conn = storage.connect(db)
    try:
        n_aff = conn.execute(
            "SELECT COUNT(*) c FROM affect WHERE source='sessionend_async'"
        ).fetchone()["c"]
        n_task = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE title='refactor sessionend'"
        ).fetchone()["c"]
        n_dig = conn.execute(
            "SELECT COUNT(*) c FROM session_digests WHERE sid='test-combined'"
        ).fetchone()["c"]
        assert n_aff == 1 and n_task == 1 and n_dig == 1
        seg_rows = conn.execute(
            "SELECT action, summary FROM audit_log"
            " WHERE target_id='test-combined' ORDER BY id"
        ).fetchall()
        summaries = [r["summary"] for r in seg_rows]
        assert summaries[0] == "start"
        assert summaries[-1].startswith("ok,user_count=")
        # 3 segment audit rows logged 'ok' (task_cand + affect from call1,
        # digest from call2).
        seg_oks = [r for r in seg_rows
                   if r["action"].startswith("sessionend_extract_")
                   and r["summary"] == "ok"]
        assert len(seg_oks) == 3
    finally:
        conn.close()


def test_sessionend_task_affect_fail_digest_ok_partial(db_env):
    """Merged call succeeds but affect writer raises → partial:affect, rc=0.
    LLM call is single; writer-level failures still produce partial summary."""
    db, _ = db_env
    _insert_events(db, "test-partial", count=10, role="user")

    # Return a valid digest block from the single merged call.
    merged_raw = (
        "===TASK===\n[]\n===END===\n"
        "===AFFECT===\n[]\n===END===\n"
        "===DIGEST===\nKIND: casual\nTL: 和老婆聊天\nLIFE: N/A\n"
        "VOICE: N/A\nFACTS: N/A\n===END===\n"
    )

    with patch("marrow.sessionend_async.seg_affect",
               side_effect=RuntimeError("affect-blew")):
        with patch("marrow.sessionend_async.LLMClient") as MockClient:
            MockClient.return_value.call.return_value = merged_raw
            from marrow import sessionend_async
            rc = sessionend_async.main(["--sid", "test-partial"])

    assert rc == 0  # partial = recovered, not fatal
    rows = _audit_rows(db, "test-partial")
    final = rows[-1]["summary"]
    assert final.startswith("partial:")
    assert "affect" in final


def test_session_events_text_prefixes_local_hhmm(db_env):
    """v2: each transcript line is prefixed with a local [HH:MM] stamp."""
    db, _ = db_env
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO events (session_id, timestamp, role, content)"
            " VALUES ('s-hhmm', '2026-05-23T00:30:00Z', 'user', 'hi')")
    from marrow import sessionend_async
    text, _date = sessionend_async._session_events_text(conn, "s-hhmm")
    conn.close()
    import re
    # 00:30 UTC → Melbourne (UTC+10) = 10:30; assert the [HH:MM] [<user>] shape.
    from marrow import config
    uname = config.persona()["user_name"]
    assert re.match(rf"\[\d{{2}}:\d{{2}}\] \[{re.escape(uname)}\] hi", text), text


def test_load_active_tasks_includes_id(db_env):
    """v2: active task lines carry `[#id]` for id-based tick."""
    db, _ = db_env
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO tasks (category, title, status) VALUES"
            " ('Study', 'Uni-370 essay', 'active')")
    from marrow import sessionend_async
    out = sessionend_async._load_active_tasks_for_sonnet(conn)
    conn.close()
    import re
    assert re.search(r"- \[#\d+\] Uni-370 essay \(Study\)", out)


def test_parse_task_rows_tick_and_new():
    from marrow.sessionend_prompts import parse_task_rows
    raw = ('prefix\n===TASK===\n'
           '[{"id": 12, "status": "done"},'
           ' {"title": "Uni-370 AT3", "category": "Assignment",'
           ' "status": "active"}]\n===END===\n')
    rows = parse_task_rows(raw)
    assert rows[0] == {"id": 12, "status": "done"}
    assert rows[1]["title"] == "Uni-370 AT3"


def test_parse_task_rows_empty_on_garbage():
    from marrow.sessionend_prompts import parse_task_rows
    assert parse_task_rows("no marker") == []
    assert parse_task_rows("===TASK===\nnot json\n===END===\n") == []


# ── _already_done new semantics ───────────────────────────────────────────────

def test_already_done_legacy_ok_row_skips(db_env):
    """Legacy summary='ok' (no user_count) -> _already_done returns True."""
    db, _ = db_env
    sid = "legacy-ok-sid"
    _insert_events(db, sid, count=10, role="user")
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'ok')",
            (sid,),
        )
    from marrow import sessionend_async
    result = sessionend_async._already_done(conn, sid)
    conn.close()
    assert result is True


def test_already_done_incremental_rerun_when_events_grew(db_env):
    """ok,user_count=10 + current user_count=15 -> _already_done returns False."""
    db, _ = db_env
    sid = "incremental-sid"
    _insert_events(db, sid, count=15, role="user")
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'ok,user_count=10')",
            (sid,),
        )
    from marrow import sessionend_async
    result = sessionend_async._already_done(conn, sid)
    conn.close()
    assert result is False


def test_already_done_skips_when_events_at_or_below_baseline(db_env):
    """ok,user_count=10 + current user_count=10 -> _already_done returns True."""
    db, _ = db_env
    sid = "at-baseline-sid"
    _insert_events(db, sid, count=10, role="user")
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'ok,user_count=10')",
            (sid,),
        )
    from marrow import sessionend_async
    result = sessionend_async._already_done(conn, sid)
    conn.close()
    assert result is True


def test_write_final_audit_records_user_count(db_env):
    """Full main loop -> final ok row matches ok,user_count=<N> pattern."""
    db, _ = db_env
    sid = "uc-test-sid"
    _insert_events(db, sid, count=8, role="user")
    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.return_value = "echo done"
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", sid])
    assert rc == 0
    rows = _audit_rows(db, sid)
    final = rows[-1]["summary"]
    import re
    assert re.match(r"ok,user_count=\d+", final), f"unexpected final summary: {final!r}"


# ── Tail: dashboard + embed_pending in sessionend_async ──────────────────────

def test_sessionend_async_writes_dashboard_at_tail(db_env, tmp_path,
                                                    monkeypatch):
    """main() calls dashboard.write_dashboard once after both LLM calls."""
    db, _ = db_env
    sid = "dash-tail-sid"
    _insert_events(db, sid, count=10, role="user")
    monkeypatch.setattr(config, "dashboard_path",
                        lambda: str(tmp_path / "dashboard.md"))

    calls: list = []

    def fake_write_dashboard(path, conn, state_dir, db):  # noqa: ARG001
        calls.append((path, db))

    with patch("marrow.sessionend_async.LLMClient") as MockClient, \
         patch("marrow.dashboard.write_dashboard",
               side_effect=fake_write_dashboard):
        MockClient.return_value.call.return_value = "echo done"
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", sid])

    assert rc == 0
    assert len(calls) == 1, f"expected 1 write_dashboard call, got {len(calls)}"
    assert calls[0][1] == db


def test_sessionend_async_continues_when_dashboard_fails(db_env, tmp_path,
                                                          monkeypatch):
    """write_dashboard raises -> _write_final_audit still writes ok row and
    an alert row exists in alerts."""
    db, _ = db_env
    sid = "dash-fail-sid"
    _insert_events(db, sid, count=10, role="user")
    monkeypatch.setattr(config, "dashboard_path",
                        lambda: str(tmp_path / "dashboard.md"))

    with patch("marrow.sessionend_async.LLMClient") as MockClient, \
         patch("marrow.dashboard.write_dashboard",
               side_effect=RuntimeError("dashboard exploded")):
        MockClient.return_value.call.return_value = "echo done"
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", sid])

    assert rc == 0
    rows = _audit_rows(db, sid)
    assert rows[-1]["summary"].startswith("ok,user_count="), (
        f"final audit should be ok, got: {rows[-1]['summary']!r}")

    conn = storage.connect(db)
    try:
        alert_row = conn.execute(
            "SELECT * FROM alerts WHERE type='dashboard' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert alert_row is not None, "expected an alert row for dashboard failure"


def test_session_end_hook_no_longer_calls_dashboard(db_env, monkeypatch,
                                                     tmp_path):
    """session_end hook must NOT call dashboard.write_dashboard directly.
    Dashboard write moved to sessionend_async tail."""
    import io
    import json

    db, _ = db_env
    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "sid-nodash",
        "timestamp": "2026-05-23T10:00:00Z",
        "message": {"role": "user", "content": "hook no-dash test"},
    }))

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "sid-nodash", "transcript_path": str(jl)})))
    monkeypatch.setattr(config, "dashboard_path",
                        lambda: str(tmp_path / "dashboard.md"))
    monkeypatch.setattr(config, "db_pages_path",
                        lambda: str(tmp_path / "db-pages"))
    monkeypatch.setattr(config, "db_pages_state_path",
                        lambda: str(tmp_path / "db_state"))
    monkeypatch.setattr(config, "sub_pages_path",
                        lambda: str(tmp_path / "db-pages"))
    monkeypatch.setattr(config, "sub_pages_state_path",
                        lambda: str(tmp_path / "db_state"))

    dash_calls: list = []

    def track_dash(*a, **kw):
        dash_calls.append(1)

    with patch("marrow.hooks.popen_detach_lazy", return_value=None), \
         patch("marrow.dashboard.write_dashboard", side_effect=track_dash):
        from marrow import hooks
        rc = hooks.session_end()

    assert rc == 0
    assert dash_calls == [], (
        f"session_end hook must not call write_dashboard; got {len(dash_calls)} call(s)")


def test_session_end_hook_no_longer_calls_embed_pending(db_env, monkeypatch,
                                                         tmp_path):
    """session_end hook must NOT call recall.embed_pending directly.
    Embedding moved to sessionend_async tail."""
    import io
    import json

    db, _ = db_env
    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "sid-noembed",
        "timestamp": "2026-05-23T10:00:00Z",
        "message": {"role": "user", "content": "hook no-embed test"},
    }))

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "sid-noembed", "transcript_path": str(jl)})))
    monkeypatch.setattr(config, "dashboard_path",
                        lambda: str(tmp_path / "dashboard.md"))
    monkeypatch.setattr(config, "db_pages_path",
                        lambda: str(tmp_path / "db-pages"))
    monkeypatch.setattr(config, "db_pages_state_path",
                        lambda: str(tmp_path / "db_state"))
    monkeypatch.setattr(config, "sub_pages_path",
                        lambda: str(tmp_path / "db-pages"))
    monkeypatch.setattr(config, "sub_pages_state_path",
                        lambda: str(tmp_path / "db_state"))

    embed_calls: list = []

    def track_embed(*a, **kw):
        embed_calls.append(1)

    with patch("marrow.hooks.popen_detach_lazy", return_value=None), \
         patch("marrow.recall.embed_pending", side_effect=track_embed):
        from marrow import hooks
        rc = hooks.session_end()

    assert rc == 0
    assert embed_calls == [], (
        f"session_end hook must not call embed_pending; got {len(embed_calls)} call(s)")


def test_sessionend_writer_operationalerror_partial_not_fail(db_env):
    """A single writer raising sqlite3.OperationalError must mark that writer
    as fail in its audit row and the session overall as partial — other writers
    still run."""
    import sqlite3 as _sqlite3
    db, _ = db_env
    sid = "test-op-error"
    _insert_events(db, sid, count=10, role="user")

    task_affect_raw = (
        "===TASK===\n"
        "[{\"title\": \"unrelated task\", \"category\": \"Daily\","
        " \"status\": \"active\", \"due\": null, \"note\": \"\"}]\n"
        "===END===\n"
        "===AFFECT===\n"
        "[{\"ep\": 1, \"valence\": 0.5, \"arousal\": 0.4,"
        " \"importance\": 2, \"label\": \"平静\","
        " \"description\": \"测试\", \"entities\": [],"
        " \"event_hint\": \"\", \"unresolved\": 0,"
        " \"reconcile_prev\": \"N/A\"}]\n"
        "===END===\n"
    )
    digest_raw = "===DIGEST===\nshort digest\n===END===\n"

    # Make seg_task_cand raise OperationalError; leave the others alone.
    from marrow import sessionend_async

    def _boom(*_a, **_kw):
        raise _sqlite3.OperationalError("simulated db lock")

    with patch("marrow.sessionend_async.LLMClient") as MockClient, \
         patch("marrow.sessionend_async.seg_task_cand", _boom):
        MockClient.return_value.call.side_effect = [task_affect_raw, digest_raw]
        rc = sessionend_async.main(["--sid", sid])

    # rc == 0 because partial is recoverable, not fatal.
    assert rc == 0
    rows = _audit_rows(db, sid)
    summaries = [r["summary"] for r in rows]

    # Session-level final must be partial, not fail.
    final = summaries[-1]
    assert final.startswith("partial:"), (
        f"expected partial:..., got {final!r}; full audit: {summaries!r}"
    )
    assert "task_cand" in final

    # Per-segment rows live under sessionend_extract_<seg>; fetch separately.
    conn = storage.connect(db)
    try:
        seg_rows = conn.execute(
            "SELECT action, summary FROM audit_log"
            " WHERE target_id=? AND action LIKE 'sessionend_extract_%'",
            (sid,),
        ).fetchall()
    finally:
        conn.close()
    seg_map = {r["action"]: r["summary"] for r in seg_rows}

    # The failing writer's segment row was logged with OperationalError.
    tc_summary = seg_map.get("sessionend_extract_task_cand", "")
    assert tc_summary.startswith("fail:"), (
        f"expected fail: row for task_cand writer, got seg_map={seg_map!r}"
    )
    assert "OperationalError" in tc_summary
    # Other writers ran to completion.
    assert seg_map.get("sessionend_extract_affect") == "ok"
    assert seg_map.get("sessionend_extract_digest") == "ok"

    # Persisted side-effects of the surviving writers landed.
    conn = storage.connect(db)
    try:
        n_aff = conn.execute(
            "SELECT COUNT(*) c FROM affect WHERE source='sessionend_async'"
        ).fetchone()["c"]
        n_dig = conn.execute(
            "SELECT COUNT(*) c FROM session_digests WHERE sid=?", (sid,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n_aff == 1
    assert n_dig == 1


# ── A-1: catchup P5 deadlock fix ─────────────────────────────────────────────

def _insert_extract_row(db: str, sid: str, summary: str,
                        occurred_at: str | None = None) -> int:
    """Insert a sessionend_extract audit row; return its rowid."""
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log"
            " (target_table, target_id, action, summary, occurred_at)"
            " VALUES ('events', ?, 'sessionend_extract', ?,"
            "  COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ','now')))",
            (sid, summary, occurred_at),
        )
        rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return rowid


def _insert_lifecycle_end_old(db: str, sid: str, minutes_ago: int = 30) -> None:
    """Insert a lifecycle:end row with occurred_at in the past."""
    import datetime as _dt
    ts = (_dt.datetime.now(_dt.timezone.utc)
          - _dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log"
            " (target_table, target_id, action, summary, occurred_at)"
            " VALUES ('events', ?, 'session_lifecycle:end', '', ?)",
            (sid, ts),
        )
    conn.close()


def test_p5_partial_terminal_spawns(db_env, monkeypatch):
    """end_row + start + terminal partial:digest → fall through → spawn."""
    import datetime as _dt
    from marrow import sessionstart_catchup
    db, _ = db_env
    sid = "p5-partial-spawn"
    _insert_lifecycle_marker(db, sid, "session_lifecycle:start",
                             summary="ppid=99999,source=cc,started_at=1")
    # End row must be old enough to pass state-4 grace (>5 min).
    _insert_lifecycle_end_old(db, sid, minutes_ago=30)
    _insert_extract_row(db, sid, "start")
    _insert_extract_row(db, sid, "partial:digest")

    monkeypatch.setattr(sessionstart_catchup, "MAX_FIRE", 5)
    spawned = []
    with patch("marrow.sessionstart_catchup.popen_detach_lazy",
               side_effect=lambda a, log_path: spawned.append(a)):
        sessionstart_catchup.main()
    fired = {a[a.index("--sid") + 1] for a in spawned}
    assert sid in fired, "partial:digest sid must re-spawn"


def test_p5_inflight_within_grace_skips(db_env, monkeypatch):
    """start within grace period, no terminal → in-flight → skip."""
    import datetime as _dt
    from marrow import sessionstart_catchup
    db, _ = db_env
    sid = "p5-inflight-skip"
    recent_ts = (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_lifecycle_marker(db, sid, "session_lifecycle:start",
                             summary="ppid=99999,source=cc,started_at=1")
    # Old end_row so state-4 grace is not the reason for skip — P5 must be.
    _insert_lifecycle_end_old(db, sid, minutes_ago=30)
    _insert_extract_row(db, sid, "start", occurred_at=recent_ts)

    monkeypatch.setattr(sessionstart_catchup, "MAX_FIRE", 5)
    spawned = []
    with patch("marrow.sessionstart_catchup.popen_detach_lazy",
               side_effect=lambda a, log_path: spawned.append(a)):
        sessionstart_catchup.main()
    fired = {a[a.index("--sid") + 1] for a in spawned}
    assert sid not in fired, "genuinely in-flight sid must skip"


def test_p5_stale_start_no_terminal_spawns(db_env, monkeypatch):
    """start older than 15 min, no terminal → died mid-run → spawn."""
    import datetime as _dt
    from marrow import sessionstart_catchup
    db, _ = db_env
    sid = "p5-stale-spawn"
    stale_ts = (_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_lifecycle_marker(db, sid, "session_lifecycle:start",
                             summary="ppid=99999,source=cc,started_at=1")
    # End row must be old enough to pass state-4 grace (>5 min).
    _insert_lifecycle_end_old(db, sid, minutes_ago=30)
    _insert_extract_row(db, sid, "start", occurred_at=stale_ts)

    monkeypatch.setattr(sessionstart_catchup, "MAX_FIRE", 5)
    spawned = []
    with patch("marrow.sessionstart_catchup.popen_detach_lazy",
               side_effect=lambda a, log_path: spawned.append(a)):
        sessionstart_catchup.main()
    fired = {a[a.index("--sid") + 1] for a in spawned}
    assert sid in fired, "stale-start (no terminal) must spawn"


def test_p5_ok_terminal_skips(db_env, monkeypatch):
    """start + ok terminal → session completed normally → skip (state 3)."""
    import datetime as _dt
    from marrow import sessionstart_catchup
    db, _ = db_env
    sid = "p5-ok-skip"
    recent_ts = (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_lifecycle_marker(db, sid, "session_lifecycle:start",
                             summary="ppid=99999,source=cc,started_at=1")
    _insert_lifecycle_end_old(db, sid, minutes_ago=30)
    _insert_extract_row(db, sid, "start", occurred_at=recent_ts)
    # ok,user_count=5 terminal; no events inserted → count=0 ≤ 5 → state 3 skip.
    _insert_extract_row(db, sid, "ok,user_count=5")

    monkeypatch.setattr(sessionstart_catchup, "MAX_FIRE", 5)
    spawned = []
    with patch("marrow.sessionstart_catchup.popen_detach_lazy",
               side_effect=lambda a, log_path: spawned.append(a)):
        sessionstart_catchup.main()
    fired = {a[a.index("--sid") + 1] for a in spawned}
    assert sid not in fired, "completed (ok terminal) sid must skip"


# ── A-2: strike-two chain + digest zero-write ─────────────────────────────────

def test_strike_two_chain_second_fail_alerts(db_env):
    """Write a prior fail row, then call _write_final_audit with second fail →
    alert row exists with fingerprint sessionend_async_retry_failed."""
    from marrow import sessionend_async
    db, _ = db_env
    sid = "strike-two-chain"
    _write_extract_row(db, sid, "start")
    _write_extract_row(db, sid, "fail:LLMError")
    _write_extract_row(db, sid, "start")
    conn = storage.connect(db)
    sessionend_async._write_final_audit(conn, sid, "fail:LLMError")
    conn.close()

    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT fingerprint FROM alerts"
            " WHERE type='sessionend_async' AND resolved=0"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "second fail must produce alert"
    assert row["fingerprint"] == "sessionend_async_retry_failed"


def test_strike_two_first_fail_no_alert(db_env):
    """Single first failure must stay silent — no alert row."""
    from marrow import sessionend_async
    db, _ = db_env
    sid = "strike-one-silent"
    _write_extract_row(db, sid, "start")
    conn = storage.connect(db)
    sessionend_async._write_final_audit(conn, sid, "fail:LLMError")
    conn.close()

    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT id FROM alerts WHERE type='sessionend_async'"
        ).fetchone()
    finally:
        conn.close()
    assert row is None, "first fail must be silent"


def test_digest_zero_rows_becomes_partial_no_immediate_alert(db_env):
    """_run_writer with zero_is_fail=True: digest writer returns 0 →
    segment audit is fail:zero_rows → final audit is partial containing
    'digest'; NO digest_zero_write alert row exists."""
    from marrow import sessionend_async
    db, _ = db_env
    sid = "digest-zero-partial"

    conn = storage.connect(db)
    # Simulate: task_cand ok, affect ok, digest returns 0.
    sessionend_async._write_segment_audit(conn, sid, "task_cand", "ok")
    sessionend_async._write_segment_audit(conn, sid, "affect", "ok")
    sessionend_async._run_writer(
        conn, sid, "digest", lambda: 0, zero_is_fail=True)
    sessionend_async._write_final_audit(conn, sid,
        "partial:digest")  # as the pipeline would compute

    conn.close()

    conn = storage.connect(db)
    try:
        # No immediate digest_zero_write alert.
        alert = conn.execute(
            "SELECT id FROM alerts WHERE fingerprint='digest_zero_write'"
        ).fetchone()
        # Segment row says fail:zero_rows.
        seg = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE target_id=? AND action='sessionend_extract_digest'",
            (sid,),
        ).fetchone()
        final = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE target_id=? AND action='sessionend_extract'"
            " ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
    finally:
        conn.close()

    assert alert is None, "no digest_zero_write alert must exist"
    assert seg is not None and seg["summary"] == "fail:zero_rows"
    assert final is not None and "digest" in final["summary"]


def _stamp_start(conn, sid: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'start')",
            (sid,),
        )


def test_retry_success_after_prior_fail_collects_nothing(db_env):
    """Stale fail rows from a prior attempt must not flip a successful retry
    back to partial: _collect_run_failures sees only rows after the latest
    'start' stamp."""
    from marrow import sessionend_async
    db, _ = db_env
    sid = "retry-scope-ok"
    conn = storage.connect(db)
    try:
        _stamp_start(conn, sid)
        sessionend_async._write_segment_audit(conn, sid, "digest",
                                              "fail:zero_rows")
        _stamp_start(conn, sid)
        sessionend_async._write_segment_audit(conn, sid, "task_cand", "ok")
        sessionend_async._write_segment_audit(conn, sid, "affect", "ok")
        sessionend_async._write_segment_audit(conn, sid, "digest", "ok")
        assert sessionend_async._collect_run_failures(conn, sid) == []
    finally:
        conn.close()


def test_current_run_failure_still_collected(db_env):
    from marrow import sessionend_async
    db, _ = db_env
    sid = "retry-scope-fail"
    conn = storage.connect(db)
    try:
        _stamp_start(conn, sid)
        sessionend_async._write_segment_audit(conn, sid, "affect", "ok")
        sessionend_async._write_segment_audit(conn, sid, "digest",
                                              "fail:zero_rows")
        assert sessionend_async._collect_run_failures(conn, sid) == ["digest"]
    finally:
        conn.close()


def test_collect_without_start_row_falls_back_to_all_rows(db_env):
    """COALESCE 0 fallback: if the start stamp failed, every segment row
    counts (old behaviour, fail-safe)."""
    from marrow import sessionend_async
    db, _ = db_env
    sid = "retry-scope-nostart"
    conn = storage.connect(db)
    try:
        sessionend_async._write_segment_audit(conn, sid, "digest",
                                              "fail:LLMError")
        assert sessionend_async._collect_run_failures(conn, sid) == ["digest"]
    finally:
        conn.close()


# ── A-3: add_alert fallback sink ──────────────────────────────────────────────

def test_add_alert_fallback_on_db_failure(tmp_path, monkeypatch):
    """Force DB write to fail → line lands in alerts-fallback.jsonl,
    no exception propagates, return value is -1."""
    import json as _json
    from marrow import repo, config as _cfg
    monkeypatch.setattr(_cfg, "DATA_DIR", tmp_path)
    # Point storage.connect at an unwritable path so the INSERT raises.
    monkeypatch.setattr(
        "marrow.repo.storage.connect",
        lambda db=None: (_ for _ in ()).throw(
            Exception("forced DB failure")),
    )
    result = repo.add_alert("warn", "test_type", "test_fp",
                            source="test", message="boom")
    assert result == -1
    sink = tmp_path / "alerts-fallback.jsonl"
    assert sink.exists()
    line = _json.loads(sink.read_text().strip())
    assert line["fingerprint"] == "test_fp"
    assert line["severity"] == "warn"


def test_fallback_drain_replays_and_truncates(tmp_path, monkeypatch):
    """Write a valid line to alerts-fallback.jsonl; run drain logic →
    alert row lands in DB and the file is truncated."""
    import json as _json
    from datetime import datetime, timezone
    from marrow import sessionstart_catchup, config as _cfg, storage
    db = str(tmp_path / "drain.db")
    conn = storage.init_db(db)
    conn.close()
    monkeypatch.setattr(_cfg, "db_path", lambda: db)
    monkeypatch.setattr(_cfg, "DATA_DIR", tmp_path)

    sink = tmp_path / "alerts-fallback.jsonl"
    rec = _json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "severity": "warn",
        "type": "drain_test",
        "fingerprint": "drain_fp",
        "source": "test",
        "message": "drained",
    })
    sink.write_text(rec + "\n", encoding="utf-8")

    sessionstart_catchup._drain_fallback_sink(db)

    # File should be truncated (empty).
    assert sink.read_text() == ""
    # Alert row must be in DB.
    fresh = storage.connect(db)
    try:
        row = fresh.execute(
            "SELECT fingerprint FROM alerts WHERE fingerprint='drain_fp'"
        ).fetchone()
    finally:
        fresh.close()
    assert row is not None, "drained alert must land in DB"


# ── Fix 1 regression: force_run with 0 events must skip, not fail ────────────

def test_sessionend_zero_events_with_force_flag_skips(db_env, monkeypatch):
    """Fix 1: force_run=True but 0 user events -> skip:short_session,user_count=0.

    Session 9f11d4ed had a reset:mm_plus audit row (force_run=True via
    _has_mm_plus_reset) but zero events, causing infinite fail:no_events retries.
    The count==0 guard must fire before force_run is even evaluated.
    """
    db, tmp_path = db_env
    sid = "zero-events-force"

    # Write a reset:mm_plus row so _has_mm_plus_reset returns True.
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'reset:mm_plus')",
            (sid,),
        )
    conn.close()

    # No events inserted — count == 0.
    call_count = []

    def boom(*a, **kw):
        call_count.append(1)
        raise AssertionError("LLMClient must not be called with zero events")

    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.side_effect = boom
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", sid])

    assert rc == 0, "should exit 0 (skip), not 1 (fail)"
    rows = _audit_rows(db, sid)
    summaries = [r["summary"] for r in rows]
    assert any(s == "skip:short_session,user_count=0" for s in summaries), \
        f"expected skip:short_session,user_count=0 in {summaries}"
    assert not any(s.startswith("fail:") for s in summaries), \
        f"unexpected fail row in {summaries}"
    assert not call_count


def test_parse_digest_block_facts_to_life_lines():
    from marrow.sessionend_writers import _parse_digest_block
    raw = (
        "===DIGEST===\n"
        "KIND: task\n"
        "TL: 修了timeline bug\n"
        "LIFE: N/A\n"
        "VOICE: N/A\n"
        "FACTS:\n"
        "- 14:00【平淡】一起修timeline bug\n"
        "===END==="
    )
    result = _parse_digest_block(raw)
    assert result["kind"] == "task"
    assert result["life_lines"] is not None
    assert "14:00【平淡】一起修timeline bug" in result["life_lines"]
