"""Tests for the MARROW_BRIDGE=1 gate.

When synapse-wx wraps cc, the bridge owns sessionend timing (fires async
on 6h idle, not on every /model swap or kill). The gate must:

  - SessionEnd with env: archive + lifecycle:end + bridge_owns marker, no popen.
  - SessionEnd without env: existing popen path intact.
  - sessionstart_catchup: bridge_owns marker → skip (until newer extract row).
  - sessionstart_catchup: marker + later fail extract → spawn (state 5 retry).
"""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from marrow import config, hooks, storage
from marrow.hooks import session_end
from marrow.sessionstart_catchup import _classify


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


def _audit_latest(db: str, sid: str, action: str) -> str | None:
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE action=? AND target_id=? ORDER BY id DESC LIMIT 1",
            (action, sid),
        ).fetchone()
        return row["summary"] if row else None
    finally:
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


# ── SessionEnd: env present ──────────────────────────────────────────────────

def test_session_end_with_bridge_env_skips_popen(env, monkeypatch, tmp_path):
    """MARROW_BRIDGE=1: archive runs, lifecycle:end + bridge_owns written,
    popen_detach NOT called."""
    db, _ = env
    sid = "bridge-sid-1"
    tpath = tmp_path / "fake.jsonl"
    tpath.write_text("")
    _stdin(monkeypatch, {
        "session_id": sid,
        "cwd": str(tmp_path),  # non-worktree dir
        "transcript_path": str(tpath),
    })
    monkeypatch.setenv("MARROW_BRIDGE", "1")

    # transcript.clean returns one row so sid is captured downstream.
    fake_rows = [{"session_id": sid, "role": "user", "content": "hi",
                  "timestamp": "2026-06-02T00:00:00Z",
                  "source_hash": "h1"}]
    with patch.object(hooks.transcript, "clean", return_value=fake_rows) as mclean, \
         patch.object(hooks.repo, "archive_events") as march, \
         patch.object(hooks.transcript, "is_headless", return_value=False), \
         patch.object(hooks, "_is_worktree_session", return_value=False), \
         patch.object(hooks, "popen_detach") as mpop:
        rc = session_end()

    assert rc == 0
    mclean.assert_called_once()
    march.assert_called_once()
    mpop.assert_not_called()

    # Both audit rows present.
    assert _audit_latest(db, sid, "session_lifecycle:end") == ""
    assert _audit_latest(db, sid, "manual_skip") == "bridge_owns"


# ── SessionEnd: env absent ───────────────────────────────────────────────────

def test_session_end_without_env_spawns_popen(env, monkeypatch, tmp_path):
    """No MARROW_BRIDGE env: existing popen_detach path runs."""
    db, _ = env
    sid = "normal-sid-1"
    tpath = tmp_path / "fake.jsonl"
    tpath.write_text("")
    _stdin(monkeypatch, {
        "session_id": sid,
        "cwd": str(tmp_path),
        "transcript_path": str(tpath),
    })
    monkeypatch.delenv("MARROW_BRIDGE", raising=False)

    fake_rows = [{"session_id": sid, "role": "user", "content": "hi",
                  "timestamp": "2026-06-02T00:00:00Z",
                  "source_hash": "h2"}]
    with patch.object(hooks.transcript, "clean", return_value=fake_rows), \
         patch.object(hooks.repo, "archive_events"), \
         patch.object(hooks.transcript, "is_headless", return_value=False), \
         patch.object(hooks, "_is_worktree_session", return_value=False), \
         patch.object(hooks, "popen_detach") as mpop:
        rc = session_end()

    assert rc == 0
    mpop.assert_called_once()
    # No bridge_owns marker.
    assert _audit_latest(db, sid, "manual_skip") is None


# ── catchup: bridge_owns marker present, no newer extract ────────────────────

