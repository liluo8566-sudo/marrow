"""Regression tests for entity-recall + memes-leg + milestone live paths.

Tests directly exercising entity_force_include (now deleted) have been removed.
Live-path tests (_entity_candidates + fusion, memes FTS leg, milestone FTS leg,
body_nonempty helper) are kept.
"""
from __future__ import annotations

import datetime as dt
import struct
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from marrow import recall as rm, repo, storage


def _fake_vec(seed: int, dim: int = 1024) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.random(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _blob(v: np.ndarray) -> bytes:
    return struct.pack(f"{len(v)}f", *v.tolist())


def _insert_memes_vec(db, meme_id: int, vec: np.ndarray) -> None:
    blob = _blob(vec)
    db.execute(
        "INSERT OR REPLACE INTO memes_vec(rowid, embedding) VALUES(?, ?)",
        (meme_id, blob),
    )
    db.execute(
        "INSERT OR REPLACE INTO memes_vec_meta(rowid, embedder_id, dim) "
        "VALUES(?, 'bge-m3', 1024)",
        (meme_id,),
    )
    db.commit()


@pytest.fixture()
def seeded_db(tmp_path):
    conn = storage.init_db(str(tmp_path / "ev.db"))
    base = dt.datetime(2026, 5, 1, 10, 0, 0, tzinfo=dt.timezone.utc)

    def add_event(sid: str, content: str, hours: int = 0) -> int:
        ts = (base + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        repo.archive_events(conn, [{
            "session_id": sid, "timestamp": ts,
            "role": "user", "content": content,
        }])
        return conn.execute(
            "SELECT id FROM events WHERE session_id=?", (sid,)
        ).fetchone()["id"]

    # ASCII entity (Amber) — used by FTS live-path test.
    amber_eid = add_event("s_amber_1", "Amber 今天去 ED 帮忙，超累。", 8)
    conn.execute(
        "INSERT INTO entities (kind, name, mention_count, source) "
        "VALUES ('person', 'Amber', 3, 'test')"
    )

    # Memes row: cipher Plan price.
    conn.execute(
        "INSERT INTO memes (type, key, value, context, use_count, status) "
        "VALUES ('cipher', 'Plan', 'Max 5x · $100/mo', "
        "'Anthropic plan tier', 4, 'active')"
    )

    # Milestone: title (Bendigo placement).
    conn.execute(
        "INSERT INTO milestones (scope, date, title, description, pinned) "
        "VALUES ('career', '2015-08-01', 'Bendigo placement', "
        "'Failed placement 2015', 0)"
    )

    # Noise events.
    for i in range(15):
        add_event(f"s_noise_{i}", f"完全不相关的内容编号 {i}", 50 + i)

    conn.commit()
    yield conn, amber_eid
    conn.close()


def test_memes_leg_returns_cipher_on_query_match(seeded_db):
    """recall_fusion must surface memes row when key matches query (FTS+vec)."""
    conn, _ = seeded_db
    meme_id = conn.execute(
        "SELECT id FROM memes WHERE key='Plan'"
    ).fetchone()["id"]
    vec = _fake_vec(20)
    _insert_memes_vec(conn, meme_id, vec)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = [vec]
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(conn, "我的 plan 是什么价钱", limit=10)
    memes_hits = [
        r for r in results
        if r.get("kind") == "memes" and "Plan" in (r.get("content") or "")
    ]
    assert len(memes_hits) >= 1, (
        f"Expected memes kind row containing (Plan), got: "
        f"{[(r.get('kind'), r.get('content')) for r in results]}"
    )


def test_milestone_reverse_substring_match(seeded_db):
    """Milestone title (Bendigo placement) must hit via FTS candidates."""
    conn, _ = seeded_db
    cands = rm._milestone_candidates(
        conn, "今天 Bendigo placement 怎么样", limit=10
    )
    bendigo = [c for c in cands if c["content"].startswith("Bendigo")]
    assert bendigo, f"Expected Bendigo milestone candidate, got {cands}"
    assert bendigo[0]["bm25"] == 1.0, (
        f"Expected kw_score=1.0 (best rank), got {bendigo[0]['bm25']}"
    )


# ── body_nonempty filter ──────────────────────────────────────────────────────

def test_body_nonempty_unit():
    """None / empty / whitespace-only → False; anything with a char → True."""
    from marrow.recall import _body_nonempty
    assert _body_nonempty(None) is False
    assert _body_nonempty("") is False
    assert _body_nonempty("   ") is False
    assert _body_nonempty("\n\t ") is False
    assert _body_nonempty("x") is True
    assert _body_nonempty(" hello ") is True
    assert _body_nonempty(["non-string"]) is True


def test_entity_candidates_whitespace_fact_dropped(tmp_path):
    """Entity with whitespace-only fact must not surface as an entity card
    in recall output — the _entity_candidates FTS scan drops empty facts,
    and the vec-only path also skips them.
    """
    conn = storage.init_db(str(tmp_path / "f.db"))
    try:
        base = dt.datetime(2026, 5, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
        repo.archive_events(conn, [{
            "session_id": "good-sid",
            "timestamp": base.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "role": "user",
            "content": "Zara helped me debug recall today",
        }])
        conn.execute(
            "INSERT INTO entities (kind, name, fact, mention_count, source) "
            "VALUES ('person', 'Zara', '   ', 1, 'test')"
        )
        conn.commit()

        with patch.object(rm, "_ensure_embedder", return_value=None):
            results = rm.recall_fusion(conn, "Zara", limit=10, min_score=0.1)

        for r in results:
            content = r.get("content") or ""
            assert content.strip(), (
                f"recall returned whitespace-body row: {r!r}"
            )
            if r.get("kind") == "entity":
                if ": " in content:
                    fact_part = content.rsplit(": ", 1)[1]
                    assert fact_part.strip(), (
                        f"entity card carries whitespace-only fact: {r!r}"
                    )
        assert any("Zara helped" in (r.get("content") or "")
                   for r in results), f"no FTS event in {results!r}"
    finally:
        conn.close()
