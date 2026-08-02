"""Cross-session replay — stateless latest-window query + private marker.

Covers: seed on first call, nothing-new -> empty, marker advance, whole-turn
slash drop, drop_patterns, 2/4 caps from config, exclusion semantics (own sid
only by default, cortex.toml shell override), exclude_target_channels, idle
gate, enabled=false, the one-marker-per-sid contract shared by SessionStart and
turn_inject, and the marker-lock busy skip (no unlocked write).
"""
from __future__ import annotations

import fcntl
import io
import json
import os
from datetime import datetime, timedelta, timezone

from marrow import config, cortex_bridge, hooks, replay, storage

SID_SELF = "self1111-2222"
SID_OTHER = "othr9999-8888"
SID_CT = "ctsid0000"


def _fresh_db(tmp_path):
    p = str(tmp_path / "d.db")
    storage.init_db(p).close()
    return p


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _ev(db, sid, role, content, *, channel="cli", ts="2026-07-26T04:00:00Z"):
    conn = storage.connect(db)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO events(session_id, timestamp, role, content, channel)"
                " VALUES(?,?,?,?,?)", (sid, ts, role, content, channel))
        return cur.lastrowid
    finally:
        conn.close()


def _setup(monkeypatch, tmp_path, db, replay_extra=None):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: db)
    real = config.load

    def _patched():
        cfg = dict(real())
        rp = dict(cfg.get("replay", {}))
        rp["idle_gate_min"] = 0
        if replay_extra:
            rp.update(replay_extra)
        cfg["replay"] = rp
        return cfg

    monkeypatch.setattr(config, "load", _patched)


def _marker(key):
    return replay.load_marker(key)


# ── seed + no backfill beyond the latest window ─────────────────────────────

def test_first_call_renders_the_latest_window_and_seeds(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    for i in range(6):
        _ev(db, SID_OTHER, "user", f"q{i}")
        _ev(db, SID_OTHER, "assistant", f"a{i}")
    out = replay.context(SID_SELF, "cli")
    # 2 turns / 4 lines from config defaults — the OLDEST rounds never appear
    body = [ln for ln in out.splitlines() if ln.startswith("[")]
    assert len(body) == 4
    assert "q5" in out and "a5" in out and "q4" in out
    assert "q0" not in out and "q3" not in out
    assert _marker(SID_SELF) == 12


def test_second_call_with_no_new_rows_renders_nothing(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "hello there")
    _ev(db, SID_OTHER, "assistant", "hi back")
    assert replay.context(SID_SELF, "cli") != ""
    assert replay.context(SID_SELF, "cli") == ""
    assert replay.context(SID_SELF, "cli") == ""
    assert _marker(SID_SELF) == 2


def test_only_rows_newer_than_the_marker_render(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "first question")
    _ev(db, SID_OTHER, "assistant", "first answer")
    assert "first question" in replay.context(SID_SELF, "cli")
    _ev(db, SID_OTHER, "user", "second question")
    out = replay.context(SID_SELF, "cli")
    assert "second question" in out
    assert "first question" not in out
    assert _marker(SID_SELF) == 3


def test_missed_content_is_never_backfilled(tmp_path, monkeypatch):
    """A consumer away for 20 rounds sees the latest window only."""
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "seed row")
    replay.context(SID_SELF, "cli")
    for i in range(20):
        _ev(db, SID_OTHER, "user", f"missed{i}")
        _ev(db, SID_OTHER, "assistant", f"reply{i}")
    out = replay.context(SID_SELF, "cli")
    assert "missed19" in out and "reply19" in out
    assert "missed0" not in out and "missed17" not in out


def test_empty_db_writes_no_marker(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    assert replay.context(SID_SELF, "cli") == ""
    assert _marker(SID_SELF) is None


# ── caps come from config ───────────────────────────────────────────────────

def test_turn_and_line_caps_are_config_driven(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"max_turns": 1, "max_lines": 2})
    for i in range(3):
        _ev(db, SID_OTHER, "user", f"q{i}")
        _ev(db, SID_OTHER, "assistant", f"a{i}")
    out = replay.context(SID_SELF, "cli")
    body = [ln for ln in out.splitlines() if ln.startswith("[")]
    assert len(body) == 2
    assert "q2" in out and "a2" in out and "q1" not in out


def test_per_msg_chars_truncates(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"per_msg_chars": 10})
    _ev(db, SID_OTHER, "user", "x" * 50)
    assert "x" * 9 + "…" in replay.context(SID_SELF, "cli")


def test_disabled_renders_nothing_and_writes_no_marker(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"enabled": False})
    _ev(db, SID_OTHER, "user", "anything")
    assert replay.context(SID_SELF, "cli") == ""
    assert _marker(SID_SELF) is None


# ── noise filters ───────────────────────────────────────────────────────────

def test_slash_command_drops_the_whole_turn(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"max_turns": 5, "max_lines": 0})
    _ev(db, SID_OTHER, "user", "real question here")
    _ev(db, SID_OTHER, "assistant", "real answer here")
    _ev(db, SID_OTHER, "user", "/ct-duty")
    _ev(db, SID_OTHER, "assistant", "command output nobody needs")
    out = replay.context(SID_SELF, "cli")
    assert "real question here" in out and "real answer here" in out
    assert "/ct-duty" not in out
    assert "command output nobody needs" not in out
    # the dropped rows still moved the marker — they can only ever drop
    assert _marker(SID_SELF) == 4


