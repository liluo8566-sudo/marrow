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
import os
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


def test_is_machine_line_wake_tuck_marker_line_start_only(cortex_env):
    """P2-2: the always-covered wake / tuck markers now line-start match, not
    substring. A real user prompt quoting them mid-sentence stays user speech
    (so the user-wake reset + downstream hook processing still fire)."""
    # substring quote mid-body -> user message (was wrongly machine before)
    assert cortex_bridge.is_machine_line(
        "did the [NEW ROUND] path fire?") is False
    assert cortex_bridge.is_machine_line(
        "why is [TUCK-IN] showing twice in the log?") is False
    assert cortex_bridge.is_machine_line(
        "grep for [CORTEX-WAKE] in wake_signal.log") is False
    # genuine machine block: note line(s) above, tuck marker as the final line
    # (env's tuck_in_marker is [TUCK-IN]) — line-start on a non-first line.
    assert cortex_bridge.is_machine_line(
        "Budget: 40k. Pending: 2.\n"
        "⏳ [TUCK-IN] 15 min since 念念's last message. Choose again.") is True
    assert cortex_bridge.is_machine_line("⏳ [TUCK-IN] It's been 20 mins") is True


def test_is_machine_line_cjk_lead_not_machine(cortex_env):
    """P2-1 (bridge side): the narrowed decoration class excludes CJK/kana/hangul.
    A Chinese message opening with a real word then a marker is NOT machine —
    previously 「看」 was stripped and the line-start became [FUSE]."""
    assert cortex_bridge.is_machine_line("看 [FUSE] path fired?") is False
    assert cortex_bridge.is_machine_line(
        "查一下 [NEW ROUND] 是不是误判") is False
    # both-direction: the emoji-prefixed real machine line still classifies
    assert cortex_bridge.is_machine_line("⚙️ [CMD ct-sleep] 90") is True


def test_line_starts_with_marker_helper(cortex_env):
    """The shared shape check the hook's tuck-in de-dup guard rides."""
    tm = "[NEW ROUND]"
    assert cortex_bridge.line_starts_with_marker(
        "note above\n⏳ [NEW ROUND] 15 min since ...", tm) is True
    assert cortex_bridge.line_starts_with_marker(
        "did the [NEW ROUND] path fire?", tm) is False
    assert cortex_bridge.line_starts_with_marker("看 [NEW ROUND] ?", tm) is False


# The REAL ear-Monitor delivery envelope (07-14 incident): the free-round block
# arrives WRAPPED — marker line reads `<event>⏳ [NEW ROUND] …`, not raw line
# start. Verbatim shape copied from the incident; the guards must still classify
# it as a machine/tuck line so the hook does not double-inject the full note.
_WRAPPED_NEW_ROUND = (
    "<task-notification>\n"
    "<summary>Monitor event: \"cortex wake signal\"</summary>\n"
    "<event>⏳ [NEW ROUND] 87 min since the user's last message. Choose again: "
    "1) play around (playbook); 2) wait(N); 3) lie_down(next_wake_min=N). "
    "NOTE: Call MCP tool to render the wakeup note.</event>\n"
    "</task-notification>"
)


def test_line_starts_with_marker_wrapped_envelope(cortex_env):
    """P2.5/FIX2: the wrapped ear-Monitor envelope (`<event>⏳ [NEW ROUND] …`)
    still counts as a tuck line — the hook's de-dup guard rides this shape."""
    tm = "[NEW ROUND]"
    assert cortex_bridge.line_starts_with_marker(_WRAPPED_NEW_ROUND, tm) is True
    # a marker quoted inside real prose must still NOT match
    assert cortex_bridge.line_starts_with_marker(
        "he asked <why> the [NEW ROUND] path fired", tm) is False


