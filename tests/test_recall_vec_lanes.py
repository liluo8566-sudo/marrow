"""Tests for cross-table vec lanes (memes / entities / milestones) + entity card sort.

Covers:
- embed_meme / embed_entity / embed_milestone write + idempotency.
- embed_pending backfills all four lanes; skips already-embedded / inactive /
  superseded rows.
- recall_fusion surfaces memes / milestones / entity-card hits from the vec lanes.
- _VEC_ONLY_FLOOR gates low-similarity vec-only hits.
- vec lane is a no-op when the embedder is unavailable.
"""
from __future__ import annotations

import struct
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from marrow import recall as rm, repo, storage


# ── fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    yield conn
    conn.close()


def _fake_vec(seed: int, dim: int = 1024) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.random(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _make_event(db, content: str, session_id: str = "s1",
                timestamp: str = "2026-05-19T10:00:00Z") -> int:
    repo.archive_events(db, [{
        "session_id": session_id,
        "timestamp": timestamp,
        "role": "user",
        "content": content,
    }])
    return db.execute(
        "SELECT id FROM events WHERE content=?", (content,)
    ).fetchone()["id"]


def _make_meme(db, key: str, value: str = "v", status: str = "active") -> int:
    db.execute(
        "INSERT INTO memes(type, key, value, status) VALUES('cipher', ?, ?, ?)",
        (key, value, status),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _make_entity(db, name: str, fact: str = "some fact",
                 kind: str = "person", mention_count: int = 1,
                 superseded_by: int | None = None) -> int:
    db.execute(
        "INSERT INTO entities(kind, name, fact, mention_count, source, superseded_by)"
        " VALUES(?, ?, ?, ?, 'test', ?)",
        (kind, name, fact, mention_count, superseded_by),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _make_milestone(db, title: str, description: str = "d",
                    date: str = "2026-02-19", scope: str = "us") -> int:
    db.execute(
        "INSERT INTO milestones(scope, date, title, description) VALUES(?, ?, ?, ?)",
        (scope, date, title, description),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── A. embed write path ──────────────────────────────────────────────────────

def test_embed_meme_writes_and_idempotent(db):
    mid = _make_meme(db, "plan")
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([_fake_vec(1)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        assert rm.embed_meme(db, mid, "plan") is True
        assert rm.embed_meme(db, mid, "plan") is False
    assert db.execute(
        "SELECT COUNT(*) FROM memes_vec_meta WHERE rowid=?", (mid,)
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM memes_vec WHERE rowid=?", (mid,)
    ).fetchone()[0] == 1


def test_embed_entity_writes_and_idempotent(db):
    eid = _make_entity(db, "南南", fact="best friend")
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([_fake_vec(2)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        assert rm.embed_entity(db, eid, "南南: best friend") is True
        assert rm.embed_entity(db, eid, "南南: best friend") is False
    assert db.execute(
        "SELECT COUNT(*) FROM entities_vec_meta WHERE rowid=?", (eid,)
    ).fetchone()[0] == 1


def test_embed_milestone_writes_and_idempotent(db):
    mid = _make_milestone(db, "Bendigo placement", description="failed 2015")
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([_fake_vec(3)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        assert rm.embed_milestone(db, mid, "Bendigo placement") is True
        assert rm.embed_milestone(db, mid, "Bendigo placement") is False
    assert db.execute(
        "SELECT COUNT(*) FROM milestones_vec_meta WHERE rowid=?", (mid,)
    ).fetchone()[0] == 1


def test_embed_pending_backfills_all_four_lanes(db):
    _make_event(db, "event row")
    _make_meme(db, "meme-key")
    _make_entity(db, "Stellan", fact="partner")
    _make_milestone(db, "milestone-title")
    mock_emb = MagicMock()
    mock_emb.embed.side_effect = lambda texts: np.stack(
        [_fake_vec(100 + i) for i, _ in enumerate(texts)]
    )
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        n = rm.embed_pending(db, batch=10)
    assert n == 4
    for tbl in ("events_vec_meta", "memes_vec_meta",
                "entities_vec_meta", "milestones_vec_meta"):
        assert db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] == 1


def test_embed_pending_idempotent_second_run(db):
    _make_event(db, "ev1")
    _make_meme(db, "mk")
    mock_emb = MagicMock()
    mock_emb.embed.side_effect = lambda texts: np.stack(
        [_fake_vec(200 + i) for i, _ in enumerate(texts)]
    )
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        first = rm.embed_pending(db, batch=10)
        second = rm.embed_pending(db, batch=10)
    assert first == 2
    assert second == 0


def test_embed_pending_skips_inactive_and_superseded(db):
    # Active meme + dormant meme; live entity + superseded entity.
    live_mid = _make_meme(db, "live-key", status="active")
    _make_meme(db, "dormant-key", status="dormant")
    live_eid = _make_entity(db, "Live", fact="alive")
    _make_entity(db, "Dead", fact="gone", superseded_by=live_eid)
    mock_emb = MagicMock()
    mock_emb.embed.side_effect = lambda texts: np.stack(
        [_fake_vec(300 + i) for i, _ in enumerate(texts)]
    )
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        rm.embed_pending(db, batch=10)
    meme_rowids = {r["rowid"] for r in db.execute(
        "SELECT rowid FROM memes_vec_meta"
    ).fetchall()}
    ent_rowids = {r["rowid"] for r in db.execute(
        "SELECT rowid FROM entities_vec_meta"
    ).fetchall()}
    assert meme_rowids == {live_mid}
    assert ent_rowids == {live_eid}


# ── B. recall_fusion vec lanes ────────────────────────────────────────────────

def test_memes_vec_lane_surfaces_via_monkeypatch(db, monkeypatch):
    mid = _make_meme(db, "snowleopard", value="form name")
    qvec = _fake_vec(11)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])
    monkeypatch.setattr(rm, "_memes_vec_hits", lambda c, b, k: {mid: 0.7})
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(
            db, "completely different query", min_score=0.0,
        )
    memes_hits = [r for r in results if r.get("kind") == "memes"]
    assert len(memes_hits) >= 1
    assert "snowleopard" in memes_hits[0]["content"]


def test_milestones_vec_lane_surfaces_via_monkeypatch(db, monkeypatch):
    mid = _make_milestone(db, "first kiss", description="evergreen anchor")
    qvec = _fake_vec(12)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])
    monkeypatch.setattr(rm, "_milestones_vec_hits", lambda c, b, k: {mid: 0.65})
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(
            db, "unrelated query string", min_score=0.0,
        )
    ms_hits = [r for r in results if r.get("kind") == "milestone"]
    assert len(ms_hits) >= 1
    assert ms_hits[0]["score"] > 0


def test_entities_vec_lane_prepends_entity_card(db, monkeypatch):
    eid = _make_entity(db, "Sushi", fact="favourite restaurant", kind="place")
    qvec = _fake_vec(13)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])
    fake_card = {
        "id": eid, "kind": "place", "name": "Sushi",
        "fact": "favourite restaurant", "mention_count": 2,
        "created_at": "2026-05-01T00:00:00Z", "vec_score": 0.7,
    }
    monkeypatch.setattr(rm, "_entities_vec_hits", lambda c, b, k: [fake_card])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(
            db, "where should we eat tonight", min_score=0.0,
        )
    ent_hits = [r for r in results if r.get("kind") == "entity"]
    assert len(ent_hits) >= 1
    assert "Sushi" in ent_hits[0]["content"]
    assert "favourite restaurant" in ent_hits[0]["content"]


def test_vec_only_floor_gates_low_similarity(db, monkeypatch):
    mid = _make_meme(db, "lowfloor", value="x")
    qvec = _fake_vec(14)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])
    # 0.30 < _VEC_ONLY_FLOOR (0.40)
    monkeypatch.setattr(rm, "_memes_vec_hits", lambda c, b, k: {mid: 0.30})
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(
            db, "unrelated noise query", min_score=0.0,
        )
    assert not any(r.get("kind") == "memes" and r.get("id") == mid
                   for r in results)


def test_vec_lane_no_op_without_embedder(db):
    _make_meme(db, "vec-disabled-key")
    _make_entity(db, "VecDisabled", fact="x")
    _make_milestone(db, "no vec milestone")
    with patch.object(rm, "_ensure_embedder", return_value=None):
        # Should not raise; no kw substring match on this query either.
        results = rm.recall_fusion(
            db, "wholly orthogonal sentence here", min_score=0.0,
        )
    # No memes / entity / milestone vec-only rows surface.
    assert not any(r.get("kind") in ("memes", "milestone", "entity")
                   for r in results)
