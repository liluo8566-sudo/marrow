"""P3 — cross-session replay inject (hooks._replay_context).

Covers: first-sight cursor seed (no backfill), cursor advance, turn grouping +
cap + fold line, own-sid exclusion, source-ct exclusion, destination-channel
exclusion, enabled=false, nothing-new → empty inject.
"""
from __future__ import annotations

from marrow import config, hooks, storage

SID_SELF = "self1111-2222"
SID_OTHER = "othr9999-8888"


def _fresh_db(tmp_path):
    p = str(tmp_path / "d.db")
    storage.init_db(p).close()
    return p


def _ev(db, sid, role, content, *, channel="cli", ts="2026-07-17T04:00:00Z"):
    conn = storage.connect(db)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO events(session_id, timestamp, role, content, channel)"
                " VALUES(?,?,?,?,?)", (sid, ts, role, content, channel))
        return cur.lastrowid
    finally:
        conn.close()


def _cursor(sid):
    return hooks._load_replay_cursor(sid)


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


# ── first-sight seed: no backfill ───────────────────────────────────────────

def test_first_sight_seeds_no_backfill(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "old history one")
    _ev(db, SID_OTHER, "assistant", "old history two")
    # first sight → seed to MAX(id), inject nothing
    assert hooks._replay_context(SID_SELF, "cli") == ""
    assert _cursor(SID_SELF) == 2


def test_first_sight_empty_db_seeds_zero(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    assert hooks._replay_context(SID_SELF, "cli") == ""
    assert _cursor(SID_SELF) == 0


# ── nothing new after seed ──────────────────────────────────────────────────

def test_nothing_new_empty_inject(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "existing")
    hooks._replay_context(SID_SELF, "cli")  # seed
    assert hooks._replay_context(SID_SELF, "cli") == ""  # no new rows


# ── happy render + cursor advance ───────────────────────────────────────────

def test_render_and_cursor_advance(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    hooks._replay_context(SID_SELF, "cli")  # seed at 0
    _ev(db, SID_OTHER, "user", "hey there", ts="2026-07-17T04:10:00Z")
    aid = _ev(db, SID_OTHER, "assistant", "hi back", ts="2026-07-17T04:11:00Z")
    out = hooks._replay_context(SID_SELF, "cli")
    assert out.startswith("## Recent replay from other sessions")
    assert "N: hey there" in out
    assert "Y: hi back" in out
    assert "othr" in out  # source sid[:4]
    assert "cli·" in out
    assert _cursor(SID_SELF) == aid
    # consumed → next call empty
    assert hooks._replay_context(SID_SELF, "cli") == ""


# ── turn grouping: consecutive user msgs = one turn ─────────────────────────

def test_consecutive_user_same_turn(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"max_turns": 1})
    hooks._replay_context(SID_SELF, "cli")
    _ev(db, SID_OTHER, "user", "part one", ts="2026-07-17T04:10:00Z")
    _ev(db, SID_OTHER, "user", "part two", ts="2026-07-17T04:10:30Z")
    _ev(db, SID_OTHER, "assistant", "reply", ts="2026-07-17T04:11:00Z")
    out = hooks._replay_context(SID_SELF, "cli")
    # both user msgs + assistant land in the single kept turn, no fold
    assert "part one" in out and "part two" in out and "reply" in out
    assert "more turns" not in out


# ── cap + fold ──────────────────────────────────────────────────────────────

def test_cap_and_fold(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"max_turns": 2})
    hooks._replay_context(SID_SELF, "cli")
    for i in range(4):  # 4 distinct turns
        _ev(db, SID_OTHER, "user", f"q{i}", ts=f"2026-07-17T05:0{i}:00Z")
        _ev(db, SID_OTHER, "assistant", f"a{i}", ts=f"2026-07-17T05:0{i}:30Z")
    out = hooks._replay_context(SID_SELF, "cli")
    # Keep the NEWEST turns; the older overflow folds (cursor advances to max_id
    # unconditionally, so the folded turns would be silenced forever otherwise).
    assert "q2" in out and "q3" in out          # newest kept
    assert "q0" not in out and "q1" not in out  # oldest folded
    assert "+2 earlier turns" in out
    # cursor advanced past ALL rows despite fold (ambient)
    conn = storage.connect(db)
    try:
        maxid = conn.execute("SELECT MAX(id) m FROM events").fetchone()["m"]
    finally:
        conn.close()
    assert _cursor(SID_SELF) == maxid


# ── per_msg_chars truncation ────────────────────────────────────────────────

def test_truncation(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"per_msg_chars": 10})
    hooks._replay_context(SID_SELF, "cli")
    _ev(db, SID_OTHER, "user", "abcdefghijklmnopqrstuvwxyz")
    out = hooks._replay_context(SID_SELF, "cli")
    assert "abcdefghi…" in out


# ── own-sid exclusion ───────────────────────────────────────────────────────

def test_own_sid_excluded(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    hooks._replay_context(SID_SELF, "cli")
    _ev(db, SID_SELF, "user", "my own message")
    assert hooks._replay_context(SID_SELF, "cli") == ""


# ── source ct exclusion ─────────────────────────────────────────────────────

def test_source_ct_excluded(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    hooks._replay_context(SID_SELF, "cli")
    _ev(db, SID_OTHER, "user", "cortex monologue", channel="ct")
    assert hooks._replay_context(SID_SELF, "cli") == ""


# ── destination channel exclusion ──────────────────────────────────────────

def test_destination_channel_excluded(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _ev(db, SID_OTHER, "user", "should never reach cortex")
    # a session ON channel ct receives nothing at all — no seed, no cursor write
    assert hooks._replay_context(SID_SELF, "ct") == ""
    assert _cursor(SID_SELF) is None


# ── enabled=false kills feature ─────────────────────────────────────────────

def test_enabled_false(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db, {"enabled": False})
    _ev(db, SID_OTHER, "user", "hi")
    assert hooks._replay_context(SID_SELF, "cli") == ""
    assert _cursor(SID_SELF) is None
