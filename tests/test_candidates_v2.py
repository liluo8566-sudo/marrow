"""Tests for the v2 candidate writer rules:
- milestone dedup on (scope, title, date)
- memes type whitelist + 7d events_fts frequency gate
- memes pinned defaults by type
- daily passes structured affect (importance=5) into sonnet material
"""
from __future__ import annotations

import datetime as dt

import pytest

from marrow import candidates, daily, storage


def _ev(conn, sid, ts, role, content):
    conn.execute(
        "INSERT INTO events(session_id,timestamp,role,content)"
        " VALUES(?,?,?,?)", (sid, ts, role, content),
    )


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    return conn


# ── milestone dedup ─────────────────────────────────────────────────────────

def test_milestone_dedup_same_key_noop(db):
    raw = (
        "===MILESTONE_CAND===\n"
        "[{\"title\":\"trop pass\",\"scope\":\"me\","
        " \"date\":\"2026-05-16\","
        " \"description\":\"念念 cleared trop.\",\"conf\":0.9}]\n"
        "===END===\n"
    )
    n1 = candidates.write_milestone_cand(db, raw, "2026-05-16")
    n2 = candidates.write_milestone_cand(db, raw, "2026-05-16")
    assert n1 == 1 and n2 == 0
    cnt = db.execute(
        "SELECT COUNT(*) FROM milestones WHERE title='trop pass'"
        " AND scope='me' AND date='2026-05-16'"
    ).fetchone()[0]
    assert cnt == 1


# ── memes type whitelist ────────────────────────────────────────────────────

def test_memes_invalid_type_rejected(db):
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"鬼故事\",\"type\":\"phrase\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw)
    assert n == 0
    row = db.execute("SELECT 1 FROM memes WHERE key='鬼故事'").fetchone()
    assert row is None


# ── memes 7d frequency gate ─────────────────────────────────────────────────

def _seed_events_with_key(conn, key: str, count: int, date: str):
    """Insert `count` events containing `key` within the 7d window ending at
    `date`. Trigram FTS needs ≥3-char phrase for CN; key must be ≥3 chars."""
    base = dt.date.fromisoformat(date)
    for i in range(count):
        ts = (base - dt.timedelta(days=i % 6)).isoformat() + "T10:00:00Z"
        _ev(conn, f"s{i}", ts, "user", f"random {key} more random text {i}")
    conn.commit()


def test_memes_meme_type_freq_gate_drops_low_count(db):
    """type=meme + key with 0 events in last 7d → dropped."""
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"绝绝子xy\",\"type\":\"meme\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 0
    assert db.execute(
        "SELECT 1 FROM memes WHERE key='绝绝子xy'").fetchone() is None


def test_memes_meme_type_freq_gate_passes_high_count(db):
    """type=meme + key seen ≥3 times in 7d events → inserted."""
    _seed_events_with_key(db, "绝绝子yy", 4, "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"绝绝子yy\",\"type\":\"meme\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 1
    row = db.execute(
        "SELECT type, pinned FROM memes WHERE key='绝绝子yy'"
    ).fetchone()
    assert row["type"] == "meme" and row["pinned"] == 0


def test_memes_paw_bypasses_freq_gate(db):
    """type=paw — direct insert regardless of event frequency."""
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"绿茶豹\",\"type\":\"paw\","
        " \"value\":\"私\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 1


def test_memes_fact_bypasses_freq_gate(db):
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"Plan tier\",\"type\":\"fact\","
        " \"value\":\"Max 5x\",\"context\":\"\","
        " \"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 1


def test_memes_others_bypasses_freq_gate(db):
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"some-edge\",\"type\":\"others\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.8}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 1


# ── memes pinned defaults by type ───────────────────────────────────────────

def test_memes_paw_auto_pinned(db):
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"大笨鸭子\",\"type\":\"paw\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    candidates.write_memes_cand(db, raw, date="2026-05-16")
    row = db.execute(
        "SELECT pinned FROM memes WHERE key='大笨鸭子'"
    ).fetchone()
    assert row["pinned"] == 1


def test_memes_fact_auto_pinned(db):
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"keep14\",\"type\":\"fact\","
        " \"value\":\"backup 14d rolling\",\"context\":\"\","
        " \"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    candidates.write_memes_cand(db, raw, date="2026-05-16")
    row = db.execute(
        "SELECT pinned FROM memes WHERE key='keep14'"
    ).fetchone()
    assert row["pinned"] == 1


def test_memes_meme_default_unpinned(db):
    _seed_events_with_key(db, "小笼包yy", 4, "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"小笼包yy\",\"type\":\"meme\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.8}]\n"
        "===END===\n"
    )
    candidates.write_memes_cand(db, raw, date="2026-05-16")
    row = db.execute(
        "SELECT pinned FROM memes WHERE key='小笼包yy'"
    ).fetchone()
    assert row["pinned"] == 0


def test_memes_others_default_unpinned(db):
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"edge-kind\",\"type\":\"others\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.8}]\n"
        "===END===\n"
    )
    candidates.write_memes_cand(db, raw, date="2026-05-16")
    row = db.execute(
        "SELECT pinned FROM memes WHERE key='edge-kind'"
    ).fetchone()
    assert row["pinned"] == 0


# ── daily includes structured affect block ──────────────────────────────────

class _RecordingLLM:
    def __init__(self, per_role=None):
        self.per_role = per_role or {}
        self.bodies: dict[str, str] = {}

    def call(self, role, body, *, tier="cheap"):
        self.bodies[role] = body
        return self.per_role.get(role, "—")


def test_daily_material_includes_importance5_affect_ep(tmp_path):
    p = str(tmp_path / "aff.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO affect(date,ep,valence,arousal,importance,label,"
        "description) VALUES('2026-05-16',1,0.2,0.85,5,'窘迫',"
        "'工签签证被拒')"
    )
    conn.execute(
        "INSERT INTO session_digests (sid,date,text,ts)"
        " VALUES ('s1','2026-05-16','content for the day','2026-05-16T10:00Z')"
    )
    conn.commit()
    fake = _RecordingLLM(per_role={"daily": "diary text",
                                   "daily_cand": ""})
    assert daily.run_day(conn, "2026-05-16", fake, db=p) is True
    body = fake.bodies.get("daily_cand", "")
    assert "AFFECT episodes for 2026-05-16:" in body
    assert "importance=5" in body
    assert "窘迫" in body
    assert "工签签证被拒" in body
    # diary call also receives the same material
    assert "importance=5" in fake.bodies.get("daily", "")
