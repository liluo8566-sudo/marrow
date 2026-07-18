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


# ── F8: monotonic advance (no rewind) + interleaved note/hook deliveries ──────

def test_cortex_hook_never_rewinds_note_baseline(tmp_path, monkeypatch):
    # note.py advanced the shared baseline further than the hook's own render
    # would compute (smaller row-limit / stricter strip). The hook must NOT
    # rewind last_note_ts and re-deliver already-consumed turns (the 18:38 bug).
    db = _fresh_db(tmp_path)
    ws = _cortex_setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "old turn", ts="2026-07-17T04:05:00Z")
    _ev(db, SID_OTHER, "assistant", "old reply", ts="2026-07-17T04:06:00Z")
    import json
    # note.py already consumed up to 04:06 (ahead of what a hook re-render of the
    # same rows would land on if it wrote unconditionally).
    ws.write_text(json.dumps({"last_note_ts": "2026-07-17T04:06:00Z"}))
    # a new turn arrives, older than the baseline is impossible; verify the hook,
    # seeing only <= baseline rows, delivers nothing and leaves the baseline put.
    assert hooks._replay_context("ctsid0000", "ct") == ""
    assert json.loads(ws.read_text())["last_note_ts"] == "2026-07-17T04:06:00Z"


def test_cortex_alternating_note_hook_no_repeat(tmp_path, monkeypatch):
    # Alternate note-side stamps and hook-side deliveries; no line repeats and the
    # baseline only moves forward.
    db = _fresh_db(tmp_path)
    ws = _cortex_setup(monkeypatch, tmp_path, db)
    import json

    def _baseline():
        return json.loads(ws.read_text()).get("last_note_ts")

    # hook delivers turn A
    _ev(db, SID_OTHER, "user", "line A", ts="2026-07-17T04:10:00Z")
    outA = hooks._replay_context("ctsid0000", "ct")
    assert "line A" in outA
    base1 = _baseline()

    # note-side delivers turn B (stamps baseline forward itself)
    _ev(db, SID_OTHER, "assistant", "line B", ts="2026-07-17T04:11:00Z")
    ws.write_text(json.dumps({"last_note_ts": "2026-07-17T04:11:00Z"}))
    assert _baseline() > base1

    # hook next turn: nothing new (B consumed by note) -> no repeat of A or B
    assert hooks._replay_context("ctsid0000", "ct") == ""
    assert _baseline() == "2026-07-17T04:11:00Z"

    # hook delivers turn C
    _ev(db, SID_OTHER, "user", "line C", ts="2026-07-17T04:12:00Z")
    outC = hooks._replay_context("ctsid0000", "ct")
    assert "line C" in outC
    assert "line A" not in outC and "line B" not in outC
    assert _baseline() == "2026-07-17T04:12:00Z"


def test_cortex_concurrent_interleave_no_repeat_no_backward(tmp_path, monkeypatch):
    # Two writers hit the same wake_state under the real flock: one thread runs
    # hook deliveries, another stamps note-side baselines. No delivered line ever
    # repeats across all hook outputs and the baseline is monotonic throughout.
    import json
    import threading

    db = _fresh_db(tmp_path)
    ws = _cortex_setup(monkeypatch, tmp_path, db)
    for i in range(20):
        _ev(db, SID_OTHER, "user", f"msg {i}",
            ts=f"2026-07-17T05:{i:02d}:00Z")

    delivered = []
    baselines = []
    lock = threading.Lock()
    errors = []

    def _hook_worker():
        try:
            for _ in range(30):
                out = hooks._replay_context("ctsid0000", "ct")
                with lock:
                    delivered.append(out)
                    if ws.exists():
                        b = json.loads(ws.read_text()).get("last_note_ts")
                        if b:
                            baselines.append(b)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def _note_worker():
        try:
            for i in range(0, 20, 3):
                with cortex_bridge._wake_state_lock(ws):
                    d = cortex_bridge._wake_state_load(ws)
                    cur = d.get("last_note_ts")
                    cand = f"2026-07-17T05:{i:02d}:00Z"
                    if not cur or cand > cur:
                        d["last_note_ts"] = cand
                        cortex_bridge._wake_state_save(ws, d)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=_hook_worker),
               threading.Thread(target=_note_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # baseline monotonic (never moves backward)
    assert baselines == sorted(baselines)
    # no single msg line delivered by the hook twice across all outputs
    seen = set()
    for out in delivered:
        for i in range(20):
            tag = f"msg {i}"
            if out and tag in out:
                assert tag not in seen, f"{tag} delivered twice"
                seen.add(tag)
