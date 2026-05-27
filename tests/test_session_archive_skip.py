"""Tests for manual session archive skip controls (mm- / mm+ / resume).

Feature: session_archive_skip_manual
- mm- prefix: UserPromptSubmit writes audit_log manual_skip/skip row for sid
- sessionend_async respects skip flag, bypasses LLM + diary + handover
- mm+ prefix / mw sessionend rerun: force-overwrite done marker, rerun pipeline
- resume: session_start detects prior lifecycle:start row, clears any skip
- auto 3-turn skip is unchanged
"""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from marrow import config, hooks, storage
from marrow.hooks import _is_manual_skip, _write_manual_skip_flag


# ── shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "dashboard_path",
                        lambda: str(tmp_path / "dashboard.md"))
    monkeypatch.setattr(config, "sub_pages_path",
                        lambda: str(tmp_path / "db-pages"))
    monkeypatch.setattr(config, "sub_pages_state_path",
                        lambda: str(tmp_path / "db_state"))
    return db, tmp_path


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _insert_events(db: str, sid: str, count: int, role: str = "user") -> None:
    conn = storage.connect(db)
    with conn:
        for i in range(count):
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content)"
                " VALUES (?, ?, ?, ?)",
                (sid, f"2026-05-27T10:{i:02d}:00Z", role, f"msg {i}"),
            )
    conn.close()


def _audit_rows(db: str, sid: str, action: str = "manual_skip") -> list[dict]:
    conn = storage.connect(db)
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE action=? AND target_id=? ORDER BY id",
            (action, sid),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Test 1: mm- writes skip flag ──────────────────────────────────────────────

def test_mm_minus_writes_skip_flag(env, monkeypatch):
    """mm- prompt (no arg) -> audit_log has manual_skip/skip row for current sid."""
    db, _ = env
    sid = "test-mm-minus-sid"
    _stdin(monkeypatch, {"prompt": "mm-", "session_id": sid})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    rows = _audit_rows(db, sid, action="manual_skip")
    assert len(rows) == 1
    assert rows[0]["target_id"] == sid
    assert rows[0]["action"] == "manual_skip"
    assert rows[0]["summary"] == "skip"


# ── Test 2: skip blocks sessionend LLM ───────────────────────────────────────

def test_skip_blocks_sessionend_llm(env, monkeypatch):
    """manual_skip/skip row -> sessionend_async must NOT call LLM, no diary/handover write."""
    db, tmp_path = env
    sid = "test-skip-blocks-sid"
    _insert_events(db, sid, count=10)

    # Write the skip flag directly.
    conn = storage.connect(db)
    _write_manual_skip_flag(conn, sid, "skip")
    conn.close()

    llm_called = []
    h = tmp_path / "handover.md"

    with patch("marrow.sessionend_async.LLMClient") as MockClient, \
         patch("marrow.handover_render._RENDERED_PATH", h):
        MockClient.return_value.call.side_effect = lambda *a, **kw: llm_called.append(1)
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", sid])

    assert rc == 0
    assert llm_called == [], "LLM must not be called when manual skip is set"
    assert not h.exists(), "handover.md must not be written when skipped"

    # Audit row must record the manual skip.
    extract_rows = _audit_rows(db, sid, action="sessionend_extract")
    summaries = [r["summary"] for r in extract_rows]
    assert any(s == "skip:manual" for s in summaries), (
        f"expected skip:manual in audit_log, got: {summaries!r}")


# ── Test 3: mm+ reruns sid ────────────────────────────────────────────────────

