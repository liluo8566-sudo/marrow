"""User-wake reset (Item 3): a real user message in a cortex window flips the
session awake, marks the reply, resets the free-round silence cycle (drops any
pending kick carrier + the last-injection marker), kills the pending
sentinel, (re)spawns a watchdog and kicks the wake daemon. Machine lines
(wake marker / tuck-in) must NOT trigger it.

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
            "tuck_in_marker": "[TUCK-IN]",
            "machine_markers": ["[NEW ROUND]", "[TUCK-IN]",
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
    assert cortex_bridge.is_machine_line(
        "⏳ [TUCK-IN] It's been 20 mins — choose again") is True
    assert cortex_bridge.is_machine_line(
        "<task-notification>background shell exited</task-notification>") is True
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


# --- wake bell: receipt sidecar / shape recognition ---------------------------

def _put_receipt(home, text="☀️ 09:05", gen=3, state_id="cafe", rearm=False,
                 ts=None, template_prefix="☀️ "):
    from datetime import datetime, timezone
    ws = _ws(home) if (home / "wake_state.json").exists() else {}
    ws["wake_receipt"] = {
        "text": text, "gen": gen, "state_id": state_id, "rearm": rearm,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "template_prefix": template_prefix,
    }
    (home / "wake_state.json").write_text(json.dumps(ws))


def test_match_wake_bell_receipt_exact_hit(cortex_env):
    home, _ = cortex_env
    _put_receipt(home, text="☀️ 09:05", gen=7, state_id="beef")
    kind, tok, degraded = cortex_bridge.match_wake_bell("☀️ 09:05")
    assert kind == "receipt" and tok == (7, "beef") and degraded is False
    # exact-match only: a user line merely quoting the bell text is NOT a receipt.
    assert cortex_bridge.match_wake_bell("did ☀️ 09:05 fire?") is None


def test_match_wake_bell_receipt_wrapped_envelope(cortex_env):
    home, _ = cortex_env
    _put_receipt(home, text="☀️ 09:05")
    wrapped = ("<task-notification>\n<event>☀️ 09:05</event>\n"
               "</task-notification>")
    kind, _, _ = cortex_bridge.match_wake_bell(wrapped)
    assert kind == "receipt"


def test_match_wake_bell_shape_fallback_no_receipt(cortex_env):
    home, _ = cortex_env
    # No receipt on disk -> shape fallback (fail-open), degraded=True, token None.
    kind, tok, degraded = cortex_bridge.match_wake_bell("☀️ 09:05")
    assert kind == "shape" and tok is None and degraded is True


def test_match_wake_bell_legacy_marker_falls_through(cortex_env):
    # Legacy '[CORTEX-WAKE] … {g..}' recognition removed post-migration (5f7efe7
    # human-text bell + receipt sidecar). No receipt + no shape match -> not a
    # bell -> falls through to normal user-prompt handling.
    assert cortex_bridge.match_wake_bell("[CORTEX-WAKE] 09:00 {g5:abcd}") is None
    assert cortex_bridge.is_machine_line("[CORTEX-WAKE] 09:00 {g5:abcd}") is False


def test_match_wake_bell_ttl_expiry(cortex_env):
    home, _ = cortex_env
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    _put_receipt(home, text="☀️ 09:05", ts=old)
    # Receipt expired past receipt_ttl_min (default 15) -> not a receipt hit;
    # the on-screen line still matches the shape -> degraded fallback.
    kind, tok, degraded = cortex_bridge.match_wake_bell("☀️ 09:05")
    assert kind == "shape" and degraded is True


def test_match_wake_bell_user_text_equal_to_prefix_no_receipt(cortex_env):
    """Documented residue: with NO receipt, an ordinary user line that merely
    OPENS with the template prefix (not the exact '<prefix> HH:MM' shape) is NOT
    a bell -> falls through to the user-wake reset. Only the near-exact bell
    shape is swallowed (accepted fail-open degraded case)."""
    assert cortex_bridge.match_wake_bell("☀️ 早安呀") is None
    assert cortex_bridge.match_wake_bell("☀️ good morning") is None
    # The near-exact shape IS swallowed (the accepted residue).
    assert cortex_bridge.match_wake_bell("☀️ 09:05")[0] == "shape"


def test_match_wake_bell_stale_epoch_suppressed_via_receipt(cortex_env, monkeypatch):
    home, _ = cortex_env
    _put_receipt(home, text="☀️ 09:05", gen=2, state_id="dd")
    # Live epoch moved on -> the receipt token is stale.
    monkeypatch.setattr(cortex_bridge, "wake_token_current", lambda tok: False)
    kind, tok, _ = cortex_bridge.match_wake_bell("☀️ 09:05")
    assert kind == "receipt" and tok == (2, "dd")
    assert cortex_bridge.wake_token_current(tok) is False


def test_is_machine_line_new_bell_shape(cortex_env):
    home, _ = cortex_env
    _put_receipt(home, text="☀️ 09:05", gen=1, state_id="ab")
    assert cortex_bridge.is_machine_line("☀️ 09:05") is True
    # A real user message that just opens with the emoji is NOT machine.
    assert cortex_bridge.is_machine_line("☀️ 早安") is False


_ZWJ_STATIC = "[🧚‍♀️ 笨鸭换岗成功]"  # bracketed multi-codepoint ZWJ emoji + CJK, static


def test_match_wake_bell_static_zwj_receipt_exact(cortex_env):
    """A fully STATIC template (no {hm}) with a ZWJ emoji round-trips through the
    receipt exact-match — byte-for-byte, multi-codepoint intact."""
    home, _ = cortex_env
    _put_receipt(home, text=_ZWJ_STATIC, gen=8, state_id="feed",
                 template_prefix=_ZWJ_STATIC)
    # tag the receipt template as static (no {hm})
    ws = _ws(home); ws["wake_receipt"]["template"] = _ZWJ_STATIC
    (home / "wake_state.json").write_text(json.dumps(ws))
    kind, tok, degraded = cortex_bridge.match_wake_bell(_ZWJ_STATIC)
    assert kind == "receipt" and tok == (8, "feed") and degraded is False


def test_match_wake_bell_static_zwj_shape_fallback(cortex_env, monkeypatch):
    """Static template, no receipt -> shape fallback = EXACT match of the static
    text (no time appended). A user line merely containing it is not a bell."""
    monkeypatch.setattr(cortex_bridge, "wake_bell_template",
                        lambda cfg=None: _ZWJ_STATIC)
    kind, tok, degraded = cortex_bridge.match_wake_bell(_ZWJ_STATIC)
    assert kind == "shape" and tok is None and degraded is True
    # not the exact static text -> not a bell (falls through to user reset)
    assert cortex_bridge.match_wake_bell(f"是 {_ZWJ_STATIC} 吗？") is None
    assert cortex_bridge.match_wake_bell("🧚‍♀️ 早安") is None


def test_match_wake_bell_bracketed_hm_shape_fallback(cortex_env, monkeypatch):
    """A bracketed {hm} template '[<prefix> HH:MM]' matches the full bracketed
    on-screen line via shape fallback — the suffix after {hm} (the ']') is
    honored, not dropped (regression: hardcoded '$' after the time missed it)."""
    # Both producer shapes are shape-fallback candidates -> pin both, else the
    # other template's default would match on its own.
    monkeypatch.setattr(cortex_bridge, "wake_bell_template",
                        lambda cfg=None: "[☀️ {hm}]")
    monkeypatch.setattr(cortex_bridge, "spawn_opener_template",
                        lambda cfg=None: "[☀️ {hm}]")
    kind, tok, degraded = cortex_bridge.match_wake_bell("[☀️ 09:05]")
    assert kind == "shape" and tok is None and degraded is True
    # missing the closing bracket -> not the bell shape
    assert cortex_bridge.match_wake_bell("☀️ 09:05") is None


def test_match_wake_bell_shape_fallback_covers_both_templates(cortex_env, monkeypatch):
    """With no receipt, BOTH producer shapes are recognized: the resident bell
    AND the fresh-spawn opener (they are separate config keys now)."""
    monkeypatch.setattr(cortex_bridge, "wake_bell_template",
                        lambda cfg=None: "⏰ {hm} 醒了")
    monkeypatch.setattr(cortex_bridge, "spawn_opener_template",
                        lambda cfg=None: "[🧚 shift change]")
    assert cortex_bridge.match_wake_bell("⏰ 09:05 醒了")[0] == "shape"
    assert cortex_bridge.match_wake_bell("[🧚 shift change]")[0] == "shape"
    # neither shape -> ordinary user speech
    assert cortex_bridge.match_wake_bell("⏰ 早安") is None


def test_is_machine_line_bracketed_bell(cortex_env):
    """The bracketed static bell classifies as a machine line via the receipt
    exact match; a real user line merely quoting it stays user speech."""
    home, _ = cortex_env
    _put_receipt(home, text=_ZWJ_STATIC, gen=5, state_id="dead",
                 template_prefix=_ZWJ_STATIC)
    ws = _ws(home); ws["wake_receipt"]["template"] = _ZWJ_STATIC
    (home / "wake_state.json").write_text(json.dumps(ws))
    assert cortex_bridge.is_machine_line(_ZWJ_STATIC) is True
    assert cortex_bridge.is_machine_line(f"是 {_ZWJ_STATIC} 吗？") is False


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


def test_reset_logs_user_wake_row(cortex_env):
    """BUG A (marrow side): a user-triggered wake writes its OWN wake=1 row
    tagged 'user' (force_slept NULL) and binds it as wake_log_id, so the wakeup
    note's 'Last wake' counts the user wake instead of a stale scheduled row."""
    home, db = cortex_env
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE ct_wake_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, "
        "wake INTEGER, dry_run INTEGER, reasons TEXT, force_slept TEXT, "
        "shell TEXT NOT NULL DEFAULT 'cli')")
    conn.commit()
    conn.close()

    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/x/y.jsonl"})

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, reasons, force_slept, shell FROM ct_wake_log "
        "WHERE wake=1").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][1] == "user"
    assert rows[0][2] is None  # force_slept NULL -> auto-rate stats unaffected
    assert rows[0][3] == "cli"  # stamped explicitly, not left to the DEFAULT
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


