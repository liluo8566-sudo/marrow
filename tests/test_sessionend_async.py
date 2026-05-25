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
    assert [r["summary"] for r in rows] == ["start", "skip:short_session"]
    assert not call_count


def test_sessionend_async_writes_ok_audit(db_env):
    """10 user events + mocked LLM response → audit_log summary='ok'."""
    db, _ = db_env
    _insert_events(db, "test-long", count=10, role="user")

    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.return_value = "echo: 测试 done"
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", "test-long"])

    assert rc == 0
    rows = _audit_rows(db, "test-long")
    assert [r["summary"] for r in rows] == ["start", "ok"]


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
    assert [r["summary"] for r in rows] == ["start", "skip:short_session"]

    # Phase 2: real archive lands — bump events past threshold.
    _insert_events(db, sid, count=20, role="user")
    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.return_value = "echo done"
        rc2 = sessionend_async.main(["--sid", sid])
    assert rc2 == 0
    summaries = [r["summary"] for r in _audit_rows(db, sid)]
    # Stale skip dropped, reset trail logged, real run completed.
    assert "skip:short_session" not in summaries
    assert "reset:stale_skip" in summaries
    assert summaries[-1] == "ok"


def test_catchup_retries_sid_when_events_grew_past_skip(db_env, monkeypatch,
                                                          tmp_path):
    """Catchup-side mirror: a sid with skip:short_session + grown events must
    be re-fired by the catchup loop. Old code permanently blocked it."""
    db, _ = db_env
    projects = tmp_path / "projects"
    proj_dir = projects / "-Users-test"
    proj_dir.mkdir(parents=True)
    sid = "grown-sid"
    _write_real_jsonl(proj_dir / f"{sid}.jsonl", sid)
    now = time.time()
    os.utime(proj_dir / f"{sid}.jsonl", (now - 3600, now - 3600))

    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'skip:short_session')",
            (sid,))
        # Simulate the real session growing far past skip threshold.
        for i in range(20):
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content)"
                " VALUES (?, ?, 'user', ?)",
                (sid, f"2026-05-24T10:{i:02d}:00Z", f"msg {i}"))
    conn.close()

    monkeypatch.setattr(
        "marrow.sessionstart_catchup._CC_PROJECTS", projects)

    spawned: list[list[str]] = []
    with patch("marrow.sessionstart_catchup.popen_detach",
               side_effect=lambda a, log_path: spawned.append(list(a))):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()

    fired = {args[args.index("--sid") + 1] for args in spawned}
    assert sid in fired, (
        "grown sid blocked by stale skip — silent-death regression returned")


def test_catchup_keeps_skipping_genuinely_short_sids(db_env, monkeypatch,
                                                      tmp_path):
    """Counter-test: a sid with skip:short_session AND only 2 user events must
    stay skipped (not re-fired)."""
    db, _ = db_env
    projects = tmp_path / "projects"
    proj_dir = projects / "-Users-test"
    proj_dir.mkdir(parents=True)
    sid = "stays-skipped-sid"
    _write_real_jsonl(proj_dir / f"{sid}.jsonl", sid)
    now = time.time()
    os.utime(proj_dir / f"{sid}.jsonl", (now - 3600, now - 3600))
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'skip:short_session')",
            (sid,))
        for i in range(2):
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content)"
                " VALUES (?, ?, 'user', ?)",
                (sid, f"2026-05-24T10:{i:02d}:00Z", f"msg {i}"))
    conn.close()
    monkeypatch.setattr(
        "marrow.sessionstart_catchup._CC_PROJECTS", projects)
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


