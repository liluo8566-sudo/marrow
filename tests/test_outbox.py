"""P1 — outbox table + msg MCP tool.

Covers: v40 migration idempotency, happy-path insert, permission refusal,
session-prefix resolve (0/1/N matches), daily-cap refusal under concurrency,
retention prune. The msg dispatch tool is exercised via daemon.msg.
"""
from __future__ import annotations

import threading

import pytest

from marrow import config, daemon, outbox, storage


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = str(tmp_path / "t.db")
    storage.init_db(p).close()
    monkeypatch.setattr(daemon, "_DB", p)
    # sender = cortex by default so tg/wx sends pass the whitelist
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    monkeypatch.delenv("MARROW_CORTEX", raising=False)
    return p


def _rows(p):
    conn = storage.connect(p)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM outbox ORDER BY id").fetchall()]
    finally:
        conn.close()


# ── migration ──────────────────────────────────────────────────────────────

def test_migration_creates_outbox_and_indexes(tmp_path):
    conn = storage.init_db(str(tmp_path / "m.db"))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 42
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(outbox)")}
        assert {"id", "created_at", "from_sid", "from_channel", "target",
                "body", "status", "sent_at", "retry_count", "watch_reply",
                "watch_timeout_min", "watch_state",
                "replied_at", "reply_text", "receipt_seen",
                "claimed_by", "claimed_at"} <= cols
        idx = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='outbox'")}
        assert "idx_outbox_status_target" in idx
        assert "idx_outbox_watch_state_sent" in idx
    finally:
        conn.close()


