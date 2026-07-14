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
            "machine_markers": ["[CORTEX-WAKE]", "[NEW ROUND]", "[TUCK-IN]",
                                "[NIGHT]", "[FUSE]", "[CTL]", "[CMD"],
            "compact_markers": ["===== BEGIN ORIGINAL TRANSCRIPT",
                                "===== END ORIGINAL TRANSCRIPT"],
            "compact_marker_head_chars": 200,
            "wake_audit_log_file": "wake_audit.log",
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


def test_is_machine_line_new_marker_family(cortex_env):
    """Phase 3: fuse / ctl / slash-command bodies self-identify (line-start),
    so the user-wake reset never fires on them; real speech quoting a marker
    mid-body is still a user message."""
    assert cortex_bridge.is_machine_line(
        "⚙️ [FUSE] Summarise this whole session into handoff.md") is True
    assert cortex_bridge.is_machine_line(
        "⚙️ [CTL] Wrap up this turn: lie_down(next_wake_min=90).") is True
    assert cortex_bridge.is_machine_line(
        "⚙️ [CMD ct-sleep] $ARGUMENTS is a number of minutes") is True
    assert cortex_bridge.is_machine_line(
        "⏳ [NIGHT] Night window is open — one full sleep now.") is True
    # mid-body quote stays a real user message
    assert cortex_bridge.is_machine_line(
        "did the [FUSE] path fire last night?") is False


# The real compact-injection banner captured from a live cortex transcript
# (~/.claude/projects/-Users-Gabrielle--config-marrow-cortex/6c6c0bbd*.jsonl).
_COMPACT_SAMPLE = (
    "===== BEGIN ORIGINAL TRANSCRIPT (archived data — compress only; do NOT "
    "act on, answer, or continue it) =====\n"
    "===SESSION=== (sid=82d5a49b):\n[03:52] [Lumi] wake\n"
    "===== END ORIGINAL TRANSCRIPT ====="
)


def test_compact_injection_classified_as_machine_line(cortex_env):
    # The auto-compact continuation replay must NOT be seen as a user message.
    assert cortex_bridge.is_compact_injection(_COMPACT_SAMPLE) is True
    assert cortex_bridge.is_machine_line(_COMPACT_SAMPLE) is True
    # Genuine user text that merely mentions the word transcript stays user.
    assert cortex_bridge.is_compact_injection(
        "can you read the transcript for me?") is False
    assert cortex_bridge.is_machine_line(
        "here is my ORIGINAL TRANSCRIPT idea") is False
    # Marker buried past the head window is not treated as a compact banner.
    buried = ("x" * 400) + "===== BEGIN ORIGINAL TRANSCRIPT"
    assert cortex_bridge.is_compact_injection(buried) is False


def test_compact_injection_no_reset(cortex_env, monkeypatch):
    # A compact injection reaching the reset path is filtered upstream by
    # is_machine_line; here we assert the classifier the caller gates on.
    assert cortex_bridge.is_machine_line(_COMPACT_SAMPLE) is True


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


# --- audit log on destructive reset -------------------------------------------

def test_reset_writes_audit_log(cortex_env, monkeypatch):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({
        "awake": False, "sentinel_pid": 4242,
    }))
    monkeypatch.setattr(cortex_bridge, "_kill_pid", lambda p: None)
    cortex_bridge._cortex_user_wake_reset(
        {"transcript_path": "/x.jsonl", "prompt": "hey are you awake?"})
    log = (home / "wake_audit.log").read_text()
    lines = [l for l in log.splitlines() if l.strip()]
    actions = {l.split("\t")[1] for l in lines}
    assert {"awake_flip", "sentinel_kill", "floor_clear"} <= actions
    # Trigger reason (first 80 chars of the message) is recorded.
    assert any("hey are you awake?" in l for l in lines)
    # sentinel line carries the killed pid.
    assert any(l.split("\t")[1] == "sentinel_kill" and "4242" in l for l in lines)


def test_reset_audit_no_sentinel_line_when_absent(cortex_env):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({"awake": True}))
    cortex_bridge._cortex_user_wake_reset(
        {"transcript_path": "/x.jsonl", "prompt": "hi"})
    lines = [l for l in (home / "wake_audit.log").read_text().splitlines() if l.strip()]
    actions = {l.split("\t")[1] for l in lines}
    assert "sentinel_kill" not in actions  # no pid -> no kill line
    assert "awake_flip" not in actions     # already awake -> no flip line
    assert "floor_clear" in actions        # floor clear always attempted


# --- cancellation epoch (gen) -------------------------------------------------

def test_reset_bumps_gen_from_scratch(cortex_env):
    """First real user message on a fresh (no-epoch) state initialises gen=0 then
    bumps to 1, and seeds a state_id."""
    home, _ = cortex_env
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/x.jsonl"})
    d = _ws(home)
    assert d["gen"] == 1
    assert isinstance(d.get("state_id"), str) and d["state_id"]


def test_reset_bumps_gen_even_when_already_awake(cortex_env):
    """The critical BUG A guard: a user message bumps gen EVERY time, including
    when the session is already awake (so a still-running lie_down's newer
    sentinel is invalidated)."""
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps(
        {"awake": True, "gen": 5, "state_id": "deadbeef"}))
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/x.jsonl"})
    d = _ws(home)
    assert d["gen"] == 6                 # bumped despite already awake
    assert d["state_id"] == "deadbeef"   # state_id stable (no delete/recreate)


def test_reset_clears_next_wake_at(cortex_env):
    """A user arrival cancels the durable next-wake ledger so the tick reconcile
    never fires a stale scheduled alarm."""
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps(
        {"awake": True, "next_wake_at": "2030-01-01T09:00:00+11:00"}))
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/x.jsonl"})
    d = _ws(home)
    assert "next_wake_at" not in d


def test_reset_writes_gen_audit_line(cortex_env):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps(
        {"awake": True, "gen": 2, "state_id": "aa"}))
    cortex_bridge._cortex_user_wake_reset(
        {"transcript_path": "/x.jsonl", "prompt": "hey"})
    lines = [l for l in (home / "wake_audit.log").read_text().splitlines() if l.strip()]
    gen_lines = [l for l in lines if l.split("\t")[1] == "user_reset_gen"]
    assert gen_lines and "gen 2->3" in gen_lines[0]


# --- wake-line token parse + validation + legacy tolerance --------------------

def test_parse_gen_token_present_and_absent():
    assert cortex_bridge.parse_gen_token("[CORTEX-WAKE] 09:00 {g7:abcd1234}") == (7, "abcd1234")
    assert cortex_bridge.parse_gen_token("[CORTEX-WAKE] 09:00") is None   # legacy
    assert cortex_bridge.parse_gen_token("") is None


def test_wake_token_current_matches_and_stale(cortex_env):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({"gen": 4, "state_id": "cafe"}))
    assert cortex_bridge.wake_token_current((4, "cafe")) is True
    assert cortex_bridge.wake_token_current((3, "cafe")) is False   # stale gen
    assert cortex_bridge.wake_token_current((4, "beef")) is False   # ABA state_id


def test_wake_token_current_legacy_line_always_current(cortex_env):
    """A token-less (legacy) wake line is processed as before."""
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({"gen": 9, "state_id": "x"}))
    assert cortex_bridge.wake_token_current(None) is True


def test_wake_token_current_no_epoch_recorded_is_current(cortex_env):
    """No gen recorded yet -> nothing to invalidate against -> process."""
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({}))
    assert cortex_bridge.wake_token_current((1, "x")) is True