def test_catchup_picks_pending_sids(db_env, monkeypatch, tmp_path):
    """ok/skip → skip; first fail → retry once; second fail → skip;
    no audit → spawn; alive (idle<10min) → skip; lone 'start' (silent
    death) counts as one fail → retry; two silent deaths → skip."""
    db, _ = db_env
    projects = tmp_path / "projects"
    proj_dir = projects / "-Users-test"
    proj_dir.mkdir(parents=True)

    sid_done = "aaaaaaaa-done"
    sid_pending = "bbbbbbbb-pending"
    sid_failed_once = "cccccccc-failed-once"
    sid_failed_twice = "eeeeeeee-failed-twice"
    sid_alive = "dddddddd-alive"
    sid_silent_once = "ffffffff-silent-once"
    sid_silent_twice = "11111111-silent-twice"

    for sid in (sid_done, sid_pending, sid_failed_once,
                sid_failed_twice, sid_alive,
                sid_silent_once, sid_silent_twice):
        _write_real_jsonl(proj_dir / f"{sid}.jsonl", sid)

    now = time.time()
    old = now - 3600  # 1h ago, well past idle guard
    for sid in (sid_done, sid_pending, sid_failed_once, sid_failed_twice,
                sid_silent_once, sid_silent_twice):
        os.utime(proj_dir / f"{sid}.jsonl", (old, old))

    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'ok')", (sid_done,))
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'fail:LLMError')",
            (sid_failed_once,))
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'fail:LLMError')",
            (sid_failed_twice,))
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'fail:Other')",
            (sid_failed_twice,))
        # One silent death: a 'start' row with no matching terminal row.
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'start')",
            (sid_silent_once,))
        # Two silent deaths: two 'start' rows, no terminal → counts as 2 fails.
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'start')",
            (sid_silent_twice,))
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'start')",
            (sid_silent_twice,))
    conn.close()

    monkeypatch.setattr(
        "marrow.sessionstart_catchup._CC_PROJECTS", projects)
    monkeypatch.setattr("marrow.sessionstart_catchup.MAX_FIRE", 5)

    spawned: list[list[str]] = []

    def fake_popen(args, log_path):  # noqa: ARG001
        spawned.append(list(args))

    with patch("marrow.sessionstart_catchup.popen_detach", side_effect=fake_popen):
        from marrow import sessionstart_catchup
        rc = sessionstart_catchup.main()

    assert rc == 0
    fired = {args[args.index("--sid") + 1] for args in spawned}
    assert fired == {sid_pending, sid_failed_once, sid_silent_once}, fired