def test_slash_lookalikes_survive(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"max_turns": 5, "max_lines": 0})
    _ev(db, SID_OTHER, "user", "/Users/Gabrielle/CC-Lab/marrow what is this")
    _ev(db, SID_OTHER, "user", "/clear " + "z" * 60)
    out = replay.context(SID_SELF, "cli")
    assert "/Users/Gabrielle" in out
    assert "zzz" in out


def test_drop_patterns_hide_matching_rows(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db,
           {"max_turns": 5, "max_lines": 0, "drop_patterns": ["BOTNOISE"]})
    _ev(db, SID_OTHER, "user", "keep this line")
    _ev(db, SID_OTHER, "assistant", "BOTNOISE status dump")
    out = replay.context(SID_SELF, "cli")
    assert "keep this line" in out and "BOTNOISE" not in out


def test_all_noise_batch_still_advances_the_marker(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "/info")
    assert replay.context(SID_SELF, "cli") == ""
    assert _marker(SID_SELF) == 1


def test_fold_line_counts_overflow_turns(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"max_turns": 2, "max_lines": 0})
    for i in range(4):
        _ev(db, SID_OTHER, "user", f"q{i}")
        _ev(db, SID_OTHER, "assistant", f"a{i}")
    out = replay.context(SID_SELF, "cli")
    assert out.splitlines()[-1] == "+2 earlier turns"


# ── exclusion semantics ─────────────────────────────────────────────────────

def test_own_sid_never_replays_to_itself(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _ev(db, SID_SELF, "user", "my own words")
    _ev(db, SID_SELF, "assistant", "my own reply")
    assert replay.context(SID_SELF, "cli") == ""


def test_plain_session_sees_every_source_channel(tmp_path, monkeypatch):
    """Default contract: the global latest window, own sid the only exclusion."""
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _ev(db, SID_CT, "assistant", "cortex self-talk", channel="ct")
    _ev(db, SID_OTHER, "user", "human words", channel="tg")
    out = replay.context(SID_SELF, "cli")
    assert "human words" in out and "cortex self-talk" in out


def test_exclude_target_channels_silences_a_destination(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"exclude_target_channels": ["wx"]})
    _ev(db, SID_OTHER, "user", "something happened")
    assert replay.context(SID_SELF, "wx") == ""
    assert replay.context(SID_SELF, "cli") != ""


def test_shell_exclude_defaults_are_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "db_path", lambda: str(tmp_path / "none.db"))
    monkeypatch.setattr(cortex_bridge, "_cortex_toml_section",
                        lambda *a, **k: None)
    assert replay.shell_exclude_channels("cli") == []
    assert replay.shell_exclude_channels("tg") == []
    assert replay.shell_exclude_channels("wx") == []  # unmapped -> unqualified


def test_shell_exclude_honours_the_cortex_toml_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "db_path", lambda: str(tmp_path / "none.db"))
    monkeypatch.setattr(cortex_bridge, "_cortex_toml_section",
                        lambda *a, **k: {"cli": ["ct"], "tg": ["tg"]})
    assert replay.shell_exclude_channels("cli") == ["ct"]
    assert replay.shell_exclude_channels("tg") == ["tg"]
    assert replay.shell_exclude_channels("wx") == []  # unmapped -> unqualified


def test_cortex_window_sees_ct_rows_and_ignores_the_idle_gate(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"idle_gate_min": 999})
    monkeypatch.setattr(cortex_bridge, "is_cortex_session", lambda t: True)
    monkeypatch.setattr(cortex_bridge, "_cortex_toml_section",
                        lambda *a, **k: None)
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _ev(db, SID_CT, "assistant", "other cortex chatter", channel="ct")
    _ev(db, SID_OTHER, "user", "someone else typed", channel="cli")
    _ev(db, SID_SELF, "user", "just now", ts=_iso(datetime.now(timezone.utc)))
    out = replay.context(SID_SELF, "cli", transcript_path="/t/x.jsonl")
    assert "someone else typed" in out and "other cortex chatter" in out


def test_cortex_shell_override_drops_that_shells_channel(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    monkeypatch.setattr(cortex_bridge, "is_cortex_session", lambda t: True)
    monkeypatch.setattr(cortex_bridge, "_cortex_toml_section",
                        lambda *a, **k: {"tg": ["tg"]})
    monkeypatch.setenv("MARROW_CORTEX", "tg")
    _ev(db, "tgsid111", "user", "telegram window talking", channel="tg")
    _ev(db, SID_CT, "assistant", "cli cortex talking", channel="ct")
    out = replay.context(SID_SELF, "tg", transcript_path="/t/x.jsonl")
    assert "cli cortex talking" in out and "telegram window talking" not in out


# ── idle gate (non-cortex only) ─────────────────────────────────────────────

def _age(db, minutes):
    _ev(db, SID_SELF, "user", "her earlier turn",
        ts=_iso(datetime.now(timezone.utc) - timedelta(minutes=minutes)))


def test_idle_gate_holds_a_busy_session(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"idle_gate_min": 20})
    _ev(db, SID_OTHER, "user", "other session news")
    _age(db, 19)
    assert replay.context(SID_SELF, "cli") == ""
    assert _marker(SID_SELF) is None


