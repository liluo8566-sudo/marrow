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

from marrow import config, storage
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
    assert len(rows) == 1
    assert rows[0]["summary"] == "skip:short_session"
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
    assert len(rows) == 1
    assert rows[0]["summary"] == "ok"


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


def test_sessionend_async_writes_fail_audit_on_exception(db_env):
    """Single sonnet call raises → final summary='fail:RuntimeError', rc=1."""
    db, _ = db_env
    _insert_events(db, "test-fail", count=10, role="user")

    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.side_effect = RuntimeError("boom")
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", "test-fail"])

    assert rc == 1
    rows = _audit_rows(db, "test-fail")
    assert len(rows) == 1
    assert rows[0]["summary"] == "fail:RuntimeError"


# ── Unit 3: sessionstart_catchup ─────────────────────────────────────────────

def test_catchup_picks_pending_sids(db_env, monkeypatch):
    """Only sid='b' is pending; a=ok and c=skip:short_session are handled."""
    db, _ = db_env

    for sid in ["a", "b", "c"]:
        _insert_events(db, sid, count=1)

    # Mark 'a' as successfully extracted.
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', 'a', 'sessionend_extract', 'ok')",
        )
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', 'c', 'sessionend_extract', 'skip:short_session')",
        )
    conn.close()

    spawned: list[list[str]] = []

    def fake_popen(args, log_path):  # noqa: ARG001
        spawned.append(args)

    with patch("marrow.sessionstart_catchup.popen_detach", side_effect=fake_popen):
        from marrow import sessionstart_catchup
        rc = sessionstart_catchup.main()

    assert rc == 0
    assert len(spawned) == 1, f"expected 1 spawn, got {len(spawned)}: {spawned}"
    assert "--sid" in spawned[0]
    sid_idx = spawned[0].index("--sid") + 1
    assert spawned[0][sid_idx] == "b"


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
    monkeypatch.setattr(config, "sub_pages_path",
                        lambda: str(tmp_path / "sub_pages"))
    monkeypatch.setattr(config, "sub_pages_state_path",
                        lambda: str(tmp_path / "sub_state"))

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
        n = sessionend_async._seg_digest(conn, raw, "sid-d1", "2026-05-23")
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
        sessionend_async._seg_digest(
            conn, "===DIGEST===\nfirst\n===END===", "sid-r1", "2026-05-23")
        sessionend_async._seg_digest(
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
        n = sessionend_async._seg_digest(conn, "no markers here", "sid-x",
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
        n = sessionend_async._seg_affect(conn, raw, "sid-a1", "2026-05-23")
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
        sessionend_async._seg_affect(conn, raw, "sid-a2", "2026-05-23")
        row = conn.execute(
            "SELECT reconcile_prev_text FROM affect WHERE date='2026-05-23'"
        ).fetchone()
        assert row["reconcile_prev_text"] is None
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
            "[{\"title\": \"Ship 2.5c\", \"status\": \"active\","
            " \"due\": null, \"completed_at\": null, \"note\": \"\"}]\n"
            "===END===\n"
        )
        n = sessionend_async._seg_task_cand(conn, raw)
        assert n == 1
        row = conn.execute(
            "SELECT title, status FROM tasks WHERE title='Ship 2.5c'"
        ).fetchone()
        assert row is not None
        assert row["status"] == "active"
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


# ── handover segment ────────────────────────────────────────────────────────

def test_sessionend_single_call_routes_to_four_writers(db_env, tmp_path,
                                                        monkeypatch):
    """One sonnet response containing all 4 blocks fans out to each writer
    and produces 4 per-segment audit rows + final 'ok'."""
    db, _ = db_env
    _insert_events(db, "test-combined", count=10, role="user")

    h = tmp_path / "handover.md"
    h.write_text(
        "# h\n\n## This Session\n\n\n## Next Session\n\n\n"
        "<!-- handover: pending sid:test-combined -->\n",
        encoding="utf-8",
    )

    combined_raw = (
        "===AFFECT===\n"
        "[{\"ep\": 1, \"valence\": 0.8, \"arousal\": 0.5,"
        " \"importance\": 3, \"label\": \"愉悦\", \"entities\": [],"
        " \"event_hint\": \"\", \"unresolved\": 0,"
        " \"reconcile_prev\": \"N/A\"}]\n"
        "===END===\n"
        "===TASK_CAND===\n"
        "[{\"title\": \"refactor sessionend\", \"status\": \"done\","
        " \"due\": null, \"completed_at\": null, \"note\": \"\"}]\n"
        "===END===\n"
        "===DIGEST===\n"
        "Refactored sessionend to 1 call.\n"
        "===END===\n"
        "===HANDOVER===\n"
        "===THIS_SESSION===\n- shipped 4→1 call refactor\n"
        "===NEXT_SESSION===\n- verify pytest + plist reload\n"
        "===END===\n"
    )

    with patch("marrow.sessionend_async.LLMClient") as MockClient, \
         patch("marrow.sessionend_async._HANDOVER_PATH", h):
        MockClient.return_value.call.return_value = combined_raw
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", "test-combined"])

    assert rc == 0
    conn = storage.connect(db)
    try:
        # 1 affect row, 1 task row, 1 digest row.
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
        # 4 per-segment audit rows + 1 final summary row.
        seg_rows = conn.execute(
            "SELECT action, summary FROM audit_log"
            " WHERE target_id='test-combined' ORDER BY id"
        ).fetchall()
        actions = [r["action"] for r in seg_rows]
        assert actions == [
            "sessionend_extract_affect",
            "sessionend_extract_task_cand",
            "sessionend_extract_digest",
            "sessionend_extract_handover",
            "sessionend_extract",
        ]
        assert seg_rows[-1]["summary"] == "ok"
    finally:
        conn.close()
    # Handover file updated.
    body = h.read_text(encoding="utf-8")
    assert "shipped 4→1 call refactor" in body
    assert "handover: ready sid:test-combined" in body


def test_handover_parses_two_blocks():
    from marrow.sessionend_async import _parse_handover_blocks
    raw = ("intro\n===THIS_SESSION===\n- did A\n- did B\n"
           "===NEXT_SESSION===\n- pick up C\n===END===\n")
    this_s, next_s = _parse_handover_blocks(raw)
    assert this_s == "- did A\n- did B"
    assert next_s == "- pick up C"


def test_handover_inject_section_replaces_body():
    from marrow.sessionend_async import _inject_section
    text = "## This Session\nold\nstuff\n\n## Next Session\nkeep\n"
    out = _inject_section(text, "This Session", "- new")
    assert "## This Session\n- new\n" in out
    assert "## Next Session\nkeep" in out
    assert "old" not in out


def test_seg_handover_injects_into_handover(tmp_path, monkeypatch):
    from marrow import sessionend_async
    h = tmp_path / "handover.md"
    h.write_text(
        "# Marrow handover\n\n## This Session\n\n\n## Next Session\n\n\n"
        "<!-- handover: pending sid:S1 -->\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sessionend_async, "_HANDOVER_PATH", h)
    raw = ("===THIS_SESSION===\n- shipped phase 2.5c\n"
           "===NEXT_SESSION===\n- launchctl + commit\n===END===\n")
    n = sessionend_async._seg_handover(raw, "S1")
    assert n == 1
    body = h.read_text(encoding="utf-8")
    assert "- shipped phase 2.5c" in body
    assert "- launchctl + commit" in body
    assert "handover: ready sid:S1" in body
    assert "handover: pending" not in body


def test_seg_handover_sid_lag_label(tmp_path, monkeypatch):
    from marrow import sessionend_async
    h = tmp_path / "handover.md"
    h.write_text(
        "## This Session\n\n## Next Session\n\n"
        "<!-- handover: pending sid:OLD -->\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sessionend_async, "_HANDOVER_PATH", h)
    raw = ("===THIS_SESSION===\n- x\n===NEXT_SESSION===\n- y\n===END===\n")
    sessionend_async._seg_handover(raw, "NEW")
    body = h.read_text(encoding="utf-8")
    assert "handover sid=NEW, skeleton sid=OLD" in body
