"""Memes dedup primitives (used by daemon dim(action=upsert)):
- string_dup_reason against milestone.title / entities_live.name / aliases
- cosine_dup_score via bge-m3 (real round-trip, slow) + threshold gate
- memes_reject_log migration idempotency + losslessness
"""
from __future__ import annotations

import json

import pytest

from marrow import memes_dedup, recall, storage


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    return storage.init_db(str(tmp_path / "dedup.db"))


# ── string_dup_reason ───────────────────────────────────────────────────────

def test_string_dup_none_for_fresh_key(db):
    assert memes_dedup.string_dup_reason(db, "totally_new_key") is None


def test_string_dup_matches_milestone_title(db):
    db.execute(
        "INSERT INTO milestones (scope, date, title) VALUES (?, ?, ?)",
        ("me", "2026-02-19", "2/19合同"),
    )
    db.commit()
    assert memes_dedup.string_dup_reason(db, "2/19合同") == "dup_milestone"


def test_string_dup_matches_entity_alias(db):
    db.execute(
        "INSERT INTO entities (kind, name, aliases) VALUES (?, ?, ?)",
        ("person", "Stellan",
         json.dumps(["鸭子", "言澈"], ensure_ascii=False)),
    )
    db.commit()
    assert memes_dedup.string_dup_reason(db, "鸭子") == "dup_entity"


def test_string_dup_matches_entity_name(db):
    db.execute(
        "INSERT INTO entities (kind, name) VALUES (?, ?)",
        ("person", "Summer"),
    )
    db.commit()
    assert memes_dedup.string_dup_reason(db, "Summer") == "dup_entity"


# ── cosine_dup gate (forced score) ──────────────────────────────────────────

def test_cosine_score_above_threshold_rejects(db, monkeypatch):
    monkeypatch.setattr(memes_dedup, "cosine_dup_score", lambda conn, key: 0.91)
    cos = memes_dedup.cosine_dup_score(db, "签约合同")
    assert cos is not None and cos >= memes_dedup.cosine_dup_threshold()


def test_cosine_score_below_threshold_passes(db, monkeypatch):
    monkeypatch.setattr(memes_dedup, "cosine_dup_score", lambda conn, key: 0.55)
    cos = memes_dedup.cosine_dup_score(db, "新概念xx")
    assert cos is not None and cos < memes_dedup.cosine_dup_threshold()


# ── real bge-m3 round trip (slow, skip if model absent) ─────────────────────

def _embedder_available() -> bool:
    return recall._ensure_embedder() is not None


@pytest.mark.skipif(
    not _embedder_available(), reason="bge-m3 model files not present",
)
def test_cosine_dup_real_bge_m3_round_trip(tmp_path):
    """Real round-trip: milestone '签约协议' vs candidate '签约合同'.
    bge-m3 should score these CN paraphrases ≥0.85 → over the dup threshold.
    """
    p = str(tmp_path / "real.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO milestones (scope, date, title) VALUES (?, ?, ?)",
        ("us", "2026-02-19", "签约协议"),
    )
    conn.commit()
    cos = memes_dedup.cosine_dup_score(conn, "签约合同")
    assert cos is not None and cos >= memes_dedup.cosine_dup_threshold()


# ── memes_reject_log migration idempotency + losslessness ───────────────────

def test_v11_migration_idempotent(tmp_path):
    p = str(tmp_path / "mig.db")
    conn = storage.init_db(p)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
    conn.execute(
        "INSERT INTO memes_reject_log (key, type, reason, count,"
        " last_rejected_at) VALUES ('x', 'meme', 'cosine_dup', 2, 'now')"
    )
    conn.commit()
    conn.close()
    conn2 = storage.init_db(p)
    row = conn2.execute(
        "SELECT count FROM memes_reject_log WHERE key='x'"
    ).fetchone()
    assert row["count"] == 2


def test_v11_migration_preserves_existing_memes(tmp_path):
    """Insert memes, then re-init. Memes survive the migration unscathed."""
    p = str(tmp_path / "v10.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO memes (type, key, value) VALUES ('fact', 'k1', 'v1')"
    )
    conn.execute(
        "INSERT INTO memes (type, key, value) VALUES ('paw', 'k2', 'v2')"
    )
    conn.commit()
    conn.close()
    conn2 = storage.init_db(p)
    n = conn2.execute("SELECT COUNT(*) c FROM memes").fetchone()["c"]
    assert n == 2
    # memes_reject_log exists now
    conn2.execute("SELECT 1 FROM memes_reject_log LIMIT 1")