def test_idle_gate_releases_and_shows_the_latest_window(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"idle_gate_min": 20})
    _ev(db, SID_OTHER, "user", "other session news")
    _age(db, 21)
    assert "other session news" in replay.context(SID_SELF, "cli")


def test_first_turn_is_never_gated(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"idle_gate_min": 20})
    _ev(db, SID_OTHER, "user", "other session news")
    assert "other session news" in replay.context(SID_SELF, "cli")


# ── marker files ────────────────────────────────────────────────────────────

def test_busy_marker_lock_skips_the_round_without_writing(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "content nobody may consume")
    lock = replay.marker_path(SID_SELF)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert replay.context(SID_SELF, "cli") == ""
        assert _marker(SID_SELF) is None
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # lock released -> the same content is still the latest window
    assert "content nobody may consume" in replay.context(SID_SELF, "cli")


def test_markers_are_private_per_consumer(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "shared news")
    assert "shared news" in replay.context(SID_SELF, "cli")
    assert "shared news" in replay.context("other-consumer", "cli")
    assert _marker(SID_SELF) == 1 and _marker("other-consumer") == 1


# ── hook outlets ────────────────────────────────────────────────────────────

def _run_hook(monkeypatch, fn, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    fn()
    return out.getvalue()


def test_turn_inject_is_the_only_outlet_on_a_wake_turn(tmp_path, monkeypatch):
    """A wake turn is an ordinary user-prompt turn: the note carries no replay,
    so turn_inject renders it exactly once and never again."""
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    monkeypatch.setattr(cortex_bridge, "is_cortex_session", lambda t: True)
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _ev(db, SID_OTHER, "user", "news from elsewhere")
    payload = {"session_id": SID_SELF, "transcript_path": "/t/x.jsonl",
               "prompt": "☀️ 21:47"}
    first = _run_hook(monkeypatch, hooks.turn_inject, payload)
    assert first.count("news from elsewhere") == 1
    second = _run_hook(monkeypatch, hooks.turn_inject, payload)
    assert "news from elsewhere" not in second


def test_session_start_seed_and_turn_inject_share_one_marker(tmp_path, monkeypatch):
    """SessionStart (lifecycle.py calls replay.context with the same sid) and
    turn_inject key the same marker file, so seeded content never re-injects."""
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    monkeypatch.setattr(cortex_bridge, "is_cortex_session", lambda t: True)
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _ev(db, SID_OTHER, "user", "news from elsewhere")
    seed = replay.context(SID_SELF, "cli", transcript_path="/t/x.jsonl")
    assert "news from elsewhere" in seed
    assert _marker(SID_SELF) == 1
    out = _run_hook(monkeypatch, hooks.turn_inject,
                    {"session_id": SID_SELF, "transcript_path": "/t/x.jsonl",
                     "prompt": "hello"})
    assert "news from elsewhere" not in out
    _ev(db, SID_OTHER, "assistant", "brand new line")
    out2 = _run_hook(monkeypatch, hooks.turn_inject,
                     {"session_id": SID_SELF, "transcript_path": "/t/x.jsonl",
                      "prompt": "hello again"})
    assert out2.count("brand new line") == 1
    assert _marker(SID_SELF) == 2


def test_wakeup_note_text_falls_back_to_this_shells_section(tmp_path, monkeypatch):
    """Render failure -> the frozen file, sliced to the CALLER's own section
    (heading stripped); another shell's section is never returned."""
    monkeypatch.setattr(cortex_bridge, "_render_note_fresh", lambda t, s=None: None)
    note = tmp_path / "wakeup_note.md"
    note.write_text("## cli · sid=aaaaaaaa\nfrozen note body\n\n"
                    "## tg · sid=bbbbbbbb\nother shell body\n")
    monkeypatch.setattr(cortex_bridge, "_cortex_path", lambda *a, **k: note)
    monkeypatch.setenv("MARROW_CORTEX", "1")
    assert cortex_bridge.wakeup_note_text("/t/x.jsonl") == "frozen note body"
    monkeypatch.setenv("MARROW_CORTEX", "tg")
    assert cortex_bridge.wakeup_note_text("/t/x.jsonl") == "other shell body"


def test_wakeup_note_text_none_when_this_shell_has_no_section(tmp_path, monkeypatch):
    monkeypatch.setattr(cortex_bridge, "_render_note_fresh", lambda t, s=None: None)
    note = tmp_path / "wakeup_note.md"
    note.write_text("## cli\nonly the cli section here\n")
    monkeypatch.setattr(cortex_bridge, "_cortex_path", lambda *a, **k: note)
    monkeypatch.setenv("MARROW_CORTEX", "tg")
    assert cortex_bridge.wakeup_note_text("/t/x.jsonl") is None
