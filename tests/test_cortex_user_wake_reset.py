"""User-wake reset (Item 3): a real user message in a cortex window flips the
session awake, marks the reply, clears silence state, refunds wait_count only
if the interrupted wait was still live (unexpired), clears the pending floor
deadline + sentinel, and (re)spawns a watchdog. Machine lines (wake marker /
monitor death / tuck-in) down the ear channel must NOT trigger it.

marrow venv cannot import cortex, so wake_state.json is manipulated directly —
these tests exercise that direct path.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from marrow import config, cortex_bridge


@pytest.fixture()
def cortex_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    root = tmp_path / "repo"
    root.mkdir()
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {
            "enabled": True, "home": str(home),
            "venv_python": str(py), "repo_root": str(root),
            "wake_state_file": "wake_state.json",
            "watchdog_pidfile": "watchdog.pid",
            "wake_marker": "[CORTEX-WAKE]", "tuck_in_marker": "[TUCK-IN]",
        },
    })
    monkeypatch.setenv("MARROW_CORTEX", "1")
    # Stub the watchdog respawn (no real subprocess in tests).
    monkeypatch.setattr(cortex_bridge, "_spawn_watchdog_if_absent", lambda: None)
    return home, db


def _ws(home):
    return json.loads((home / "wake_state.json").read_text())


# --- machine-line exclusion ---------------------------------------------------

def test_is_machine_line_excludes_markers(cortex_env):
    assert cortex_bridge.is_machine_line("[CORTEX-WAKE] 14:00") is True
    assert cortex_bridge.is_machine_line(
        "⏳ [TUCK-IN] It's been 20 mins (Wait cap 0/2)") is True
    assert cortex_bridge.is_machine_line(
        "<task-notification>Monitor stopped — foo</task-notification>") is True
    assert cortex_bridge.is_machine_line("hey are you there?") is False
    assert cortex_bridge.is_machine_line("") is True


def test_is_machine_line_harness_tags(cortex_env):
    # Any harness-style tag at the very start -> machine.
    assert cortex_bridge.is_machine_line(
        "<task-notification>bg task ended</task-notification>") is True
    assert cortex_bridge.is_machine_line(
        "<system-reminder>context follows</system-reminder>") is True
    # Leading whitespace before the tag is tolerated.
    assert cortex_bridge.is_machine_line("  \n<system-reminder>x") is True
    # A tag mid-string is real user text, not a machine line.
    assert cortex_bridge.is_machine_line(
        "look at this <system-reminder> in my message") is False
    assert cortex_bridge.is_machine_line("what does <tag> mean?") is False


# --- reset actions ------------------------------------------------------------

def test_reset_flips_awake_and_marks_reply(cortex_env):
    home, _ = cortex_env
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/x/y.jsonl"})
    d = _ws(home)
    assert d["awake"] is True
    assert d["user_replied_this_wake"] is True
    assert d["wake_log_id"] is None
    assert d["transcript"] == "/x/y.jsonl"
    assert "wait_count" not in d  # no wait_until -> untouched (never written)


def test_reset_clears_silence_and_sentinel(cortex_env, monkeypatch):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({
        "awake": True, "silence_wait_until": "2026-01-01T00:00:00+00:00",
        "tuck_pending": "2026-01-01T00:00:00+00:00", "sentinel_pid": 12345,
        "wait_count": 2,
    }))
    killed = []
    monkeypatch.setattr(cortex_bridge, "_kill_pid", lambda p: killed.append(p))
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/t.jsonl"})
    d = _ws(home)
    assert "silence_wait_until" not in d
    assert "tuck_pending" not in d
    assert "sentinel_pid" not in d
    # wait_until was already expired (2026-01-01) -> wait_count untouched.
    assert d["wait_count"] == 2
    assert d["user_replied_this_wake"] is True
    assert 12345 in killed  # sentinel SIGTERM'd


def test_reset_refunds_wait_count_on_live_wait_until(cortex_env):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({
        "awake": True, "silence_wait_until": "2099-01-01T00:00:00+00:00",
        "wait_count": 1,
    }))
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/t.jsonl"})
    d = _ws(home)
    assert "silence_wait_until" not in d
    assert d["wait_count"] == 0  # live wait interrupted -> refunded


def test_reset_no_refund_without_live_wait_until(cortex_env):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({
        "awake": True, "wait_count": 1,
    }))
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/t.jsonl"})
    d = _ws(home)
    assert "silence_wait_until" not in d
    assert d["wait_count"] == 1  # no live wait -> untouched


def test_reset_already_awake_preserves_awake_since(cortex_env):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({
        "awake": True, "awake_since": "2026-06-01T00:00:00+00:00",
        "wake_log_id": 99, "transcript": "/orig.jsonl",
    }))
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/new.jsonl"})
    d = _ws(home)
    # Already awake -> do not re-stamp awake_since / wake_log_id / transcript.
    assert d["awake_since"] == "2026-06-01T00:00:00+00:00"
    assert d["wake_log_id"] == 99
    assert d["transcript"] == "/orig.jsonl"
    assert d["user_replied_this_wake"] is True


def test_reset_clears_floor_deadline(cortex_env):
    home, db = cortex_env
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ct_pacemaker_state (id INTEGER PRIMARY KEY, "
                 "state TEXT, updated_at TEXT)")
    conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, '')",
        (json.dumps({"next_floor_due_at": "2026-07-11T05:00:00+10:00",
                     "last_wake_at": "x"}),))
    conn.commit()
    conn.close()
    cortex_bridge._cortex_user_wake_reset({})
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT state FROM ct_pacemaker_state WHERE id=1").fetchone()
    conn.close()
    obj = json.loads(row[0])
    assert obj["next_floor_due_at"] is None  # pending alarm cleared
    assert obj["last_wake_at"] == "x"  # other keys untouched


def test_reset_missing_table_no_crash(cortex_env):
    # No ct_pacemaker_state table -> _clear_floor_deadline swallows the error.
    cortex_bridge._cortex_user_wake_reset({})  # must not raise


def test_reset_noop_without_cortex_env(cortex_env, monkeypatch):
    home, _ = cortex_env
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/x.jsonl"})
    # Not a cortex session -> no wake_state written.
    assert not (home / "wake_state.json").exists()


def test_reset_spawns_watchdog_when_absent(cortex_env, monkeypatch):
    home, _ = cortex_env
    spawned = []
    monkeypatch.setattr(cortex_bridge, "_spawn_watchdog_if_absent",
                        lambda: spawned.append(1))
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/x.jsonl"})
    assert spawned == [1]


# --- wake_log_id backfill (Fix B) ---------------------------------------------

def test_reset_backfills_wake_log_id_from_open_row(cortex_env):
    home, db = cortex_env
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ct_wake_log (id INTEGER PRIMARY KEY, wake INTEGER)")
    conn.execute("INSERT INTO ct_wake_log (id, wake) VALUES (7, 0)")   # closed
    conn.execute("INSERT INTO ct_wake_log (id, wake) VALUES (8, 1)")   # open
    conn.commit()
    conn.close()
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/x.jsonl"})
    d = _ws(home)
    assert d["wake_log_id"] == 8  # latest open wake row


def test_reset_backfill_none_on_empty_table(cortex_env):
    home, db = cortex_env
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ct_wake_log (id INTEGER PRIMARY KEY, wake INTEGER)")
    conn.commit()
    conn.close()
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/x.jsonl"})
    d = _ws(home)
    assert d["wake_log_id"] is None  # empty table -> None, no raise


def test_latest_wake_log_id_missing_table(cortex_env):
    # No ct_wake_log table at all -> None, never raises.
    assert cortex_bridge._latest_wake_log_id() is None


# --- window-closed proxy lie_down (Fix E) -------------------------------------

def test_window_closed_runs_lie_down_when_awake(cortex_env, monkeypatch):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({
        "awake": True, "transcript": "/live.jsonl",
    }))
    calls = []
    monkeypatch.setattr(cortex_bridge.subprocess, "run",
                        lambda *a, **k: calls.append((a, k)))
    cortex_bridge.cortex_window_closed("/live.jsonl")
    assert len(calls) == 1
    cmd = calls[0][0][0]
    assert "cortex.lie_down" in cmd
    assert "--force-slept" in cmd and "auto" in cmd
    assert "--next-wake-min" in cmd


def test_window_closed_noop_when_not_awake(cortex_env, monkeypatch):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({"awake": False}))
    calls = []
    monkeypatch.setattr(cortex_bridge.subprocess, "run",
                        lambda *a, **k: calls.append(1))
    cortex_bridge.cortex_window_closed("/live.jsonl")
    assert calls == []  # idempotent no-op


def test_window_closed_skips_mismatched_transcript(cortex_env, monkeypatch):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({
        "awake": True, "transcript": "/other.jsonl",
    }))
    calls = []
    monkeypatch.setattr(cortex_bridge.subprocess, "run",
                        lambda *a, **k: calls.append(1))
    cortex_bridge.cortex_window_closed("/live.jsonl")
    assert calls == []  # different session's window -> skip


def test_window_closed_runs_when_no_transcript_recorded(cortex_env, monkeypatch):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({"awake": True}))
    calls = []
    monkeypatch.setattr(cortex_bridge.subprocess, "run",
                        lambda *a, **k: calls.append(1))
    cortex_bridge.cortex_window_closed("/live.jsonl")
    assert calls == [1]  # no transcript recorded -> allowed


def test_window_closed_noop_without_cortex_env(cortex_env, monkeypatch):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({"awake": True}))
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    calls = []
    monkeypatch.setattr(cortex_bridge.subprocess, "run",
                        lambda *a, **k: calls.append(1))
    cortex_bridge.cortex_window_closed("/live.jsonl")
    assert calls == []
