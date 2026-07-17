"""P2 — cli delivery of outbox notes via outbox.deliver.

Covers: targeted (session:<sid>) delivery, cli broadcast consume-once,
at-most-once atomic claim under concurrent racing connections, ct-only gating,
and render format (config inject_header + body verbatim).
"""
from __future__ import annotations

import io
import json
import threading

from marrow import config, hooks, outbox, storage

SID_A = "aaaa1111-2222-3333"
SID_B = "bbbb4444-5555-6666"


def _mk(db, target, body, *, from_sid="cccc9999", from_channel="cli"):
    conn = storage.connect(db)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO outbox(from_sid, from_channel, target, body)"
                " VALUES(?,?,?,?)", (from_sid, from_channel, target, body))
        return cur.lastrowid
    finally:
        conn.close()


def _status(db, rid):
    conn = storage.connect(db)
    try:
        return conn.execute(
            "SELECT status, sent_at FROM outbox WHERE id=?", (rid,)
        ).fetchone()
    finally:
        conn.close()


def _fresh_db(tmp_path):
    p = str(tmp_path / "d.db")
    storage.init_db(p).close()
    return p


# ── targeted (session:<sid>) ────────────────────────────────────────────────

def test_targeted_delivers_to_exact_sid(tmp_path):
    db = _fresh_db(tmp_path)
    rid = _mk(db, f"session:{SID_A}", "hi A")
    out = outbox.deliver(SID_A, "cli", db=db)
    assert out and "hi A" in out
    row = _status(db, rid)
    assert row["status"] == "sent" and row["sent_at"]


def test_targeted_not_delivered_to_other_sid(tmp_path):
    db = _fresh_db(tmp_path)
    rid = _mk(db, f"session:{SID_A}", "hi A")
    assert outbox.deliver(SID_B, "cli", db=db) is None
    assert _status(db, rid)["status"] == "pending"


def test_render_header_and_body(tmp_path):
    db = _fresh_db(tmp_path)
    _mk(db, f"session:{SID_A}", "the body", from_channel="wx",
        from_sid="feedbeef")
    out = outbox.deliver(SID_A, "cli", db=db)
    # header line from config template + body verbatim on its own line
    assert "📮 Message from wx·feed" in out
    assert out.endswith("the body")


# ── cli broadcast consume-once ──────────────────────────────────────────────

def test_cli_broadcast_first_session_wins(tmp_path):
    db = _fresh_db(tmp_path)
    _mk(db, "cli", "broadcast note")
    first = outbox.deliver(SID_A, "cli", db=db)
    second = outbox.deliver(SID_B, "cli", db=db)
    assert first and "broadcast note" in first
    assert second is None  # consumed by the first cli session


def test_cli_broadcast_ignored_by_non_cli_channel(tmp_path):
    db = _fresh_db(tmp_path)
    rid = _mk(db, "cli", "broadcast note")
    assert outbox.deliver(SID_A, "wx", db=db) is None
    assert _status(db, rid)["status"] == "pending"


# ── ct gating ───────────────────────────────────────────────────────────────

def test_ct_delivered_only_when_cortex(tmp_path):
    db = _fresh_db(tmp_path)
    rid = _mk(db, "ct", "for cortex")
    assert outbox.deliver(SID_A, "cli", db=db) is None       # not cortex
    assert _status(db, rid)["status"] == "pending"
    out = outbox.deliver(SID_A, "ct", is_cortex=True, db=db)
    assert out and "for cortex" in out
    assert _status(db, rid)["status"] == "sent"


# ── at-most-once under concurrency ──────────────────────────────────────────

