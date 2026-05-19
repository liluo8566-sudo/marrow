"""Tests for marrow/recall.py: embed, fusion, decay, dedup."""
from __future__ import annotations

import math
import struct
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from marrow import recall as rm, repo, storage


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    yield conn
    conn.close()


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


def _fake_vec(seed: int, dim: int = 1024) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.random(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _blob(v: np.ndarray) -> bytes:
    return struct.pack(f"{len(v)}f", *v.tolist())


def _insert_vec(db, event_id: int, vec: np.ndarray) -> None:
    """Manually insert a pre-computed vector."""
    blob = _blob(vec)
    db.execute(
        "INSERT OR IGNORE INTO events_vec(rowid, embedding) VALUES(?, ?)",
        (event_id, blob),
    )
    db.execute(
        "INSERT OR IGNORE INTO events_vec_meta(rowid, embedder_id, dim) "
        "VALUES(?, 'bge-m3', 1024)",
        (event_id,),
    )
    db.commit()


# ── vec serialization ─────────────────────────────────────────────────────────

def test_vec_roundtrip():
    v = _fake_vec(42)
    blob = rm._vec_to_blob(v)
    v2 = rm._blob_to_vec(blob)
    assert len(blob) == 1024 * 4
    assert np.allclose(v, v2, atol=1e-6)


# ── embed_event ───────────────────────────────────────────────────────────────

def test_embed_event_skips_when_no_embedder(db):
    eid = _make_event(db, "hello world")
    with patch.object(rm, "_ensure_embedder", return_value=None):
        result = rm.embed_event(db, eid, "hello world")
    assert result is False
    assert db.execute("SELECT COUNT(*) FROM events_vec_meta").fetchone()[0] == 0


def test_embed_event_writes_vec_and_meta(db):
    eid = _make_event(db, "embed me")
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([_fake_vec(1)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        result = rm.embed_event(db, eid, "embed me")
    assert result is True
    meta = db.execute("SELECT * FROM events_vec_meta WHERE rowid=?", (eid,)).fetchone()
    assert meta is not None
    assert meta["embedder_id"] == "bge-m3"
    assert meta["dim"] == 1024


def test_embed_event_idempotent(db):
    eid = _make_event(db, "idempotent test")
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([_fake_vec(2)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        rm.embed_event(db, eid, "idempotent test")
        result2 = rm.embed_event(db, eid, "idempotent test")
    assert result2 is False  # skipped
    count = db.execute("SELECT COUNT(*) FROM events_vec_meta WHERE rowid=?", (eid,)).fetchone()[0]
    assert count == 1


# ── embed_pending ─────────────────────────────────────────────────────────────

def test_embed_pending_returns_zero_without_embedder(db):
    _make_event(db, "pending event")
    with patch.object(rm, "_ensure_embedder", return_value=None):
        n = rm.embed_pending(db)
    assert n == 0


def test_embed_pending_embeds_unvectorized(db):
    _make_event(db, "event one")
    _make_event(db, "event two")
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([_fake_vec(3), _fake_vec(4)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        n = rm.embed_pending(db, batch=10)
    assert n == 2
    assert db.execute("SELECT COUNT(*) FROM events_vec_meta").fetchone()[0] == 2


def test_embed_pending_skips_already_embedded(db):
    eid = _make_event(db, "already embedded")
    v = _fake_vec(5)
    _insert_vec(db, eid, v)
    mock_emb = MagicMock()
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        n = rm.embed_pending(db, batch=10)
    assert n == 0
    mock_emb.embed.assert_not_called()


# ── recency score ─────────────────────────────────────────────────────────────

def test_recency_score_recent():
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    score = rm._recency_score(ts)
    assert score > 0.99  # exp(-0) ≈ 1


def test_recency_score_30d():
    import datetime as dt
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30))
    ts = past.strftime("%Y-%m-%dT%H:%M:%SZ")
    score = rm._recency_score(ts)
    assert abs(score - math.exp(-1)) < 0.05


def test_recency_score_bad_timestamp():
    score = rm._recency_score("not-a-date")
    assert score == math.exp(0)  # days=0 fallback


# ── decay floor ───────────────────────────────────────────────────────────────

def test_decay_floor_permanent_source_override():
    assert rm._decay_floor(3, "override", 200) == 0.5


def test_decay_floor_permanent_high_imp():
    assert rm._decay_floor(8, None, 200) == 0.5
    assert rm._decay_floor(10, None, 5) == 0.5


def test_decay_floor_mid_importance():
    assert rm._decay_floor(4, None, 100) == 0.18
    assert rm._decay_floor(7, None, 10) == 0.18


def test_decay_floor_low():
    assert rm._decay_floor(3, None, 5) == 0.0
    assert rm._decay_floor(0, None, 10) == 0.0


# ── dormant check ─────────────────────────────────────────────────────────────

def test_dormant_old_low_importance():
    assert rm._is_dormant(2, 91) is True
    assert rm._is_dormant(3, 91) is True


def test_dormant_not_old_enough():
    assert rm._is_dormant(2, 89) is False


def test_dormant_not_low_importance():
    assert rm._is_dormant(4, 200) is False


def test_dormant_none_importance():
    assert rm._is_dormant(None, 200) is True  # None treated as 0


# ── recall_fusion — FTS-only path (no embedder) ───────────────────────────────

def test_recall_fusion_empty_query(db):
    assert rm.recall_fusion(db, "") == []
    assert rm.recall_fusion(db, "   ") == []


def test_recall_fusion_fts_hit(db):
    _make_event(db, "hello marrow world")
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_fusion(db, "marrow")
    assert len(results) == 1
    assert results[0]["content"] == "hello marrow world"
    assert "score" in results[0]


def test_recall_fusion_score_above_min(db):
    _make_event(db, "hello marrow world")
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_fusion(db, "marrow", min_score=0.0)
    assert results[0]["score"] >= 0.0


def test_recall_fusion_budget_truncation(db):
    _make_event(db, "x" * 3000, timestamp="2026-05-19T01:00:00Z")
    _make_event(db, "x" * 3000, session_id="s2", timestamp="2026-05-19T01:01:00Z")
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_fusion(db, "x", limit=10, budget_chars=4000, min_score=0.0)
    total = sum(len(r["content"]) for r in results)
    assert total <= 4000


# ── recall_fusion — vec path ──────────────────────────────────────────────────

def test_recall_fusion_with_vec(db):
    qvec = _fake_vec(10)
    eid = _make_event(db, "target event for vector search")
    close_vec = qvec.copy()
    _insert_vec(db, eid, close_vec)

    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(db, "target event", min_score=0.0)
    ids = [r["id"] for r in results]
    assert eid in ids


def test_recall_fusion_floor_applied(db):
    """Event with source=override and low raw score still gets floor=0.5."""
    eid = _make_event(db, "override event source floor test")
    db.execute(
        "INSERT INTO affect(date,ep,event_id,valence,arousal,importance,source) "
        "VALUES('2026-05-19',1,?,0.5,0.3,10,'override')",
        (eid,),
    )
    db.commit()
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_fusion(db, "override", min_score=0.0)
    if results:
        assert results[0]["score"] >= 0.5


# ── dormant revival ───────────────────────────────────────────────────────────

def test_recall_fusion_dormant_excluded_without_fts(db):
    """Event with imp<=3 and age>90d is excluded when not an FTS hit."""
    import datetime as dt
    old_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=100))
    ts_str = old_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    eid = _make_event(db, "ancient dormant event zzz", timestamp=ts_str)
    db.execute(
        "INSERT INTO affect(date,ep,event_id,valence,arousal,importance) "
        "VALUES('2026-01-01',1,?,0.1,0.1,1)",
        (eid,),
    )
    db.commit()
    qvec = _fake_vec(20)
    _insert_vec(db, eid, qvec)

    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        # query that won't FTS-match "ancient dormant event zzz"
        results = rm.recall_fusion(db, "completely unrelated query", min_score=0.0)
    ids = [r["id"] for r in results]
    assert eid not in ids


# ── repo.recall delegates to fusion ──────────────────────────────────────────

def test_repo_recall_delegates_to_fusion(db):
    _make_event(db, "delegation test content")
    with patch.object(rm, "recall_fusion") as mock_fusion:
        mock_fusion.return_value = [{"id": 1, "content": "x", "score": 0.8}]
        result = repo.recall(db, "delegation test")
    mock_fusion.assert_called_once()
    assert result == [{"id": 1, "content": "x", "score": 0.8}]


# ── daemon tools ──────────────────────────────────────────────────────────────

def test_daemon_embed_pending_callable():
    import marrow.daemon as daemon
    assert callable(daemon.embed_pending)


def test_daemon_embed_pending_returns_dict(tmp_path, monkeypatch):
    import marrow.daemon as daemon
    p = str(tmp_path / "d.db")
    storage.init_db(p).close()
    monkeypatch.setattr(daemon, "_DB", p)
    with patch.object(rm, "_ensure_embedder", return_value=None):
        result = daemon.embed_pending()
    assert isinstance(result, dict)
    assert "embedded" in result