def test_catchup_cap_caps_at_max_fire(db_env, monkeypatch, tmp_path):
    """3 pending jsonls but MAX_FIRE=2 → only 2 spawn; newest mtime wins."""
    db, _ = db_env  # noqa: F841
    projects = tmp_path / "projects"
    proj_dir = projects / "-Users-test"
    proj_dir.mkdir(parents=True)

    sids = ["sid-oldest", "sid-mid", "sid-newest"]
    for sid in sids:
        _write_real_jsonl(proj_dir / f"{sid}.jsonl", sid)

    now = time.time()
    os.utime(proj_dir / "sid-oldest.jsonl",  (now - 7200, now - 7200))
    os.utime(proj_dir / "sid-mid.jsonl",     (now - 3600, now - 3600))
    os.utime(proj_dir / "sid-newest.jsonl",  (now - 1800, now - 1800))

    monkeypatch.setattr(
        "marrow.sessionstart_catchup._CC_PROJECTS", projects)

    spawned: list[list[str]] = []
    with patch("marrow.sessionstart_catchup.popen_detach",
               side_effect=lambda a, log_path: spawned.append(list(a))):
        from marrow import sessionstart_catchup
        sessionstart_catchup.main()

    assert len(spawned) == 2
    fired = [args[args.index("--sid") + 1] for args in spawned]
    assert fired == ["sid-newest", "sid-mid"]


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
        {"session_id": "sid-hook-test", "transcript_path": str(jl)})))
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
    """TASK_CAND segment writes to renamed `tasks` table."""
    db, _ = db_env
    from marrow import sessionend_async
    conn = storage.connect(db)
    try:
        raw = (
            "===TASK_CAND===\n"
            "[{\"title\": \"Ship 2.5c\", \"category\": \"Project\","
            " \"status\": \"active\","
            " \"due\": null, \"completed_at\": null, \"note\": \"\"}]\n"
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
    from marrow import sessionend_async
    conn = storage.connect(db)
    try:
        raw = (
            "===TASK_CAND===\n"
            "[{\"title\": \"flu vac\", \"category\": \"daily\","
            " \"status\": \"active\", \"due\": null, \"completed_at\": null,"
            " \"note\": \"\"},"
            " {\"title\": \"random thing\", \"category\": \"banana\","
            " \"status\": \"active\", \"due\": null, \"completed_at\": null,"
            " \"note\": \"\"},"
            " {\"title\": \"no cat field\","
            " \"status\": \"active\", \"due\": null, \"completed_at\": null,"
            " \"note\": \"\"}]\n"
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
    assert rows and rows[-1]["summary"] == "ok"


# ── handover segment (Plan H — STATE + NARRATIVE split) ─────────────────────

def test_sessionend_two_calls_routes_to_four_writers(db_env, tmp_path,
                                                      monkeypatch):
    """STATE + NARRATIVE → 4 segment writers + per-segment audit + final 'ok'.
    Plan H: client.call invoked twice; handover lands DONE/OPEN/PLAN/REFERENCE."""
    db, _ = db_env
    _insert_events(db, "test-combined", count=10, role="user")

    h = tmp_path / "handover.md"

    state_raw = (
        "===TASK_CAND===\n"
        "[{\"title\": \"refactor sessionend\", \"status\": \"done\","
        " \"due\": null, \"completed_at\": null, \"note\": \"\"}]\n"
        "===END===\n"
        "===HANDOVER===\n"
        "===DONE===\n- shipped 2-call refactor\n"
        "===OPEN===\n- N/A\n"
        "===PLAN===\n- verify pytest + plist reload\n"
        "===REFERENCE===\n- `marrow/sessionend_async.py:80` — main loop\n"
        "===END===\n"
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
        assert summaries[-1] == "ok"
        # All 4 segment audit rows logged 'ok'.
        seg_oks = [r for r in seg_rows
                   if r["action"].startswith("sessionend_extract_")
                   and r["summary"] == "ok"]
        assert len(seg_oks) == 4
    finally:
        conn.close()
    body = h.read_text(encoding="utf-8")
    assert "- shipped 2-call refactor" in body
    assert "- verify pytest + plist reload" in body
    assert "handover: ready sid:test-combined" in body
    assert "## Done" in body and "## Open" in body
    assert "## Plan" in body and "## Reference" in body


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


def test_load_prior_handover_extracts_four_state_sections(tmp_path, monkeypatch):
    """Plan H: prior handover loader pulls Done/Open/Plan/Reference now,
    not the legacy Previous/This/Next/Reference."""
    from marrow import sessionend_async, handover_render
    h = tmp_path / "handover.md"
    h.write_text(
        "# title\n\n"
        "## Alerts (active)\n- warn x\n\n"
        "## Tasks\n- [ ] foo\n\n"
        "## Done\n- did A\n\n"
        "## Open\n- blocked on B\n\n"
        "## Plan\n- pick C\n\n"
        "## Reference\n- `path/x.py:1` — y\n\n"
        "<!-- stamp -->\n", encoding="utf-8")
    monkeypatch.setattr(handover_render, "_RENDERED_PATH", h)
    out = sessionend_async._load_prior_handover_for_sonnet()
    assert "## Done\n- did A" in out
    assert "## Open\n- blocked on B" in out
    assert "## Plan\n- pick C" in out
    assert "## Reference\n- `path/x.py:1` — y" in out
    assert "Alerts" not in out and "Tasks" not in out


def test_load_prior_handover_returns_placeholder_when_missing(tmp_path, monkeypatch):
    from marrow import sessionend_async, handover_render
    monkeypatch.setattr(handover_render, "_RENDERED_PATH", tmp_path / "nope.md")
    assert sessionend_async._load_prior_handover_for_sonnet() == "(no prior handover)"


def test_parse_handover_output_slices_four_state_blocks():
    """parse_handover_output returns (done, open, plan, reference)."""
    from marrow.sessionend_prompts import parse_handover_output
    raw = ("intro\n"
           "===DONE===\n- did A\n- did B\n"
           "===OPEN===\n- waiting on review\n"
           "===PLAN===\n- pick up C\n"
           "===REFERENCE===\n- `path/foo.py:10` — entry\n"
           "===END===\n")
    done, open_, plan, reference = parse_handover_output(raw)
    assert done == "- did A\n- did B"
    assert open_ == "- waiting on review"
    assert plan == "- pick up C"
    assert reference == "- `path/foo.py:10` — entry"


def test_parse_handover_output_missing_markers_default_empty():
    from marrow.sessionend_prompts import parse_handover_output
    raw = "===DONE===\n- only one\n===END===\n"
    done, open_, plan, reference = parse_handover_output(raw)
    assert done == "- only one"
    assert open_ == "" and plan == "" and reference == ""


def test_seg_handover_composes_full_file(db_env, tmp_path, monkeypatch):
    """seg_handover writes complete handover.md in one atomic call."""
    db, _ = db_env
    h = tmp_path / "handover.md"
    monkeypatch.setattr("marrow.handover_render._RENDERED_PATH", h)
    raw = ("===DONE===\n- shipped phase 3 handover\n"
           "===OPEN===\n- N/A\n"
           "===PLAN===\n- launchctl + commit\n"
           "===REFERENCE===\n- N/A\n"
           "===END===\n")
    conn = storage.connect(db)
    try:
        from marrow.sessionend_writers import seg_handover
        n = seg_handover(conn, raw, "S1")
    finally:
        conn.close()
    assert n == 1
    body = h.read_text(encoding="utf-8")
    assert "- shipped phase 3 handover" in body
    assert "- launchctl + commit" in body
    assert "handover: ready sid:S1" in body
    assert "handover: pending" not in body
    assert "## Done" in body and "## Plan" in body


def test_seg_handover_noop_on_empty_blocks(db_env, tmp_path, monkeypatch):
    """If LLM returns no markers, leave file untouched."""
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
