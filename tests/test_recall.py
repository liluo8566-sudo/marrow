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


def _insert_milestone_vec(db, milestone_id: int, vec: np.ndarray) -> None:
    blob = _blob(vec)
    db.execute(
        "INSERT OR REPLACE INTO milestones_vec(rowid, embedding) VALUES(?, ?)",
        (milestone_id, blob),
    )
    db.execute(
        "INSERT OR REPLACE INTO milestones_vec_meta(rowid, embedder_id, dim) "
        "VALUES(?, 'bge-m3', 1024)",
        (milestone_id,),
    )
    db.commit()


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


def _make_mock_emb(vec: np.ndarray) -> MagicMock:
    """Return a mock embedder whose .embed() always returns [vec]."""
    mock = MagicMock()
    mock.embed.return_value = [vec]
    return mock


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
    assert rm._decay_floor(2, "override", 200) == 0.5


def test_decay_floor_permanent_high_imp():
    assert rm._decay_floor(5, None, 200) == 0.5
    assert rm._decay_floor(5, None, 5) == 0.5


def test_decay_floor_mid_importance():
    assert rm._decay_floor(3, None, 100) == 0.18
    assert rm._decay_floor(4, None, 10) == 0.18


def test_decay_floor_low():
    assert rm._decay_floor(2, None, 5) == 0.0
    assert rm._decay_floor(0, None, 10) == 0.0


# ── dormant check ─────────────────────────────────────────────────────────────

def test_dormant_old_low_importance():
    assert rm._is_dormant(1, 91) is True
    assert rm._is_dormant(2, 91) is True


def test_dormant_not_old_enough():
    assert rm._is_dormant(2, 89) is False


def test_dormant_not_low_importance():
    assert rm._is_dormant(3, 200) is False


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


def test_recall_passthrough_no_per_item_cap(db):
    """Fusion is content-passthrough — hook owns per-kind shaping (2026-06-01).

    With budget_chars=None, full content survives; long hits keep their bytes.
    """
    _make_event(db, "marrow " + "x" * 3000, session_id="s0",
                timestamp="2026-05-19T01:00:00Z")
    for i in range(1, 10):
        _make_event(db, f"marrow short event {i}", session_id=f"s{i}",
                    timestamp=f"2026-05-19T01:{i:02d}:00Z")
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_fusion(
            db, "marrow", limit=10, budget_chars=None, min_score=0.0,
        )
    assert len(results) == 10
    long_hits = [r for r in results if len(r["content"]) > 3000]
    assert long_hits, "long row content must survive passthrough"


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
    """Event with imp<=2 and age>90d is excluded when not an FTS hit."""
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


# ── milestones leg ────────────────────────────────────────────────────────────