def test_mm_plus_reruns_sid(env, monkeypatch, tmp_path):
    """mm+ <uuid> -> force-clears done marker, sessionend_async runs end-to-end (LLM called + write)."""
    db, data_tmp = env
    sid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    _insert_events(db, sid, count=10)

    # Pre-seed an ok row to simulate already-done state.
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'ok,user_count=10')",
            (sid,),
        )
    conn.close()

    spawned = []

    def fake_popen(args, log_path):
        spawned.append(list(args))

    _stdin(monkeypatch, {"prompt": f"mm+ {sid}", "session_id": "current-sid"})
    with patch("marrow.hooks.popen_detach", side_effect=fake_popen):
        rc = hooks.main(["user_prompt_submit"])
    assert rc == 0

    # popen must have been called with sessionend_async --sid <sid>.
    async_calls = [c for c in spawned if "sessionend_async" in " ".join(c)]
    assert len(async_calls) == 1, f"expected 1 sessionend_async spawn, got: {spawned}"
    assert "--sid" in async_calls[0]
    idx = async_calls[0].index("--sid") + 1
    assert async_calls[0][idx] == sid

    # reset:mm_plus row must exist in audit_log.
    reset_rows = _audit_rows(db, sid, action="sessionend_extract")
    reset_summaries = [r["summary"] for r in reset_rows]
    assert "reset:mm_plus" in reset_summaries, (
        f"expected reset:mm_plus row, got: {reset_summaries!r}")

    # After reset, sessionend_async should call LLM (ok row was overridden by reset row).
    llm_called = []
    h = data_tmp / "handover.md"
    with patch("marrow.sessionend_async.LLMClient") as MockClient, \
         patch("marrow.handover_render._RENDERED_PATH", h):
        MockClient.return_value.call.return_value = "echo done"
        from marrow import sessionend_async
        rc2 = sessionend_async.main(["--sid", sid])
    assert rc2 == 0
    assert MockClient.return_value.call.called, "LLM must be called after mm+ rerun"


# ── Test 4: resume clears skip, sessionend runs normally ─────────────────────

def test_resume_clears_skip(env, monkeypatch, tmp_path):
    """Write skip row, simulate resume (session_start with prior lifecycle:start),
    assert skip_cleared row written, then sessionend LLM IS called."""
    db, data_tmp = env
    sid = "test-resume-clear-sid"
    _insert_events(db, sid, count=10)

    # Write manual skip.
    conn = storage.connect(db)
    _write_manual_skip_flag(conn, sid, "skip")
    # Write a lifecycle:start row to mark this as a resumed session.
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'session_lifecycle:start', 'ppid=999,source=cc,started_at=0')",
            (sid,),
        )
    conn.close()

    assert _is_manual_skip(storage.connect(db), sid), "skip must be set before resume"

    # Simulate session_start (resume) for same sid.
    _stdin(monkeypatch, {"session_id": sid})
    with patch("marrow.hooks.popen_detach"):
        rc = hooks.main(["session_start"])
    assert rc == 0

    # skip_cleared row must now exist.
    cleared_rows = _audit_rows(db, sid, action="manual_skip")
    summaries = [r["summary"] for r in cleared_rows]
    assert "skip_cleared" in summaries, (
        f"expected skip_cleared row after resume, got: {summaries!r}")

    # _is_manual_skip must now return False (latest row wins).
    conn2 = storage.connect(db)
    try:
        assert not _is_manual_skip(conn2, sid), "skip should be cleared after resume"
    finally:
        conn2.close()

    # sessionend_async must call LLM now that skip is cleared.
    llm_called = []
    h = data_tmp / "handover.md"
    with patch("marrow.sessionend_async.LLMClient") as MockClient, \
         patch("marrow.handover_render._RENDERED_PATH", h):
        MockClient.return_value.call.return_value = "echo done"
        from marrow import sessionend_async
        rc2 = sessionend_async.main(["--sid", sid])
    assert rc2 == 0
    assert MockClient.return_value.call.called, "LLM must be called after resume clears skip"


# ── Test 5: auto 3-turn skip still works ─────────────────────────────────────

def test_auto_3turn_still_works(env, monkeypatch):
    """Session with <=3 user turns, no manual flag -> auto-skip preserved, LLM not called."""
    db, _ = env
    sid = "test-auto-3turn-sid"
    _insert_events(db, sid, count=2)  # 2 user events, below default threshold of 3

    llm_called = []
    with patch("marrow.sessionend_async.LLMClient") as MockClient:
        MockClient.return_value.call.side_effect = lambda *a, **kw: llm_called.append(1)
        from marrow import sessionend_async
        rc = sessionend_async.main(["--sid", sid])

    assert rc == 0
    assert llm_called == [], "LLM must not be called for auto-skip short sessions"

    # Must have a skip:short_session row.
    rows = _audit_rows(db, sid, action="sessionend_extract")
    summaries = [r["summary"] for r in rows]
    assert any(s.startswith("skip:short_session") for s in summaries), (
        f"expected skip:short_session, got: {summaries!r}")
