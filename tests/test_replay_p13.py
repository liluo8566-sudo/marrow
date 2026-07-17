"""P13 — replay full-coverage for cortex + idle gate for others.

Covers: cortex normal-turn replay via the shared note.py last_note_ts cursor
(no double-feed hook<->note), ct destination now delivered on a normal turn,
idle-gate boundary (19min gated / 21min injects), first-turn injects, gated turn
holds the per-sid cursor, other excluded channels still excluded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marrow import config, cortex_bridge, hooks, storage

SID_SELF = "self1111-2222"
SID_OTHER = "othr9999-8888"


def _fresh_db(tmp_path):
    p = str(tmp_path / "d.db")
    storage.init_db(p).close()
    return p


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _ev(db, sid, role, content, *, channel="cli", ts=None):
    if ts is None:
        ts = "2026-07-17T04:00:00Z"
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
        if replay_extra:
            rp.update(replay_extra)
        cfg["replay"] = rp
        return cfg

    monkeypatch.setattr(config, "load", _patched)


def _cursor(sid):
    return hooks._load_replay_cursor(sid)


# ── idle gate boundary ──────────────────────────────────────────────────────

def _seed_and_new(db, tmp_path, monkeypatch, extra=None):
    _setup(monkeypatch, tmp_path, db, extra)
    hooks._replay_context(SID_SELF, "cli")  # first sight seeds cursor
    _ev(db, SID_OTHER, "user", "fresh from another session",
        ts="2026-07-17T04:10:00Z")


def test_idle_gate_gated_below_threshold(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _seed_and_new(db, tmp_path, monkeypatch, {"idle_gate_min": 20})
    cur_before = _cursor(SID_SELF)
    # her last completed turn in THIS sid was 19 min ago -> gated
    _ev(db, SID_SELF, "user", "recent turn",
        ts=_iso(datetime.now(timezone.utc) - timedelta(minutes=19)))
    assert hooks._replay_context(SID_SELF, "cli") == ""
    # cursor held while gated -> nothing lost
    assert _cursor(SID_SELF) == cur_before


def test_idle_gate_injects_above_threshold(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _seed_and_new(db, tmp_path, monkeypatch, {"idle_gate_min": 20})
    _ev(db, SID_SELF, "user", "older turn",
        ts=_iso(datetime.now(timezone.utc) - timedelta(minutes=21)))
    out = hooks._replay_context(SID_SELF, "cli")
    assert "fresh from another session" in out


def test_idle_gate_first_turn_injects(tmp_path, monkeypatch):
    # no prior user event for this sid = first turn = idle satisfied
    db = _fresh_db(tmp_path)
    _seed_and_new(db, tmp_path, monkeypatch, {"idle_gate_min": 20})
    out = hooks._replay_context(SID_SELF, "cli")
    assert "fresh from another session" in out


def test_idle_gate_zero_disables(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _seed_and_new(db, tmp_path, monkeypatch, {"idle_gate_min": 0})
    _ev(db, SID_SELF, "user", "just now",
        ts=_iso(datetime.now(timezone.utc) - timedelta(minutes=1)))
    out = hooks._replay_context(SID_SELF, "cli")
    assert "fresh from another session" in out


def test_gated_turn_holds_cursor_then_releases(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _seed_and_new(db, tmp_path, monkeypatch, {"idle_gate_min": 20})
    cur_seed = _cursor(SID_SELF)
    _ev(db, SID_SELF, "user", "recent",
        ts=_iso(datetime.now(timezone.utc) - timedelta(minutes=5)))
    # gated: cursor unchanged
    assert hooks._replay_context(SID_SELF, "cli") == ""
    assert _cursor(SID_SELF) == cur_seed
    # her turn ages past the gate -> the held event is still delivered (fold caps
    # the burst; nothing lost). Simulate age by rewriting her last turn ts.
    conn = storage.connect(db)
    try:
        with conn:
            conn.execute(
                "UPDATE events SET timestamp=? WHERE session_id=? AND role='user'",
                (_iso(datetime.now(timezone.utc) - timedelta(minutes=30)), SID_SELF))
    finally:
        conn.close()
    out = hooks._replay_context(SID_SELF, "cli")
    assert "fresh from another session" in out


# ── other excluded channels still excluded ──────────────────────────────────

def test_other_excluded_channel_still_blocked(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"exclude_target_channels": ["foo"]})
    _ev(db, SID_OTHER, "user", "hi")
    assert hooks._replay_context(SID_SELF, "foo") == ""
    assert _cursor(SID_SELF) is None


# ── cortex: shared last_note_ts cursor, ct now delivered on normal turn ──────

def _cortex_setup(monkeypatch, tmp_path, db):
    _setup(monkeypatch, tmp_path, db)
    monkeypatch.setenv("MARROW_CORTEX", "1")
    ws = tmp_path / "wake_state.json"
    monkeypatch.setattr(cortex_bridge, "_cortex_wake_state_path", lambda: ws)
    return ws


def test_cortex_receives_on_normal_turn(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    ws = _cortex_setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "she said hi", ts="2026-07-17T04:10:00Z")
    _ev(db, SID_OTHER, "assistant", "he replied", ts="2026-07-17T04:11:00Z")
    # cortex session channel is 'ct' — previously excluded, now delivered
    out = hooks._replay_context("ctsid0000", "ct")
    assert out.startswith("## Recent replay from other sessions")
    assert "she said hi" in out and "he replied" in out
    # cursor advanced into wake_state.last_note_ts
    import json
    d = json.loads(ws.read_text())
    assert d["last_note_ts"] == "2026-07-17T04:11:00Z"


def test_cortex_shared_cursor_no_double_feed(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    ws = _cortex_setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "first msg", ts="2026-07-17T04:10:00Z")
    out1 = hooks._replay_context("ctsid0000", "ct")
    assert "first msg" in out1
    # a subsequent note render (simulated: it reads timestamp > last_note_ts) must
    # not re-deliver the same event. Same query the note side uses.
    import json
    since = json.loads(ws.read_text())["last_note_ts"]
    conn = storage.connect(db)
    try:
        rows = conn.execute(
            "SELECT content FROM events WHERE role IN ('user','assistant') "
            "AND COALESCE(channel,'') != 'ct' AND timestamp > ? ORDER BY id",
            (since,)).fetchall()
    finally:
        conn.close()
    assert rows == []  # note render sees nothing new -> no double-feed
    # hook itself also empty on the next turn (cursor consumed)
    assert hooks._replay_context("ctsid0000", "ct") == ""


def test_note_then_hook_no_double_feed(tmp_path, monkeypatch):
    # note render advances last_note_ts first; the hook must not re-deliver.
    db = _fresh_db(tmp_path)
    ws = _cortex_setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "delivered by note", ts="2026-07-17T04:10:00Z")
    # simulate note render: stamp last_note_ts to the newest rendered ts
    import json
    ws.write_text(json.dumps({"last_note_ts": "2026-07-17T04:10:00Z"}))
    # hook on the next normal turn sees nothing new
    assert hooks._replay_context("ctsid0000", "ct") == ""


def test_cortex_excludes_ct_source(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _cortex_setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "cortex monologue", channel="ct",
        ts="2026-07-17T04:10:00Z")
    assert hooks._replay_context("ctsid0000", "ct") == ""
