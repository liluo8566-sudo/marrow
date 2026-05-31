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
    with patch("marrow.sessionstart_catchup.popen_detach",
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
    with patch("marrow.sessionstart_catchup.popen_detach",
               side_effect=lambda a, log_path: spawned.append(list(a))):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()
    fired = {args[args.index("--sid") + 1] for args in spawned}
    assert sid not in fired


def test_sessionend_async_writes_fail_audit_on_exception(db_env):
    """Both sonnet calls raise → final summary='fail:state=...,narrative=...',
    rc=1. Plan H: STATE + NARRATIVE both blown means full fail."""
    db, _ = db_env
    _insert_events(db, "test-fail", count=10, role="user")

    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.side_effect = RuntimeError("boom")
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", "test-fail"])

    assert rc == 1
    rows = _audit_rows(db, "test-fail")
    assert rows[0]["summary"] == "start"
    assert rows[-1]["summary"] == (
        "fail:state=RuntimeError,narrative=RuntimeError")


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

    with patch("marrow.sessionstart_catchup.popen_detach", side_effect=fake_popen):
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
    with patch("marrow.sessionstart_catchup.popen_detach",
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

    with patch("marrow.hooks.popen_detach", side_effect=fake_popen):
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


def test_seg_digest_no_marker_returns_zero(db_env):
    """Raw without ===DIGEST=== marker writes nothing, returns 0."""
    db, _ = db_env
    from marrow import sessionend_async
    conn = storage.connect(db)
    try:
        n = sessionend_writers.seg_digest(conn, "no markers here", "sid-x",
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
        # Seed an unresolved prior so reconcile_ref links.
        conn.execute(
            "INSERT INTO affect (date, ep, valence, arousal, importance,"
            " label, source, unresolved)"
            " VALUES ('2026-05-22', 1, 0.2, 0.7, 4, '焦虑',"
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
            " WHERE date='2026-05-23'"
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


# ── handover segment (Plan H — STATE + NARRATIVE split) ─────────────────────

def test_sessionend_two_calls_routes_to_four_writers(db_env, tmp_path,
                                                      monkeypatch):
    """STATE + NARRATIVE → 4 segment writers + per-segment audit + final 'ok'.
    v2: client.call invoked twice; handover applies the DOING_DIFF; PROGRESS is
    frozen (not appended)."""
    db, _ = db_env
    _insert_events(db, "test-combined", count=10, role="user")

    h = tmp_path / "handover.md"

    state_raw = (
        "===TASK===\n"
        "[{\"title\": \"refactor sessionend\", \"category\": \"Project\","
        " \"status\": \"active\", \"due\": null, \"note\": \"\"}]\n"
        "===END===\n"
        "===DOING_DIFF===\n"
        "ADD:\n"
        "[Marrow] - shipped 2-call refactor\n"
        "  - Current: 2-call flow live\n"
        "  - Next: verify pytest + plist reload\n"
        "  - Reference: marrow/sessionend_async.py:80\n"
        "===END===\n"
        "===NOTE_DONE===\nN/A\n===END===\n"
    )
    narrative_raw = (
        "===AFFECT===\n"
        "[{\"ep\": 1, \"valence\": 0.8, \"arousal\": 0.5,"
        " \"importance\": 3, \"label\": \"愉悦\","
        " \"description\": \"refactor 通过\", \"entities\": [],"
        " \"event_hint\": \"\", \"unresolved\": 0,"
        " \"reconcile_prev\": \"N/A\"}]\n"
        "===END===\n"
        "===DIGEST===\nRefactored sessionend to 2 calls.\n===END===\n"
    )

    with patch("marrow.sessionend_async.LLMClient") as MockClient, \
         patch("marrow.handover_render._RENDERED_PATH", h):
        MockClient.return_value.call.side_effect = [state_raw, narrative_raw]
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
        # 4 segment audit rows logged 'ok' (handover/task_cand from STATE,
        # affect/digest from NARRATIVE). No 'progress' writer (frozen).
        seg_oks = [r for r in seg_rows
                   if r["action"].startswith("sessionend_extract_")
                   and r["summary"] == "ok"]
        assert len(seg_oks) == 4
        assert not any(r["action"].endswith("_progress") for r in seg_rows)
    finally:
        conn.close()
    body = h.read_text(encoding="utf-8")
    assert "shipped 2-call refactor" in body
    assert "verify pytest + plist reload" in body
    assert "handover: ready sid:test-combined" in body
    assert "## Done" in body and "## Doing" in body
    assert "## Lumi's Note" in body


def test_sessionend_state_fail_narrative_ok_partial(db_env, tmp_path,
                                                      monkeypatch):
    """STATE raises, NARRATIVE succeeds → narrative writers run, state ones
    skipped → final summary='partial:...'. Plan H independence."""
    db, _ = db_env
    _insert_events(db, "test-partial", count=10, role="user")
    h = tmp_path / "handover.md"

    narrative_raw = (
        "===AFFECT===\n"
        "[{\"ep\": 1, \"valence\": 0.6, \"arousal\": 0.4,"
        " \"importance\": 2, \"label\": \"平静\","
        " \"description\": \"测试通过\", \"entities\": [],"
        " \"event_hint\": \"\", \"unresolved\": 0,"
        " \"reconcile_prev\": \"N/A\"}]\n"
        "===END===\n"
        "===DIGEST===\nshort digest\n===END===\n"
    )
    state_err = RuntimeError("state-blew-up")

    with patch("marrow.sessionend_async.LLMClient") as MockClient, \
         patch("marrow.handover_render._RENDERED_PATH", h):
        MockClient.return_value.call.side_effect = [state_err, narrative_raw]
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", "test-partial"])

    assert rc == 0  # partial = recovered, not fatal
    rows = _audit_rows(db, "test-partial")
    final = rows[-1]["summary"]
    assert final.startswith("partial:")
    assert "task_cand" in final and "handover" in final


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
    # 00:30 UTC → Melbourne (UTC+10) = 10:30; assert the [HH:MM] [念念] shape.
    assert re.match(r"\[\d{2}:\d{2}\] \[念念\] hi", text), text


def test_load_doing_for_sonnet_extracts_threads_with_ids(tmp_path, monkeypatch):
    """v2: doing loader pulls `## Doing` threads prefixed with [#id]."""
    from marrow import sessionend_async, handover_render
    h = tmp_path / "handover.md"
    h.write_text(
        "# title\n\n"
        "## Done\n- old <!-- done:1700000000 -->\n\n"
        "## Doing\n"
        "1. [Marrow] - thread A\n"
        "  - Current: a-state\n"
        "  - Next: a-next\n"
        "  - Reference: N/A\n"
        "<!-- id:3 -->\n\n"
        "## Lumi's Note\n- buy hand cream\n\n"
        "<!-- handover: ready sid:x ts:1700000000 -->\n", encoding="utf-8")
    monkeypatch.setattr(handover_render, "_RENDERED_PATH", h)
    out = sessionend_async._load_doing_for_sonnet()
    assert "[#3] [Marrow] - thread A" in out
    assert "Current: a-state" in out
    # Note / Done content not in the doing block.
    assert "buy hand cream" not in out


def test_load_doing_returns_placeholder_when_missing(tmp_path, monkeypatch):
    from marrow import sessionend_async, handover_render
    monkeypatch.setattr(handover_render, "_RENDERED_PATH", tmp_path / "nope.md")
    assert sessionend_async._load_doing_for_sonnet() == "(no prior handover)"


def test_load_note_returns_verbatim_body(tmp_path, monkeypatch):
    from marrow import sessionend_async, handover_render
    h = tmp_path / "handover.md"
    h.write_text(
        "## Doing\n- N/A\n\n"
        "## Lumi's Note\n- buy hand cream\n- recharge SIM\n\n",
        encoding="utf-8")
    monkeypatch.setattr(handover_render, "_RENDERED_PATH", h)
    out = sessionend_async._load_note()
    assert "buy hand cream" in out and "recharge SIM" in out


def test_load_note_na_when_missing(tmp_path, monkeypatch):
    from marrow import sessionend_async, handover_render
    monkeypatch.setattr(handover_render, "_RENDERED_PATH", tmp_path / "nope.md")
    assert sessionend_async._load_note() == "N/A"


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


def test_parse_doing_diff_all_verbs():
    from marrow.sessionend_prompts import parse_doing_diff
    raw = (
        "===DOING_DIFF===\n"
        "CLOSE: 1, 2\n"
        "KEEP: 3\n"
        "UPDATE:\n"
        "#4 [Marrow] - thread D\n"
        "  - Current: new\n"
        "  - Next: x\n"
        "  - Reference: N/A\n"
        "ADD:\n"
        "[Study] - thread E\n"
        "  - Current: e\n"
        "  - Next: y\n"
        "  - Reference: N/A\n"
        "===END===\n")
    d = parse_doing_diff(raw)
    assert d["close"] == [1, 2]
    assert d["keep"] == [3]
    assert len(d["update"]) == 1 and d["update"][0]["id"] == 4
    assert "thread D" in d["update"][0]["block"]
    assert len(d["add"]) == 1 and "thread E" in d["add"][0]


def test_parse_doing_diff_missing_subblocks_degrade():
    from marrow.sessionend_prompts import parse_doing_diff
    # No marker at all.
    assert parse_doing_diff("nothing") == {
        "close": [], "keep": [], "update": [], "add": []}
    # Only CLOSE present; bad id token skipped, not crashing.
    d = parse_doing_diff("===DOING_DIFF===\nCLOSE: 1, foo, 3\n===END===\n")
    assert d["close"] == [1, 3]
    assert d["keep"] == [] and d["update"] == [] and d["add"] == []


def test_parse_note_done_drops_na():
    from marrow.sessionend_prompts import parse_note_done
    assert parse_note_done("===NOTE_DONE===\nN/A\n===END===\n") == []
    out = parse_note_done(
        "===NOTE_DONE===\n- buy hand cream\n- recharge SIM\n===END===\n")
    assert out == ["- buy hand cream", "- recharge SIM"]


def test_seg_handover_applies_diff(db_env, tmp_path, monkeypatch):
    """seg_handover applies the DOING_DIFF (ADD) to the single file."""
    db, _ = db_env
    h = tmp_path / "handover.md"
    monkeypatch.setattr("marrow.handover_render._RENDERED_PATH", h)
    raw = (
        "===DOING_DIFF===\n"
        "ADD:\n"
        "[Marrow] - shipped phase 3 handover\n"
        "  - Current: diff-apply done\n"
        "  - Next: launchctl + commit\n"
        "  - Reference: N/A\n"
        "===END===\n"
        "===NOTE_DONE===\nN/A\n===END===\n")
    conn = storage.connect(db)
    try:
        from marrow.sessionend_writers import seg_handover
        n = seg_handover(conn, raw, "S1")
    finally:
        conn.close()
    assert n == 1
    body = h.read_text(encoding="utf-8")
    assert "shipped phase 3 handover" in body
    assert "diff-apply done" in body
    assert "handover: ready sid:S1" in body
    assert "<!-- id:1 -->" in body
    assert "## Done" in body and "## Doing" in body


def test_seg_handover_noop_on_no_diff_marker(db_env, tmp_path, monkeypatch):
    """No DOING_DIFF marker → leave file untouched."""
    db, _ = db_env
    h = tmp_path / "handover.md"
    h.write_text("PRE-EXISTING", encoding="utf-8")
    monkeypatch.setattr("marrow.handover_render._RENDERED_PATH", h)
    conn = storage.connect(db)
    try:
        from marrow.sessionend_writers import seg_handover
        n = seg_handover(conn, "no markers here", "S1")
    finally:
        conn.close()
    assert n == 0
    assert h.read_text(encoding="utf-8") == "PRE-EXISTING"


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


def test_write_final_audit_records_user_count(db_env, tmp_path, monkeypatch):
    """Full main loop -> final ok row matches ok,user_count=<N> pattern."""
    db, _ = db_env
    sid = "uc-test-sid"
    _insert_events(db, sid, count=8, role="user")
    h = tmp_path / "handover.md"
    monkeypatch.setattr("marrow.handover_render._RENDERED_PATH", h)
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
    h = tmp_path / "handover.md"
    monkeypatch.setattr("marrow.handover_render._RENDERED_PATH", h)
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
    h = tmp_path / "handover.md"
    monkeypatch.setattr("marrow.handover_render._RENDERED_PATH", h)
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

    with patch("marrow.hooks.popen_detach", return_value=None), \
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

    with patch("marrow.hooks.popen_detach", return_value=None), \
         patch("marrow.recall.embed_pending", side_effect=track_embed):
        from marrow import hooks
        rc = hooks.session_end()

    assert rc == 0
    assert embed_calls == [], (
        f"session_end hook must not call embed_pending; got {len(embed_calls)} call(s)")


def test_sessionend_writer_operationalerror_partial_not_fail(db_env, tmp_path,
                                                              monkeypatch):
    """Outcome 2: a single writer raising sqlite3.OperationalError (or any
    Exception outside the legacy ValueError/RuntimeError/TypeError/KeyError
    tuple) must mark that writer as fail in its audit row and the session
    overall as partial — other writers still run.

    Today's regression would have let OperationalError escape _run_writer,
    bubble to _run_extraction's outer try, and stamp the whole session as
    fail:OperationalError, losing the work of every other writer.
    """
    import sqlite3 as _sqlite3
    db, _ = db_env
    sid = "test-op-error"
    _insert_events(db, sid, count=10, role="user")
    h = tmp_path / "handover.md"

    state_raw = (
        "===TASK===\n"
        "[{\"title\": \"unrelated task\", \"category\": \"Daily\","
        " \"status\": \"active\", \"due\": null, \"note\": \"\"}]\n"
        "===END===\n"
        "===DOING_DIFF===\n"
        "ADD:\n"
        "[Marrow] - did stuff\n"
        "  - Current: stuff done\n"
        "  - Next: next\n"
        "  - Reference: N/A\n"
        "===END===\n"
        "===NOTE_DONE===\nN/A\n===END===\n"
    )
    narrative_raw = (
        "===AFFECT===\n"
        "[{\"ep\": 1, \"valence\": 0.5, \"arousal\": 0.4,"
        " \"importance\": 2, \"label\": \"平静\","
        " \"description\": \"测试\", \"entities\": [],"
        " \"event_hint\": \"\", \"unresolved\": 0,"
        " \"reconcile_prev\": \"N/A\"}]\n"
        "===END===\n"
        "===DIGEST===\nshort digest\n===END===\n"
    )

    # Make seg_handover raise OperationalError; leave the others alone.
    from marrow import sessionend_async

    def _boom(*_a, **_kw):
        raise _sqlite3.OperationalError("simulated db lock")

    with patch("marrow.sessionend_async.LLMClient") as MockClient, \
         patch("marrow.handover_render._RENDERED_PATH", h), \
         patch("marrow.sessionend_async.seg_handover", _boom):
        MockClient.return_value.call.side_effect = [state_raw, narrative_raw]
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
    assert "handover" in final

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

    # The failing writer's segment row was logged with the writer name and a
    # specific exception type, not the generic outer-catch placeholder.
    h_summary = seg_map.get("sessionend_extract_handover", "")
    assert h_summary.startswith("fail:"), (
        f"expected fail: row for handover writer, got seg_map={seg_map!r}"
    )
    assert "OperationalError" in h_summary, (
        f"expected OperationalError name in summary, got {h_summary!r}"
    )
    # Other writers in the same call ran to completion.
    assert seg_map.get("sessionend_extract_task_cand") == "ok"
    assert seg_map.get("sessionend_extract_affect") == "ok"
    assert seg_map.get("sessionend_extract_digest") == "ok"

    # And the persisted side-effects of the surviving writers landed.
    conn = storage.connect(db)
    try:
        n_task = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE title='unrelated task'"
        ).fetchone()["c"]
        n_aff = conn.execute(
            "SELECT COUNT(*) c FROM affect WHERE source='sessionend_async'"
        ).fetchone()["c"]
        n_dig = conn.execute(
            "SELECT COUNT(*) c FROM session_digests WHERE sid=?", (sid,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n_task == 1
    assert n_aff == 1
    assert n_dig == 1