def test_concurrent_claim_exactly_one_winner(tmp_path):
    """Two connections race the same broadcast row → exactly one delivers."""
    db = _fresh_db(tmp_path)
    _mk(db, "cli", "race note")
    results: list = []
    barrier = threading.Barrier(2)

    def worker(sid):
        barrier.wait()
        results.append(outbox.deliver(sid, "cli", db=db))

    ts = [threading.Thread(target=worker, args=(s,)) for s in (SID_A, SID_B)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    delivered = [r for r in results if r]
    assert len(delivered) == 1
    conn = storage.connect(db)
    try:
        statuses = [r["status"] for r in conn.execute(
            "SELECT status FROM outbox").fetchall()]
    finally:
        conn.close()
    assert statuses == ["sent"]


def test_multiple_notes_same_target_all_delivered(tmp_path):
    db = _fresh_db(tmp_path)
    _mk(db, f"session:{SID_A}", "one")
    _mk(db, f"session:{SID_A}", "two")
    out = outbox.deliver(SID_A, "cli", db=db)
    assert "one" in out and "two" in out


def test_no_sid_no_channel_match_returns_none(tmp_path):
    db = _fresh_db(tmp_path)
    _mk(db, "cli", "x")
    # a non-cli channel with no sid and not cortex matches nothing
    assert outbox.deliver(None, "wx", db=db) is None


# ── ct wake-branch merge (hook integration) ─────────────────────────────────

def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _ctx(capsys):
    out = capsys.readouterr().out
    if not out.strip():
        return ""
    return json.loads(out)["hookSpecificOutput"].get("additionalContext", "")


def _enable_cortex(monkeypatch, tmp_path, db, extra=None):
    real = config.load

    def _patched():
        cfg = dict(real())
        cx = dict(cfg.get("cortex", {}))
        cx["enabled"] = True
        cx["home"] = str(tmp_path)
        if extra:
            cx.update(extra)
        cfg["cortex"] = cx
        return cfg

    monkeypatch.setattr(config, "load", _patched)
    monkeypatch.setattr(config, "db_path", lambda: db)


def test_ct_note_merged_into_wake_payload(tmp_path, monkeypatch, capsys):
    """A ct-targeted outbox note is consumed inside the wake branch and appended
    below the wakeup note (the normal delivery path never runs on a wake turn)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    db = _fresh_db(tmp_path)
    (tmp_path / "wakeup_note.md").write_text("wake body", encoding="utf-8")
    _enable_cortex(monkeypatch, tmp_path, db, {"wake_marker": "[CORTEX-WAKE]"})
    _mk(db, "ct", "covert note for cortex", from_channel="tg", from_sid="tgtg0001")
    _stdin(monkeypatch, {"session_id": "ctsid1",
                         "prompt": "[CORTEX-WAKE] 14:00 wake"})
    assert hooks.main(["user_prompt_submit"]) == 0
    ctx = _ctx(capsys)
    assert "wake body" in ctx
    assert "covert note for cortex" in ctx
    # note consumed
    assert _status(db, 1)["status"] == "sent"


def test_wake_payload_note_only_when_no_wakeup(tmp_path, monkeypatch, capsys):
    """No frozen wakeup note but a pending ct note → the note alone is injected."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    db = _fresh_db(tmp_path)
    _enable_cortex(monkeypatch, tmp_path, db, {"wake_marker": "[CORTEX-WAKE]"})
    _mk(db, "ct", "lone note")
    _stdin(monkeypatch, {"session_id": "ctsid2",
                         "prompt": "[CORTEX-WAKE] 14:00 wake"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "lone note" in _ctx(capsys)


# ── cli delivery via the normal (non-cortex) prompt path ────────────────────

def test_cli_session_note_injected_on_normal_turn(tmp_path, monkeypatch, capsys):
    """A plain cli session gets its session-targeted note injected on a real
    prompt (rides the nudge channel, emitted even when recall has no hits)."""
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    monkeypatch.delenv("MARROW_CHANNEL", raising=False)
    db = _fresh_db(tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: db)
    _mk(db, f"session:{SID_A}", "note for A")
    _stdin(monkeypatch, {"session_id": SID_A, "cwd": str(tmp_path),
                         "prompt": "some ordinary question"})
    assert hooks.main(["user_prompt_submit"]) == 0
    ctx = _ctx(capsys)
    assert "note for A" in ctx
    assert _status(db, 1)["status"] == "sent"


def test_ct_note_injected_on_normal_cortex_turn(tmp_path, monkeypatch, capsys):
    """P12 fix: a ct-targeted note is delivered on a NORMAL cortex turn (no wake
    marker), not only on the wake turn — the normal path now passes
    is_cortex=is_cortex_session()."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    db = _fresh_db(tmp_path)
    _enable_cortex(monkeypatch, tmp_path, db)
    _mk(db, "ct", "covert note on a normal turn", from_channel="tg")
    _stdin(monkeypatch, {"session_id": "ctsid9", "cwd": str(tmp_path),
                         "prompt": "an ordinary cortex reply, not a wake bell"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "covert note on a normal turn" in _ctx(capsys)
    assert _status(db, 1)["status"] == "sent"


def test_ct_note_normal_turn_consume_once(tmp_path, monkeypatch, capsys):
    """A ct note claimed on one normal turn is not re-delivered on the next."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    db = _fresh_db(tmp_path)
    _enable_cortex(monkeypatch, tmp_path, db)
    _mk(db, "ct", "once only")
    _stdin(monkeypatch, {"session_id": "ctsidA", "cwd": str(tmp_path),
                         "prompt": "turn one"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "once only" in _ctx(capsys)
    _stdin(monkeypatch, {"session_id": "ctsidA", "cwd": str(tmp_path),
                         "prompt": "turn two"})
    assert hooks.main(["user_prompt_submit"]) == 0
    assert "once only" not in _ctx(capsys)
    assert _status(db, 1)["status"] == "sent"
