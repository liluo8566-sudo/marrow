"""Tests for the worktree-session gate in marrow.hooks.

Feature: cc instances launched inside a non-primary git worktree are
task-isolated runs whose dialogue must not enter marrow events. The gate
skips archive + LLM but still records lifecycle markers (summary carries
worktree=1) so catchup doesn't tag the sid as silent_death.

Detection uses real `git worktree list --porcelain`; tests spin up an
actual repo + linked worktree in tmp_path rather than mocking subprocess.
"""
from __future__ import annotations

import io
import json
import subprocess
from unittest.mock import patch

import pytest

from marrow import config, hooks, storage
from marrow.hooks import (
    _is_worktree_session,
    _primary_worktree,
    session_end,
    session_start,
    user_prompt_submit,
)


# ── shared fixtures ───────────────────────────────────────────────────────────

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


@pytest.fixture()
def repo_with_worktree(tmp_path):
    """Create a primary repo + one linked worktree. Returns (primary, wt)."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _run = lambda *a, **k: subprocess.run(
        a, cwd=str(primary), capture_output=True, text=True, check=True, **k,
    )
    _run("git", "init", "-q", "-b", "main")
    _run("git", "config", "user.email", "t@t")
    _run("git", "config", "user.name", "t")
    (primary / "f").write_text("x")
    _run("git", "add", "f")
    _run("git", "commit", "-qm", "init")
    wt = tmp_path / "wt"
    _run("git", "worktree", "add", "-q", str(wt), "-b", "feat")
    return str(primary), str(wt)


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _audit_summary(db: str, sid: str, action: str) -> str | None:
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


# ── detector ─────────────────────────────────────────────────────────────────

class TestIsWorktreeSession:
    def test_empty_cwd_false(self):
        assert _is_worktree_session("") is False

    def test_missing_cwd_false(self, tmp_path):
        assert _is_worktree_session(str(tmp_path / "nope")) is False

    def test_non_git_dir_false(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        assert _is_worktree_session(str(d)) is False

    def test_primary_worktree_false(self, repo_with_worktree):
        primary, _ = repo_with_worktree
        assert _is_worktree_session(primary) is False

    def test_linked_worktree_true(self, repo_with_worktree):
        _, wt = repo_with_worktree
        assert _is_worktree_session(wt) is True

    def test_primary_worktree_helper(self, repo_with_worktree):
        primary, wt = repo_with_worktree
        # Both views must resolve to the same primary realpath.
        from os.path import realpath
        assert _primary_worktree(primary) == realpath(primary)
        assert _primary_worktree(wt) == realpath(primary)


# ── session_start ─────────────────────────────────────────────────────────────

class TestSessionStartWorktreeGate:
    def test_worktree_session_empty_context(
        self, env, repo_with_worktree, monkeypatch, capsys
    ):
        db, _ = env
        _, wt = repo_with_worktree
        sid = "wt-session-1"
        _stdin(monkeypatch, {"session_id": sid, "cwd": wt})
        # popen_detach noop so the test doesn't fork catchup.
        with patch.object(hooks, "popen_detach"):
            rc = session_start()
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        # No affect/handoff injection for worktree sessions.
        assert ctx == ""
        # Lifecycle row carries worktree=1 marker for forensics.
        summary = _audit_summary(db, sid, "session_lifecycle:start")
        assert summary is not None
        assert "worktree=1" in summary

    def test_primary_session_unchanged(
        self, env, repo_with_worktree, monkeypatch, capsys
    ):
        db, _ = env
        primary, _ = repo_with_worktree
        sid = "primary-session-1"
        _stdin(monkeypatch, {"session_id": sid, "cwd": primary})
        with patch.object(hooks, "popen_detach"):
            rc = session_start()
        assert rc == 0
        # Lifecycle row does NOT carry worktree=1 marker.
        summary = _audit_summary(db, sid, "session_lifecycle:start")
        assert summary is not None
        assert "worktree=1" not in summary
        # additionalContext still flows (handoff at minimum).
        out = json.loads(capsys.readouterr().out)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "Marrow handoff" in ctx


# ── session_end ───────────────────────────────────────────────────────────────

class TestSessionEndWorktreeGate:
    def test_worktree_session_skips_archive(
        self, env, repo_with_worktree, monkeypatch, tmp_path
    ):
        db, _ = env
        _, wt = repo_with_worktree
        sid = "wt-end-1"
        # Fake transcript path (would be parsed in non-worktree path).
        tpath = tmp_path / "fake.jsonl"
        tpath.write_text("")
        _stdin(monkeypatch, {
            "session_id": sid,
            "cwd": wt,
            "transcript_path": str(tpath),
        })
        # transcript.clean / archive_events must NOT be called for worktree.
        with patch.object(hooks.transcript, "clean") as mclean, \
             patch.object(hooks.repo, "archive_events") as march, \
             patch.object(hooks, "popen_detach") as mpop:
            rc = session_end()
        assert rc == 0
        mclean.assert_not_called()
        march.assert_not_called()
        mpop.assert_not_called()
        # Lifecycle:end still recorded with worktree=1 marker so catchup
        # doesn't tag this sid as silent_death.
        summary = _audit_summary(db, sid, "session_lifecycle:end")
        assert summary == "worktree=1"

    def test_worktree_no_sid_still_returns_0(
        self, env, repo_with_worktree, monkeypatch, tmp_path
    ):
        _, wt = repo_with_worktree
        tpath = tmp_path / "fake.jsonl"
        tpath.write_text("")
        _stdin(monkeypatch, {"cwd": wt, "transcript_path": str(tpath)})
        with patch.object(hooks.transcript, "clean") as mclean:
            rc = session_end()
        assert rc == 0
        mclean.assert_not_called()


# ── user_prompt_submit ────────────────────────────────────────────────────────

class TestUserPromptSubmitWorktreeGate:
    def test_worktree_skips_recall(
        self, env, repo_with_worktree, monkeypatch, capsys
    ):
        """Worktree sessions: no recall injection, no token spend, no log
        spam — short-circuit before config load + db connect + recall_fusion."""
        _, wt = repo_with_worktree
        _stdin(monkeypatch, {
            "session_id": "wt-prompt-1",
            "cwd": wt,
            "prompt": "go fix the lint warning in bridge.py",
        })
        # Any call into config.load / storage.connect / recall_fusion would
        # mean the gate failed to fire — patch them as canaries.
        with patch.object(hooks.config, "load") as mload, \
             patch.object(hooks.storage, "connect") as mconn:
            rc = user_prompt_submit()
        assert rc == 0
        mload.assert_not_called()
        mconn.assert_not_called()
        # No additionalContext on stdout — pure no-op for worktree turn.
        assert capsys.readouterr().out == ""

    def test_primary_recall_still_runs(
        self, env, repo_with_worktree, monkeypatch
    ):
        """Sanity: primary worktree path must NOT short-circuit — config.load
        is reached (will then hit recall.vector gate; we don't care about the
        downstream behavior here, only that the gate didn't preempt it)."""
        primary, _ = repo_with_worktree
        _stdin(monkeypatch, {
            "session_id": "primary-prompt-1",
            "cwd": primary,
            "prompt": "what's left on this branch",
        })
        with patch.object(hooks.config, "load",
                          return_value={"recall": {"vector": False}}) as mload:
            rc = user_prompt_submit()
        assert rc == 0
        mload.assert_called()