def test_reset_clears_silence_cycle_and_sentinel(cortex_env, monkeypatch):
    """A user arrival resets the free-round silence cycle: drops any pending
    kick carrier + the last-injection marker, kills the sentinel."""
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({
        "awake": True, "kick_round": True,
        "tuck_pending": "2026-01-01T00:00:00+00:00", "sentinel_pid": 12345,
    }))
    killed = []
    monkeypatch.setattr(cortex_bridge, "_kill_pid", lambda p: killed.append(p))
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/t.jsonl"})
    d = _ws(home)
    assert "kick_round" not in d
    assert "tuck_pending" not in d
    assert "sentinel_pid" not in d
    assert d["user_replied_this_wake"] is True
    assert 12345 in killed  # sentinel SIGTERM'd


def test_reset_stamps_last_user_msg_ts(cortex_env):
    """FIX 3 presence gate source: a real user turn stamps last_user_msg_ts so
    the occupancy nudge can hold while the user is active. A stale prior stamp is
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


def test_reset_kicks_the_daemon_socket(cortex_env, monkeypatch):
    """After the state write the reset sends one `<shell>\\n` kick to the
    daemon socket — notification only, on top of every existing step."""
    sent = []
    monkeypatch.setattr(cortex_bridge, "_daemon_socket_path",
                        lambda: cortex_env[0] / "state" / "cortex-daemon.sock")

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            sent.append(("timeout", t))

        def connect(self, path):
            sent.append(("connect", path))

        def sendall(self, data):
            sent.append(("send", data))

    import socket as _s
    monkeypatch.setattr(_s, "socket", lambda *a, **k: _Sock())
    cortex_bridge._cortex_user_wake_reset({})
    assert ("connect", str(cortex_env[0] / "state" / "cortex-daemon.sock")) in sent
    assert ("send", b"cli\n") in sent
    # State write still happened — the kick is additive, not a replacement.
    assert _ws(cortex_env[0])["awake"] is True


def test_reset_survives_a_missing_daemon_socket(cortex_env):
    """No daemon listening -> silent no-op, reset completes normally."""
    cortex_bridge._cortex_user_wake_reset({})  # must not raise
    assert _ws(cortex_env[0])["awake"] is True


def test_daemon_socket_path_defaults_under_cortex_home(cortex_env):
    home, _ = cortex_env
    assert cortex_bridge._daemon_socket_path() == home / "state" / "cortex-daemon.sock"


def test_reset_missing_table_no_crash(cortex_env):
    # No ct_pacemaker_state table anywhere -> the reset no longer touches it.
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
    assert {"awake_flip", "sentinel_kill"} <= actions
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
    assert "user_reset_gen" in actions     # epoch bump always audited


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


# --- wake-line token validation -----------------------------------------------

def test_wake_token_current_matches_and_stale(cortex_env):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({"gen": 4, "state_id": "cafe"}))
    assert cortex_bridge.wake_token_current((4, "cafe")) is True
    assert cortex_bridge.wake_token_current((3, "cafe")) is False   # stale gen
    assert cortex_bridge.wake_token_current((4, "beef")) is False   # ABA state_id


def test_wake_token_current_tokenless_always_current(cortex_env):
    """A token-less wake (receipt without gen/state_id) is processed as before."""
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({"gen": 9, "state_id": "x"}))
    assert cortex_bridge.wake_token_current(None) is True


def test_wake_token_current_no_epoch_recorded_is_current(cortex_env):
    """No gen recorded yet -> nothing to invalidate against -> process."""
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({}))
    assert cortex_bridge.wake_token_current((1, "x")) is True


# --- shell scoping: a non-cli shell must not write the cli ledger --------------

@pytest.fixture()
def tg_shell(cortex_env, tmp_path, monkeypatch):
    """cortex_env re-pointed at the tg shell: [cortex].shells lists tg and the
    per-shell ledger lives under tmp."""
    home, db = cortex_env
    base = config.load()
    shells_dir = tmp_path / "shells"
    shells_dir.mkdir()
    cx = {**base["cortex"], "shells": ["cli", "tg"],
          "shell_state_dir": str(shells_dir)}
    monkeypatch.setattr(config, "load", lambda: {**base, "cortex": cx})
    monkeypatch.setenv("MARROW_CORTEX", "tg")
    return home, shells_dir


def test_tg_turn_never_touches_cli_wake_state(tg_shell, monkeypatch):
    """Root cause of the mid-sleep respawn: a tg-shell user message ran the cli
    user-wake reset, which cleared wake_state.json's next_wake_at and flipped it
    awake — cortex reconcile then read the deliberately closed cli window as an
    "accidental close of awake window" and respawned it long before the booked
    wake. The tg host cancels its OWN booked wake
    (synapse_tg.shell.on_user_message), so marrow must leave the cli ledger
    byte-for-byte alone."""
    home, _ = tg_shell
    seed = {"awake": False, "gen": 7, "state_id": "abc", "session_id": "S1",
            "next_wake_at": "2026-07-26T20:02:00+10:00"}
    (home / "wake_state.json").write_text(json.dumps(seed))
    spawned = []
    monkeypatch.setattr(cortex_bridge, "_spawn_watchdog_if_absent",
                        lambda: spawned.append(1))
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/tg/t.jsonl",
                                           "prompt": "醒着么"})
    assert _ws(home) == seed   # alarm + epoch + awake untouched
    assert spawned == []       # and no cli watchdog started from a tg turn


def test_cli_turn_still_clears_the_booked_alarm(cortex_env):
    """The cli shell keeps the original behaviour: its own user message cancels
    the pending alarm and bumps the cancellation epoch."""
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({
        "awake": False, "gen": 3, "state_id": "z",
        "next_wake_at": "2026-07-26T20:02:00+10:00"}))
    cortex_bridge._cortex_user_wake_reset({"transcript_path": "/t.jsonl",
                                           "prompt": "hi"})
    d = _ws(home)
    assert "next_wake_at" not in d
    assert d["awake"] is True
    assert d["gen"] == 4


def test_presence_state_cli_reads_wake_state(cortex_env):
    home, _ = cortex_env
    (home / "wake_state.json").write_text(json.dumps({
        "last_user_msg_ts": "2026-07-26T09:00:00+00:00",
        "transcript": "/cli/c.jsonl"}))
    ws = cortex_bridge._shell_presence_state()
    assert ws["last_user_msg_ts"] == "2026-07-26T09:00:00+00:00"
    assert ws["transcript"] == "/cli/c.jsonl"


def test_presence_state_tg_reads_its_own_ledger(tg_shell):
    """The occupancy nudge's presence gate + handoff header must judge a tg window off
    the tg ledger (host-written last_user_ts / session_id), never off cli's."""
    home, shells_dir = tg_shell
    (home / "wake_state.json").write_text(json.dumps({
        "last_user_msg_ts": "2020-01-01T00:00:00+00:00",
        "transcript": "/cli/c.jsonl"}))
    (shells_dir / "tg.json").write_text(json.dumps({
        "last_user_ts": "2026-07-26T09:46:49+00:00",
        "session_id": "d994b51d-bc04-48ea-a8d7-d72274e28bb3"}))
    ws = cortex_bridge._shell_presence_state()
    assert ws["last_user_msg_ts"] == "2026-07-26T09:46:49+00:00"
    assert ws["transcript"] == "d994b51d-bc04-48ea-a8d7-d72274e28bb3.jsonl"
    assert cortex_bridge._user_active_within(ws, 15) is False   # stamp is old