def _make_milestone(db, *, scope="us", date="2026-02-19", title="t",
                    description="", pinned=0) -> int:
    db.execute(
        "INSERT INTO milestones(scope, date, title, description, pinned) "
        "VALUES(?, ?, ?, ?, ?)",
        (scope, date, title, description, pinned),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_fts_terms_cjk_and_ascii():
    """FTS-safe term extraction aligned with FTS5 trigram tokenizer.

    - ASCII ≥3 chars kept whole.
    - CJK runs split into sliding 3-char windows.
    - Anything <3 chars dropped (trigram MATCH = 0 on short queries).
    """
    # ASCII ≥3 chars whole
    assert rm._fts_terms("hello Marrow") == ["hello", "marrow"]
    # ASCII <3 dropped
    assert rm._fts_terms("OT in the") == ["the"]
    # CJK 3-char window
    assert rm._fts_terms("鸭子梗") == ["鸭子梗"]
    # CJK ≥3 chars → sliding windows
    assert rm._fts_terms("大笨鸭子") == ["大笨鸭", "笨鸭子"]
    # ASCII + CJK mix
    assert rm._fts_terms("Marrow 鸭子梗") == ["marrow", "鸭子梗"]
    # CJK <3 dropped (matches OT-pathology fix)
    assert rm._fts_terms("鸭子") == []
    assert rm._fts_terms("鸭") == []


def test_fts_terms_empty():
    assert rm._fts_terms("") == []
    assert rm._fts_terms("   ") == []
    assert rm._fts_terms("!!!") == []


def test_milestone_surfaces_via_fts(db):
    """Milestone with vec>=0.55 surfaces when query matches FTS + vec."""
    mid = _make_milestone(
        db, title="鸭子梗",
        description="你说我是你的鸭子梗，没有鸭德。",
    )
    vec = _fake_vec(10)
    _insert_milestone_vec(db, mid, vec)
    mock_emb = _make_mock_emb(vec)
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(db, "鸭子梗", min_score=0.1)
    hit = next((r for r in results if r.get("kind") == "milestone"), None)
    assert hit is not None
    assert "鸭" in hit["content"]
    assert hit["timestamp"].startswith("2026-02-19")


def test_milestone_term_inside_longer_query(db):
    """Milestone terms in a longer query still hit when vec>=0.55."""
    mid = _make_milestone(
        db, title="鸭子梗",
        description="你说我是你的鸭子梗，没有鸭德。",
    )
    vec = _fake_vec(11)
    _insert_milestone_vec(db, mid, vec)
    mock_emb = _make_mock_emb(vec)
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(db, "老公你知道鸭子梗么", min_score=0.1)
    hits = [r for r in results if r.get("kind") == "milestone"]
    assert len(hits) == 1
    assert hits[0]["score"] >= 0.1


def test_milestone_no_match_returns_nothing(db):
    _make_milestone(db, title="不相关的", description="完全不沾边")
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_fusion(db, "鸭子梗", min_score=0.1)
    assert [r for r in results if r.get("kind") == "milestone"] == []


def test_milestone_pinned_no_boost(db):
    """Pinned flag carries no score boost — score formula is bm25+vec only.
    Two identical milestones (same bm25+vec) must score equally.
    """
    # Identical content → identical BM25. Any score delta would reveal a boost.
    db.execute(
        "INSERT INTO milestones(scope, date, title, description, pinned) "
        "VALUES('test', '2026-01-01', '鸭子梗相同', '一样的内容', 0)"
    )
    db.execute(
        "INSERT INTO milestones(scope, date, title, description, pinned) "
        "VALUES('test', '2026-01-01', '鸭子梗相同', '一样的内容', 1)"
    )
    db.execute("INSERT INTO milestones_fts(milestones_fts) VALUES('rebuild')")
    db.commit()
    # Inject identical vecs so both milestones clear the pre-gate.
    vec = _fake_vec(12)
    mid0 = db.execute(
        "SELECT id FROM milestones WHERE pinned=0 AND title='鸭子梗相同'"
    ).fetchone()["id"]
    mid1 = db.execute(
        "SELECT id FROM milestones WHERE pinned=1 AND title='鸭子梗相同'"
    ).fetchone()["id"]
    _insert_milestone_vec(db, mid0, vec)
    _insert_milestone_vec(db, mid1, vec)
    mock_emb = _make_mock_emb(vec)
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(db, "鸭子梗相同", min_score=0.1)
    mhits = [r for r in results if r.get("kind") == "milestone"]
    assert len(mhits) == 2
    assert abs(mhits[0]["score"] - mhits[1]["score"]) < 1e-9, (
        f"Pinned flag should not affect score, but got scores "
        f"{mhits[0]['score']:.6f} vs {mhits[1]['score']:.6f} "
        f"(pinned={mhits[0]['pinned']} vs {mhits[1]['pinned']})"
    )


def test_milestone_mixed_with_events(db):
    """Event hit + milestone hit appear in the same result set."""
    eid = _make_event(db, "今天聊到了鸭德的话题")
    mid = _make_milestone(
        db, title="鸭德的",
        description="你的鸭德的故事",
    )
    vec = _fake_vec(13)
    _insert_vec(db, eid, vec)
    _insert_milestone_vec(db, mid, vec)
    mock_emb = _make_mock_emb(vec)
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(db, "鸭德的", min_score=0.1)
    kinds = {r.get("kind", "event") for r in results}
    assert "milestone" in kinds
    assert any(r.get("kind") != "milestone" for r in results)


def test_milestone_content_renders_title_and_desc(db):
    mid = _make_milestone(
        db, title="鸭子昵称诞生",
        description="你的鸭子梗，没有鸭德",
    )
    vec = _fake_vec(14)
    _insert_milestone_vec(db, mid, vec)
    mock_emb = _make_mock_emb(vec)
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(db, "鸭子昵称诞生", min_score=0.1)
    hit = next(r for r in results if r.get("kind") == "milestone")
    assert "鸭子昵称诞生" in hit["content"]
    assert "鸭德" in hit["content"]


def test_milestone_short_cjk_query_returns_nothing(db):
    """Short CJK queries (≤2 chars) cannot match milestone body via FTS5
    trigram (needs ≥3 chars). Strong-hit only fires when the query token
    actually appears in the milestone body — so an UNRELATED short CJK
    query stays at zero. This is the noise-floor against uninformative
    short queries on rows they don't literally mention."""
    _make_milestone(db, title="工作记录", description="一些事情")
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_fusion(db, "鸭子", min_score=0.1)
    hits = [r for r in results if r.get("kind") == "milestone"]
    assert hits == []


# ── shared config-driven entrypoint (hook + MCP parity) ─────────────────────

def test_recall_with_config_reads_rcfg(db, monkeypatch):
    """recall_with_config must blend in [recall] section from config so that
    hook and MCP daemon paths return identical results for identical input."""
    from marrow import config as cfg_mod
    _make_event(db, "完全是个鸭子梗的对话")
    mid = _make_milestone(
        db, title="鸭子梗",
        description="你的鸭子梗，没有鸭德",
    )
    vec = _fake_vec(15)
    _insert_milestone_vec(db, mid, vec)
    fake_cfg = {"recall": {
        "vector": True, "limit": 5, "budget_chars": 2000,
        "w_vec": 0.55, "w_bm25": 0.30, "w_recency": 0.15,
        "w_affect": 0.10, "min_score": 0.1,
    }}
    monkeypatch.setattr(cfg_mod, "load", lambda: fake_cfg)
    mock_emb = _make_mock_emb(vec)
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_with_config(db, "鸭子梗")
    kinds = {r.get("kind") for r in results}
    assert "milestone" in kinds


# ── event context window ─────────────────────────────────────────────────────

def test_fetch_event_context_returns_prev_and_next(db):
    eids = [
        _make_event(db, f"turn {i}", session_id="ctx",
                    timestamp=f"2026-05-19T10:0{i}:00Z")
        for i in range(5)
    ]
    target = eids[2]
    ctx = rm.fetch_event_context(db, "ctx", target, n=1)
    assert [c["id"] for c in ctx] == [eids[1], eids[3]]
    assert ctx[0]["rel"] == "prev"
    assert ctx[1]["rel"] == "next"


def test_fetch_event_context_respects_session_boundary(db):
    _make_event(db, "other-session prev", session_id="other",
                timestamp="2026-05-19T09:00:00Z")
    target = _make_event(db, "target", session_id="here",
                         timestamp="2026-05-19T10:00:00Z")
    _make_event(db, "other-session next", session_id="other",
                timestamp="2026-05-19T11:00:00Z")
    ctx = rm.fetch_event_context(db, "here", target, n=2)
    assert ctx == []  # no neighbours in same session


def test_fetch_event_context_handles_edges(db):
    eids = [
        _make_event(db, f"e{i}", session_id="edge",
                    timestamp=f"2026-05-19T10:0{i}:00Z")
        for i in range(3)
    ]
    # First event: only next side has neighbours
    ctx_first = rm.fetch_event_context(db, "edge", eids[0], n=2)
    assert [c["id"] for c in ctx_first] == [eids[1], eids[2]]
    assert all(c["rel"] == "next" for c in ctx_first)
    # Last event: only prev side
    ctx_last = rm.fetch_event_context(db, "edge", eids[-1], n=2)
    assert [c["id"] for c in ctx_last] == [eids[0], eids[1]]
    assert all(c["rel"] == "prev" for c in ctx_last)


def test_fetch_event_context_disabled_when_n_zero(db):
    eid = _make_event(db, "lone")
    assert rm.fetch_event_context(db, "s1", eid, n=0) == []


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