def test_is_machine_line_wrapped_envelope(cortex_env):
    """Both directions: the wrapped free-round envelope is machine; a real
    user message quoting the marker mid-body stays a user message."""
    assert cortex_bridge.is_machine_line(_WRAPPED_NEW_ROUND) is True
    assert cortex_bridge.is_machine_line(
        "did the [NEW ROUND] path fire after the <event> block?") is False


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
    # No ct_wake_log table in this fixture db -> the activation-row insert falls
    # back to None (best-effort, never blocks the wake). The db write now runs
    # AFTER the wake_state lock releases (P2 fix), so a failed write leaves the
    # key simply absent rather than explicitly None — .get() reads both the same.
    assert d.get("wake_log_id") is None
    assert d["transcript"] == "/x/y.jsonl"
    assert "wait_count" not in d  # no wait_until -> untouched (never written)


def test_reset_logs_user_wake_row(cortex_env):
    """BUG A (marrow side): a user-triggered wake writes its OWN wake=1 row
    tagged 'user' (force_slept NULL) and binds it as wake_log_id, so the wakeup
    note's 'Last wake' counts the user wake instead of a stale scheduled row."""
    home, db = cortex_env
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE ct_wake_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, "
        "wake INTEGER, dry_run INTEGER, reasons TEXT, force_slept TEXT)")
    conn.commit()
    conn.close()

    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/x/y.jsonl"})

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, reasons, force_slept FROM ct_wake_log WHERE wake=1").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][1] == "user"
    assert rows[0][2] is None  # force_slept NULL -> auto-rate stats unaffected
    assert _ws(home)["wake_log_id"] == rows[0][0]  # bound to the fresh row


def test_reset_db_write_does_not_hold_wake_state_lock(cortex_env, monkeypatch):
    """P2 fix: the ct_wake_log write (_log_user_wake_row) must run OUTSIDE the
    wake_state flock. cortex's own strict lock on the SAME file gives up after
    5s; a slow/contended db write held under this lock would stall the user's
    prompt and starve a concurrent cortex-side mutation. Proven by having the
    stubbed db-write function itself try to acquire the wake_state lock file
    (non-blocking) — it must succeed, meaning the outer lock was already
    released before this call runs."""
    import fcntl
    home, _ = cortex_env
    lock_path = (home / "wake_state.json").with_suffix(".lock")
    acquired = {}

    def fake_log_user_wake_row():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired["ok"] = True
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            acquired["ok"] = False
        finally:
            os.close(fd)
        return 42

    monkeypatch.setattr(cortex_bridge, "_log_user_wake_row", fake_log_user_wake_row)
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/x/y.jsonl"})

    assert acquired.get("ok") is True  # the outer wake_state lock was released
    assert _ws(home)["wake_log_id"] == 42  # patch-back still binds the id


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


def test_reset_stamps_last_user_msg_ts(cortex_env):
    """FIX 3 presence gate source: a real user turn stamps last_user_msg_ts so
    the 120k nudge can hold while the user is active. A stale prior stamp is
    refreshed."""
    from datetime import datetime, timezone
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({
        "awake": True, "last_user_msg_ts": "2020-01-01T00:00:00+00:00",
    }))
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/t.jsonl",
                                           "prompt": "hey are you there?"})
    d = _ws(home)
    ts = datetime.fromisoformat(d["last_user_msg_ts"])
    assert (datetime.now(timezone.utc) - ts).total_seconds() < 60
    # helper agrees the user is active within 15 min
    assert cortex_bridge._user_active_within(d, 15) is True


def test_machine_turn_does_not_refresh_user_ts(cortex_env):
    """Trap: machine/injected turns must never count as user presence. The hook
    gates _cortex_user_wake_reset behind is_machine_line, so a machine line never
    reaches the writer and the stale ts is preserved (helper reports inactive)."""
    home, _ = cortex_env
    stale = "2020-01-01T00:00:00+00:00"
    (home / "wake_state.json").write_text(json.dumps({
        "awake": True, "last_user_msg_ts": stale,
    }))
    wrapped = ("<task-notification>\n<event>⏳ [NEW ROUND] 87 min since the "
               "user's last message.</event>\n</task-notification>")
    # a machine line is classified as such -> the hook skips the reset writer
    assert cortex_bridge.is_machine_line(wrapped) is True
    d = _ws(home)
    assert d["last_user_msg_ts"] == stale  # never refreshed
    assert cortex_bridge._user_active_within(d, 15) is False


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
    assert d.get("wake_log_id") is None  # empty table -> None, no raise


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
