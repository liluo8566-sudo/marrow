"""F6 — own-channel note visibility (hooks._outbound_notes).

A bridge sends an outbound note on a wire channel (cortex→tg) straight to the
wire, bypassing that channel's resident session. This surfaces those notes to
the resident session on its next turn: header + bodies, per-sid cursor,
forward-only, consume-once. Other channels' notes are not surfaced; only sent
outbound notes (never her replies) show.
"""
from __future__ import annotations

from marrow import config, hooks, storage

SID = "self1111-2222"


def _fresh_db(tmp_path):
    p = str(tmp_path / "d.db")
    storage.init_db(p).close()
    return p


def _sent_note(db, target, body, sent_at, *, status="sent", reply_text=None):
    conn = storage.connect(db)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO outbox(from_channel, target, body, status, sent_at,"
                " reply_text) VALUES('ct', ?, ?, ?, ?, ?)",
                (target, body, status, sent_at, reply_text),
            )
        return cur.lastrowid
    finally:
        conn.close()


def _setup(monkeypatch, tmp_path, db):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: db)


def _cursor(sid):
    return hooks._load_outbound_cursor(sid)


# ── first-sight seed: no backfill ───────────────────────────────────────────

def test_first_sight_seeds_no_backfill(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    _sent_note(db, "tg", "old note", "2026-07-17T04:00:00Z")
    assert hooks._outbound_notes(SID, "tg") == ""
    assert _cursor(SID) == "2026-07-17T04:00:00Z"


def test_first_sight_empty_seeds_blank(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    assert hooks._outbound_notes(SID, "tg") == ""
    # no notes yet: cursor file exists but blank (seeded, distinct from absent)
    assert _cursor(SID) == ""


# ── surfacing + consume-once ────────────────────────────────────────────────

def test_cortex_to_tg_note_surfaced_next_turn(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    hooks._outbound_notes(SID, "tg")  # seed
    _sent_note(db, "tg", "hey, checking in", "2026-07-17T05:00:00Z")
    out = hooks._outbound_notes(SID, "tg")
    assert "Sent on this channel" in out
    assert "hey, checking in" in out


def test_consume_once(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    hooks._outbound_notes(SID, "tg")  # seed
    _sent_note(db, "tg", "one", "2026-07-17T05:00:00Z")
    assert "one" in hooks._outbound_notes(SID, "tg")
    # second turn: no repeat
    assert hooks._outbound_notes(SID, "tg") == ""


def test_cursor_forward_only(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    hooks._outbound_notes(SID, "tg")  # seed blank
    _sent_note(db, "tg", "later", "2026-07-17T06:00:00Z")
    hooks._outbound_notes(SID, "tg")  # cursor → 06:00
    assert _cursor(SID) == "2026-07-17T06:00:00Z"
    # a note stamped EARLIER than the cursor never rewinds it
    _sent_note(db, "tg", "earlier-late-arrival", "2026-07-17T05:30:00Z")
    assert hooks._outbound_notes(SID, "tg") == ""
    assert _cursor(SID) == "2026-07-17T06:00:00Z"


# ── channel isolation ───────────────────────────────────────────────────────

def test_other_channel_notes_not_surfaced(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    hooks._outbound_notes(SID, "tg")  # seed
    _sent_note(db, "wx", "wx note", "2026-07-17T05:00:00Z")
    assert hooks._outbound_notes(SID, "tg") == ""


def test_non_wire_channel_skipped(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    # cli/ct/session are delivered inline; own-note surfacing never runs
    _sent_note(db, "cli", "inline note", "2026-07-17T05:00:00Z")
    assert hooks._outbound_notes(SID, "cli") == ""
    assert _cursor(SID) is None  # never even seeded


# ── outbound only — her replies untouched ───────────────────────────────────

def test_pending_note_not_surfaced(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    hooks._outbound_notes(SID, "tg")  # seed
    _sent_note(db, "tg", "not yet sent", "2026-07-17T05:00:00Z", status="pending")
    assert hooks._outbound_notes(SID, "tg") == ""


def test_reply_text_not_rendered(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    hooks._outbound_notes(SID, "tg")  # seed
    _sent_note(
        db, "tg", "the outbound body", "2026-07-17T05:00:00Z",
        reply_text="HER PRIVATE REPLY",
    )
    out = hooks._outbound_notes(SID, "tg")
    assert "the outbound body" in out
    assert "HER PRIVATE REPLY" not in out


# ── per-sid isolation ───────────────────────────────────────────────────────

def test_per_sid_cursor(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _setup(monkeypatch, tmp_path, db)
    other = "othr9999-8888"
    hooks._outbound_notes(SID, "tg")  # seed SID
    hooks._outbound_notes(other, "tg")  # seed OTHER
    _sent_note(db, "tg", "shared note", "2026-07-17T05:00:00Z")
    # both resident tg sessions independently see it once
    assert "shared note" in hooks._outbound_notes(SID, "tg")
    assert "shared note" in hooks._outbound_notes(other, "tg")
    assert hooks._outbound_notes(SID, "tg") == ""
    assert hooks._outbound_notes(other, "tg") == ""