def test_migration_idempotent(tmp_path):
    p = str(tmp_path / "idem.db")
    conn = storage.init_db(p)
    conn.close()
    # second init_db (re-run every migration) must not raise
    conn = storage.init_db(p)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 42
        storage._migrate_to_v40(conn)  # direct re-entry
        storage._migrate_to_v41(conn)
        storage._migrate_to_v42(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 42
        # receipt + audit columns present + idempotent (re-init must not raise)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(outbox)")}
        assert {"replied_at", "reply_text", "receipt_seen",
                "claimed_by", "claimed_at"} <= cols
    finally:
        conn.close()


# ── happy path ───────────────────────────────────────────────────────────────

def test_send_session_target_inserts(db):
    conn = storage.connect(db)
    with conn:
        conn.execute("INSERT INTO sessions(sid, channel) VALUES('abcd1234','cli')")
    conn.close()
    r = outbox.send("session:abcd", "hello there", db=db)
    assert r["ok"] and r["target"] == "session:abcd1234"
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["target"] == "session:abcd1234"
    assert rows[0]["body"] == "hello there"
    assert rows[0]["status"] == "pending"
    assert rows[0]["from_channel"] == "ct"


def test_send_empty_text_refused(db):
    r = outbox.send("cli", "   ", db=db)
    assert not r["ok"] and "empty" in r["error"]


def test_send_ct_target_fires_note_kick(db, monkeypatch):
    # F9: a ct-targeted send kicks cortex immediately (kind='note', note_id=row).
    from marrow import cortex_bridge
    calls = []
    monkeypatch.setattr(cortex_bridge, "kick_cortex",
                        lambda kind, note_id=None: calls.append((kind, note_id)))
    r = outbox.send("ct", "睡了吗", db=db)
    assert r["ok"]
    assert calls == [("note", r["id"])]


def test_send_non_ct_target_does_not_kick(db, monkeypatch):
    # cli / session sends stay mailbox — no kick.
    from marrow import cortex_bridge
    calls = []
    monkeypatch.setattr(cortex_bridge, "kick_cortex",
                        lambda kind, note_id=None: calls.append((kind, note_id)))
    outbox.send("cli", "hey other session", db=db)
    assert calls == []


def test_send_watch_flags_stored(db):
    r = outbox.send("tg", "ping", watch_reply=True, watch_timeout_min=15, db=db)
    assert r["ok"]
    row = _rows(db)[0]
    assert row["watch_reply"] == 1
    assert row["watch_state"] == "armed"
    assert row["watch_timeout_min"] == 15


def test_send_timeout_only_arms(db):
    # FIX 3: watch_reply=False + watch_timeout_min set must still arm, or
    # claim_timeouts (synapse cortex_kick.py, WHERE watch_state='armed') can
    # never find the row.
    r = outbox.send("tg", "ping", watch_timeout_min=10, db=db)
    assert r["ok"]
    row = _rows(db)[0]
    assert row["watch_reply"] == 0
    assert row["watch_state"] == "armed"
    assert row["watch_timeout_min"] == 10


def test_send_reply_only_arms(db):
    r = outbox.send("tg", "ping", watch_reply=True, db=db)
    assert r["ok"]
    row = _rows(db)[0]
    assert row["watch_reply"] == 1
    assert row["watch_state"] == "armed"
    assert row["watch_timeout_min"] is None


def test_send_no_watch_stays_unarmed(db):
    r = outbox.send("tg", "ping", db=db)
    assert r["ok"]
    row = _rows(db)[0]
    assert row["watch_reply"] == 0
    assert row["watch_state"] is None
    assert row["watch_timeout_min"] is None


# ── permission ───────────────────────────────────────────────────────────────

def test_user_target_refused_for_non_whitelisted_channel(db, monkeypatch):
    monkeypatch.setenv("MARROW_CHANNEL", "cli")
    r = outbox.send("tg", "hi", db=db)
    assert not r["ok"] and "not allowed" in r["error"]
    assert _rows(db) == []


def test_session_target_open_to_all_channels(db, monkeypatch):
    monkeypatch.setenv("MARROW_CHANNEL", "cli")
    conn = storage.connect(db)
    with conn:
        conn.execute("INSERT INTO sessions(sid, channel) VALUES('zzz9','wx')")
    conn.close()
    r = outbox.send("session:zzz9", "note", db=db)
    assert r["ok"]


def test_cli_ct_targets_open_to_all(db, monkeypatch):
    monkeypatch.setenv("MARROW_CHANNEL", "cli")
    assert outbox.send("cli", "a", db=db)["ok"]
    assert outbox.send("ct", "b", db=db)["ok"]


# ── session-prefix resolve ─────────────────────────────────────────────────

def test_prefix_zero_matches_refused(db):
    r = outbox.send("session:nope", "x", db=db)
    assert not r["ok"] and "no session" in r["error"]


def test_prefix_multiple_matches_refused(db):
    conn = storage.connect(db)
    with conn:
        conn.execute("INSERT INTO sessions(sid, channel) VALUES('dead01','cli')")
        conn.execute("INSERT INTO sessions(sid, channel) VALUES('dead02','cli')")
    conn.close()
    r = outbox.send("session:dead", "x", db=db)
    assert not r["ok"] and "matches 2" in r["error"]


def test_unknown_target_refused(db):
    r = outbox.send("slack", "x", db=db)
    assert not r["ok"] and "unknown target" in r["error"]


# ── daily cap ────────────────────────────────────────────────────────────────

def test_user_cap_refuses_and_alerts(db, monkeypatch):
    monkeypatch.setattr(config, "load", _cfg_with_caps(config, cap_user=2))
    assert outbox.send("tg", "1", db=db)["ok"]
    assert outbox.send("wx", "2", db=db)["ok"]
    r = outbox.send("tg", "3", db=db)
    assert not r["ok"] and "daily cap" in r["error"]
    assert len(_rows(db)) == 2
    conn = storage.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE type='outbox_cap'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_session_cap_independent_of_user_cap(db, monkeypatch):
    monkeypatch.setattr(config, "load",
                        _cfg_with_caps(config, cap_user=1, cap_session=1))
    assert outbox.send("cli", "s", db=db)["ok"]
    assert not outbox.send("ct", "s2", db=db)["ok"]  # session cap hit
    # user cap independent — tg still available
    assert outbox.send("tg", "u", db=db)["ok"]


def test_ct_cap_blocked_send_does_not_kick(db, monkeypatch):
    # F9: the cap gate runs BEFORE the kick — a cap-refused ct send never kicks.
    from marrow import cortex_bridge
    calls = []
    monkeypatch.setattr(cortex_bridge, "kick_cortex",
                        lambda kind, note_id=None: calls.append(kind))
    monkeypatch.setattr(config, "load",
                        _cfg_with_caps(config, cap_user=1, cap_session=1))
    assert outbox.send("ct", "first", db=db)["ok"]     # this one kicks
    assert not outbox.send("ct", "second", db=db)["ok"]  # cap hit -> no kick
    assert calls == ["note"]                              # exactly one kick


def test_cap_under_concurrency(db, monkeypatch):
    """Two threads racing at cap=1 → exactly one insert wins."""
    monkeypatch.setattr(config, "load", _cfg_with_caps(config, cap_user=1))
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(outbox.send("tg", "race", db=db))

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    oks = [r for r in results if r.get("ok")]
    assert len(oks) == 1
    assert len(_rows(db)) == 1


# ── retention prune ────────────────────────────────────────────────────────

def test_prune_removes_old_sent_failed_only(db):
    conn = storage.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO outbox(target, body, status, created_at)"
            " VALUES('tg','old-sent','sent',"
            " strftime('%Y-%m-%dT%H:%M:%SZ','now','-40 days'))")
        conn.execute(
            "INSERT INTO outbox(target, body, status, created_at)"
            " VALUES('tg','old-failed','failed',"
            " strftime('%Y-%m-%dT%H:%M:%SZ','now','-40 days'))")
        conn.execute(
            "INSERT INTO outbox(target, body, status, created_at)"
            " VALUES('tg','old-pending','pending',"
            " strftime('%Y-%m-%dT%H:%M:%SZ','now','-40 days'))")
        conn.execute(
            "INSERT INTO outbox(target, body, status, created_at)"
            " VALUES('tg','recent-sent','sent',"
            " strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 days'))")
    with conn:
        n = outbox.prune(conn, 30)
    conn.close()
    assert n == 2
    bodies = {r["body"] for r in _rows(db)}
    assert bodies == {"old-pending", "recent-sent"}


# ── msg tool dispatch ──────────────────────────────────────────────────────

def test_msg_tool_send_and_list(db):
    conn = storage.connect(db)
    with conn:
        conn.execute("INSERT INTO sessions(sid, channel) VALUES('feed01','cli')")
    conn.close()
    r = daemon.msg("send", to="session:feed01", text="via tool")
    assert r["ok"]
    listed = daemon.msg("list")
    assert any(x["body"] == "via tool" for x in listed)


def test_msg_tool_unknown_action(db):
    r = daemon.msg("frobnicate")
    assert not r["ok"] and "unknown action" in r["error"]


def test_msg_tool_send_missing_args(db):
    assert not daemon.msg("send", text="no target")["ok"]
    assert not daemon.msg("send", to="cli")["ok"]


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg_with_caps(config_mod, *, cap_user=30, cap_session=100):
    real = config_mod.load

    def _load():
        cfg = real()
        ob = dict(cfg.get("outbox", {}) or {})
        ob["daily_cap_user"] = cap_user
        ob["daily_cap_session"] = cap_session
        ob.setdefault("user_send_channels", ["ct"])
        ob.setdefault("retention_days", 30)
        cfg["outbox"] = ob
        return cfg

    return _load