def test_catchup_bridge_owns_classifies_skip(env):
    db, _ = env
    sid = "bridge-sid-2"
    conn = storage.connect(db)
    try:
        # lifecycle:start + lifecycle:end + bridge_owns marker, NO ok/fail.
        with conn:
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('events', ?, 'session_lifecycle:start',"
                " 'ppid=99999,source=cc,started_at=0')",
                (sid,),
            )
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('events', ?, 'session_lifecycle:end', '')",
                (sid,),
            )
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('events', ?, 'manual_skip', 'bridge_owns')",
                (sid,),
            )

        # Empty live ppids set — ppid 99999 is dead, so without the bridge gate
        # _classify would fall to state 5 (end + no ok + grace passed is the
        # near case; here grace not passed but we want to assert the gate
        # short-circuits regardless of downstream state).
        result = _classify(conn, sid, set())
    finally:
        conn.close()
    assert result == "skip"


# ── catchup: bridge_owns superseded by later fail row ────────────────────────

def test_catchup_bridge_owns_superseded_by_fail_spawns(env):
    """Marker written, then bridge manually fired sessionend_async which
    failed (fail:* row). Catchup must NOT honor the stale marker — fall
    through to state 5 retry path."""
    db, _ = env
    sid = "bridge-sid-3"
    conn = storage.connect(db)
    try:
        with conn:
            # Old lifecycle:start (ppid dead, plus end > 5min ago to trigger state 5).
            conn.execute(
                "INSERT INTO audit_log"
                " (target_table, target_id, action, summary, occurred_at)"
                " VALUES ('events', ?, 'session_lifecycle:start',"
                " 'ppid=99999,source=cc,started_at=0',"
                " '2026-05-01T00:00:00Z')",
                (sid,),
            )
            conn.execute(
                "INSERT INTO audit_log"
                " (target_table, target_id, action, summary, occurred_at)"
                " VALUES ('events', ?, 'session_lifecycle:end', '',"
                " '2026-05-01T00:00:00Z')",
                (sid,),
            )
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('events', ?, 'manual_skip', 'bridge_owns')",
                (sid,),
            )
        # Later fail row written by the bridge's manual fire — higher id.
        _insert_extract(db, sid, "fail:timeout")
        result = _classify(conn, sid, set())
    finally:
        conn.close()
    # End marker + no ok row + > 5min elapsed → state 5 spawn.
    assert result == "spawn"


# ── catchup: bridge_owns marker older than TTL falls through ─────────────────

def test_catchup_bridge_owns_ttl_expired_spawns(env):
    """Bridge crashed and never recovered. Marker > 12h old, no manual fire
    ever happened. TTL must let catchup fall through to state 5 spawn so the
    sid isn't orphaned forever."""
    db, _ = env
    sid = "bridge-sid-4"
    conn = storage.connect(db)
    try:
        with conn:
            # lifecycle:start/end + bridge_owns marker all stamped 24h ago.
            conn.execute(
                "INSERT INTO audit_log"
                " (target_table, target_id, action, summary, occurred_at)"
                " VALUES ('events', ?, 'session_lifecycle:start',"
                " 'ppid=99999,source=cc,started_at=0',"
                " '2026-05-01T00:00:00Z')",
                (sid,),
            )
            conn.execute(
                "INSERT INTO audit_log"
                " (target_table, target_id, action, summary, occurred_at)"
                " VALUES ('events', ?, 'session_lifecycle:end', '',"
                " '2026-05-01T00:00:00Z')",
                (sid,),
            )
            conn.execute(
                "INSERT INTO audit_log"
                " (target_table, target_id, action, summary, occurred_at)"
                " VALUES ('events', ?, 'manual_skip', 'bridge_owns',"
                " '2026-05-01T00:00:00Z')",
                (sid,),
            )
        result = _classify(conn, sid, set())
    finally:
        conn.close()
    # Stale marker (> 12h) → TTL kicks in → fall through to state 5.
    assert result == "spawn"
