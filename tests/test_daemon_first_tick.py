"""first MCP tool (C4 First tick, renamed 07-06) — session self-marks a
nagged item seen/handled so repeat-nagging stops. Marks land in
ct_first_tick. untick/list actions covered in test_daemon_actions.py."""
from __future__ import annotations

import pytest

from marrow import config, cortex_bridge, daemon, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    storage.init_db(db).close()
    monkeypatch.setattr(daemon, "_DB", db)
    monkeypatch.setattr(cortex_bridge, "_DB", db)
    monkeypatch.setattr(config, "db_path", lambda: db)
    return db


def _read(db, item):
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT item, seen_at, sid, note, status FROM ct_first_tick WHERE item=?",
            (item,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def test_mark_records_row(env):
    out = cortex_bridge.first("tick", item="gym-reminder",
                        note="already at Clayton gym", sid="s1")
    assert out == {"ok": True, "item": "gym-reminder", "sid": "s1",
                   "note": "already at Clayton gym", "status": "done"}
    row = _read(env, "gym-reminder")
    assert row["sid"] == "s1"
    assert row["note"] == "already at Clayton gym"
    assert row["status"] == "done"
    assert row["seen_at"]  # UTC stamp present


def test_latest_call_wins(env):
    cortex_bridge.first("tick", item="item-x", note="starting", sid="s1")
    cortex_bridge.first("tick", item="item-x", note="handled", sid="s2")
    row = _read(env, "item-x")
    assert row["sid"] == "s2"
    assert row["note"] == "handled"
    conn = storage.connect(env)
    try:
        n = conn.execute("SELECT COUNT(*) FROM ct_first_tick WHERE item='item-x'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1  # upsert, no duplicate rows


def test_reject_empty_item(env):
    out = cortex_bridge.first("tick", item="   ")
    assert out["ok"] is False
    conn = storage.connect(env)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ct_first_tick").fetchone()[0] == 0
    finally:
        conn.close()


def test_default_sid_falls_back_gracefully(env):
    out = cortex_bridge.first("tick", item="no-session-item")
    assert out["ok"] is True
    assert out["sid"] is None  # no active session in a fresh test DB
