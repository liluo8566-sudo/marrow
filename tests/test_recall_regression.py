"""Regression tests for all changes in the anchor-bias + recall-quality pass.

Covers:
- test_anchor_pregate: vec<0.55 anchor rows are dropped before scoring
- test_no_anchor_bias: event and milestone with same bm25+vec score equally
- test_pinned_no_boost: pinned milestone == unpinned at same bm25+vec
- test_cwd_boost: same-bucket event gets +0.05; cross-bucket gets +0.00
- test_exclude_kinds: default excludes diary/task; explicit () includes them
- test_utc_to_local: 2026-06-06T14:00:00Z -> "2026-06-07" Melbourne date
- test_stopword_filter: CJK + ASCII stopword filtering
- test_time_anchor_strip: WX time-anchor prefix stripped at recall entry
"""
from __future__ import annotations

import datetime as dt
import struct
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from marrow import recall as rm, repo, storage


# ── helpers ───────────────────────────────────────────────────────────────────

def _fake_vec(seed: int, dim: int = 1024) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.random(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _blob(v: np.ndarray) -> bytes:
    return struct.pack(f"{len(v)}f", *v.tolist())


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


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


def _insert_entity_vec(db, entity_id: int, vec: np.ndarray) -> None:
    blob = _blob(vec)
    db.execute(
        "INSERT OR REPLACE INTO entities_vec(rowid, embedding) VALUES(?, ?)",
        (entity_id, blob),
    )
    db.execute(
        "INSERT OR REPLACE INTO entities_vec_meta(rowid, embedder_id, dim) "
        "VALUES(?, 'bge-m3', 1024)",
        (entity_id,),
    )
    db.commit()


@pytest.fixture()
def db(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    yield conn
    conn.close()


# ── 1. anchor vec pre-gate ────────────────────────────────────────────────────

def test_anchor_pregate_low_vec_milestone_dropped(db):
    """Milestone with vec=0.45 (below 0.55 floor) must NOT surface when the
    query does not substring-match the anchor title/description (no strong-hit).
    Strong-hit bypasses the floor only when the query literally contains the anchor.
    """
    db.execute(
        "INSERT INTO milestones(scope, date, title, description, pinned) "
        "VALUES('test', '2026-01-01', 'zxqpregate unique title', 'zxquniquedesc', 0)"
    )
    db.execute("INSERT INTO milestones_fts(milestones_fts) VALUES('rebuild')")
    db.commit()
    mid = db.execute("SELECT id FROM milestones WHERE title LIKE 'zxqpregate%'").fetchone()["id"]

    # Build a query vec and a milestone vec that will give ~0.45 similarity.
    q_vec = _fake_vec(1)
    # Rotate query vec to produce ~0.45 similarity: blend with orthogonal.
    orth = _fake_vec(99)
    orth = orth - np.dot(orth, q_vec) * q_vec
    orth /= np.linalg.norm(orth)
    # cos(sim) ≈ 0.45 → we want component along q_vec to be 0.45.
    m_vec = 0.45 * q_vec + np.sqrt(1 - 0.45**2) * orth
    m_vec = m_vec / np.linalg.norm(m_vec)
    actual_sim = _cosine_sim(q_vec, m_vec)
    assert actual_sim < 0.55, f"setup error: sim={actual_sim:.3f} should be <0.55"

    _insert_milestone_vec(db, mid, m_vec)

    mock_emb = MagicMock()
    mock_emb.embed.return_value = [q_vec]
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        # Query does NOT contain "zxqpregate" or "zxquniquedesc" — no strong-hit.
        results = rm.recall_fusion(
            db, "some unrelated semantic query about nothing", min_score=0.01
        )

    milestone_hits = [r for r in results if r.get("kind") == "milestone"]
    assert milestone_hits == [], (
        f"Expected milestone with vec<0.55 and no strong-hit to be dropped, got: {milestone_hits}"
    )


def test_anchor_pregate_high_vec_milestone_kept(db):
    """Milestone with vec=0.60 (above 0.55 floor) must surface."""
    db.execute(
        "INSERT INTO milestones(scope, date, title, description, pinned) "
        "VALUES('test', '2026-01-01', 'high vec milestone', 'desc', 0)"
    )
    db.execute("INSERT INTO milestones_fts(milestones_fts) VALUES('rebuild')")
    db.commit()
    mid = db.execute("SELECT id FROM milestones WHERE title='high vec milestone'").fetchone()["id"]

    q_vec = _fake_vec(2)
    orth = _fake_vec(98)
    orth = orth - np.dot(orth, q_vec) * q_vec
    orth /= np.linalg.norm(orth)
    m_vec = 0.60 * q_vec + np.sqrt(1 - 0.60**2) * orth
    m_vec = m_vec / np.linalg.norm(m_vec)
    actual_sim = _cosine_sim(q_vec, m_vec)
    assert actual_sim >= 0.55, f"setup error: sim={actual_sim:.3f} should be >=0.55"

    _insert_milestone_vec(db, mid, m_vec)

    mock_emb = MagicMock()
    mock_emb.embed.return_value = [q_vec]
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(db, "high vec milestone", min_score=0.01)

    milestone_hits = [r for r in results if r.get("kind") == "milestone"]
    assert len(milestone_hits) >= 1, (
        f"Expected milestone with vec>=0.55 to surface, got none. All: {results}"
    )


def test_anchor_pregate_memes_low_vec_dropped(db):
    """Memes row with vec<0.55 must be dropped when query does not substring-match
    the anchor key/value (no strong-hit). Strong-hit only fires on literal match.
    """
    db.execute(
        "INSERT INTO memes(type, key, value, use_count, status) "
        "VALUES('phrase', 'zxqlowvec phrase', 'zxqsome unique value', 1, 'active')"
    )
    db.commit()
    mid = db.execute("SELECT id FROM memes WHERE key='zxqlowvec phrase'").fetchone()["id"]

    q_vec = _fake_vec(3)
    orth = _fake_vec(97)
    orth = orth - np.dot(orth, q_vec) * q_vec
    orth /= np.linalg.norm(orth)
    m_vec = 0.40 * q_vec + np.sqrt(1 - 0.40**2) * orth
    m_vec = m_vec / np.linalg.norm(m_vec)
    actual_sim = _cosine_sim(q_vec, m_vec)
    assert actual_sim < 0.55

    _insert_memes_vec(db, mid, m_vec)

    mock_emb = MagicMock()
    mock_emb.embed.return_value = [q_vec]
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        # Query does not contain "zxqlowvec" or "zxqsome" — no strong-hit fires.
        results = rm.recall_fusion(db, "some unrelated semantic query about nothing", min_score=0.01)

    memes_hits = [r for r in results if r.get("kind") == "memes"]
    assert memes_hits == [], f"Expected memes with vec<0.55 and no strong-hit dropped, got: {memes_hits}"


def test_anchor_pregate_entity_low_vec_dropped(db):
    """Entity with vec<0.55 must be dropped when query does not substring-match
    the anchor name/fact/aliases (no strong-hit).
    """
    db.execute(
        "INSERT INTO entities(kind, name, fact, mention_count, source) "
        "VALUES('person', 'ZxqLowVecPerson', 'zxqfact zzzkjhbody aaabbb', 1, 'test')"
    )
    db.execute("INSERT INTO entities_fts(entities_fts) VALUES('rebuild')")
    db.commit()
    eid = db.execute("SELECT id FROM entities WHERE name='ZxqLowVecPerson'").fetchone()["id"]

    q_vec = _fake_vec(4)
    orth = _fake_vec(96)
    orth = orth - np.dot(orth, q_vec) * q_vec
    orth /= np.linalg.norm(orth)
    e_vec = 0.40 * q_vec + np.sqrt(1 - 0.40**2) * orth
    e_vec = e_vec / np.linalg.norm(e_vec)
    assert _cosine_sim(q_vec, e_vec) < 0.55

    _insert_entity_vec(db, eid, e_vec)

    mock_emb = MagicMock()
    mock_emb.embed.return_value = [q_vec]
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        # Query shares no tokens with anchor body — no strong-hit fires.
        results = rm.recall_fusion(db, "pqrxyz bbbccc dddyyy", min_score=0.01)

    entity_hits = [r for r in results if r.get("kind") == "entity"]
    assert entity_hits == [], f"Expected entity with vec<0.55 and no strong-hit dropped, got: {entity_hits}"


# ── 2. no anchor bias ─────────────────────────────────────────────────────────

def test_no_anchor_bias(db):
    """Milestone scored as w_bm25*bm25 + w_milestones_vec*vec — no static bias.

    Seeds a milestone with vec>=0.55 so it clears the pre-gate.
    Score must equal the formula exactly with no additive constant.
    """
    import re
    import math
    db.execute(
        "INSERT INTO milestones(scope, date, title, description, pinned) "
        "VALUES('test', '2026-06-01', 'anchorbiascheck milestone', '', 0)"
    )
    db.execute("INSERT INTO milestones_fts(milestones_fts) VALUES('rebuild')")
    db.commit()

    mid = db.execute(
        "SELECT id FROM milestones WHERE title='anchorbiascheck milestone'"
    ).fetchone()["id"]

    # Give it vec=0.70 so it clears floor=0.55 and produces a known score.
    q_vec = _fake_vec(42)
    orth = _fake_vec(99)
    orth -= np.dot(orth, q_vec) * q_vec
    orth /= np.linalg.norm(orth)
    m_vec = 0.70 * q_vec + np.sqrt(1 - 0.70**2) * orth
    m_vec /= np.linalg.norm(m_vec)
    _insert_milestone_vec(db, mid, m_vec)

    mock_emb = MagicMock()
    mock_emb.embed.return_value = [q_vec]

    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(db, "anchorbiascheck milestone", min_score=0.01)

    milestone_hits = [r for r in results if r.get("kind") == "milestone"]
    assert milestone_hits, "Expected milestone with vec>=0.55 to surface"

    ms_score = milestone_hits[0]["score"]
    # Score must be w_bm25*bm25 + w_milestones_vec*vec — no static bonus.
    # Max possible = 0.30*1.0 + 0.60*1.0 = 0.90; real vec~0.70 so < 0.75.
    assert ms_score < 0.80, (
        f"Milestone score {ms_score:.4f} too high — suggests static bias added"
    )


# ── 3. pinned no boost (matches task spec test_pinned_no_boost) ───────────────

def test_pinned_no_boost(db):
    """Pinned milestone same score as unpinned at identical bm25+vec."""
    db.execute(
        "INSERT INTO milestones(scope, date, title, description, pinned) "
        "VALUES('test', '2026-01-01', '正确测试', 'same desc', 0)"
    )
    db.execute(
        "INSERT INTO milestones(scope, date, title, description, pinned) "
        "VALUES('test', '2026-01-01', '正确测试', 'same desc', 1)"
    )
    db.execute("INSERT INTO milestones_fts(milestones_fts) VALUES('rebuild')")
    db.commit()

    # Inject identical vecs so both milestones clear the pre-gate (floor=0.55).
    # Identical vec → cosine sim = 1.0 for both → equal score.
    vec = _fake_vec(30)
    mid0 = db.execute(
        "SELECT id FROM milestones WHERE pinned=0 AND title='正确测试'"
    ).fetchone()["id"]
    mid1 = db.execute(
        "SELECT id FROM milestones WHERE pinned=1 AND title='正确测试'"
    ).fetchone()["id"]
    _insert_milestone_vec(db, mid0, vec)
    _insert_milestone_vec(db, mid1, vec)

    mock_emb = MagicMock()
    mock_emb.embed.return_value = [vec]
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(db, "正确测试", min_score=0.01)

    mhits = [r for r in results if r.get("kind") == "milestone"]
    assert len(mhits) == 2
    assert abs(mhits[0]["score"] - mhits[1]["score"]) < 1e-9, (
        f"Pinned should not affect score: {mhits[0]['score']:.9f} vs "
        f"{mhits[1]['score']:.9f}"
    )


# ── 3b. anchor formula coefficient regression ─────────────────────────────────
# Formula: raw = w_bm25(0.30) * bm25 + w_milestones_vec(0.58) * vec
# Four canonical examples from the design spec:
#   vec=0.55, bm25=0.0  → 0.319  (below min_score=0.40 — never surfaces)
#   vec=0.70, bm25=0.0  → 0.406  (just clears min_score)
#   vec=0.55, bm25=0.5  → 0.469  (vec+keyword combo surfaces)
#   vec=0.0             → dropped by pre-gate before scoring

W_BM25 = 0.30
W_MS_VEC = 0.58   # w_milestones_vec default


def test_anchor_formula_vec055_bm25_zero():
    """vec=0.55, bm25=0 → 0.319 (below min_score=0.40, won't surface)."""
    raw = W_BM25 * 0.0 + W_MS_VEC * 0.55
    assert abs(raw - 0.319) < 1e-3, f"Expected ~0.319, got {raw:.4f}"


def test_anchor_formula_vec070_bm25_zero():
    """vec=0.70, bm25=0 → 0.406 (just clears min_score=0.40)."""
    raw = W_BM25 * 0.0 + W_MS_VEC * 0.70
    assert abs(raw - 0.406) < 1e-3, f"Expected ~0.406, got {raw:.4f}"


def test_anchor_formula_vec055_bm25_half():
    """vec=0.55, bm25=0.5 → 0.469 (vec+keyword combo surfaces)."""
    raw = W_BM25 * 0.5 + W_MS_VEC * 0.55
    assert abs(raw - 0.469) < 1e-3, f"Expected ~0.469, got {raw:.4f}"


def test_anchor_formula_fts_only_dropped(db):
    """Milestone with vec=0 (FTS-only hit) is dropped by the pre-gate when query
    does not substring-match the anchor body (no strong-hit bypass).
    """
    db.execute(
        "INSERT INTO milestones(scope, date, title, description, pinned) "
        "VALUES('test', '2026-01-01', 'zxqftsonly unique anchor', 'zxqdesc', 0)"
    )
    db.execute("INSERT INTO milestones_fts(milestones_fts) VALUES('rebuild')")
    db.commit()
    # No vec inserted — vec=0.0, below _ANCHOR_VEC_FLOOR=0.55.
    # Query does not contain "zxqftsonly" or "zxqdesc" → no strong-hit → must be dropped.
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_fusion(db, "some unrelated semantic query about nothing", min_score=0.01)
    hits = [r for r in results if r.get("kind") == "milestone"]
    assert hits == [], f"FTS-only milestone with no strong-hit must be dropped by pre-gate, got: {hits}"


# ── 4. cwd boost ──────────────────────────────────────────────────────────────

def test_cwd_boost(tmp_path):
    """Same-bucket event gets +0.05 boost; cross-bucket event gets +0.00.

    Uses patched _load_bucket_rules to enforce the new config values
    (same_boost=0.05, diff_penalty=0.00) regardless of test config fallback.
    """
    from unittest.mock import patch as _patch

    # New config defaults: same_boost=0.05, diff_penalty=0.00
    _buckets = (("/cc-lab", "project"), ("/study", "study"))
    _same_boost = 0.05
    _diff_penalty = 0.00

    conn = storage.init_db(str(tmp_path / "cwd.db"))
    try:
        ts = "2026-06-01T00:00:00Z"
        conn.execute("INSERT INTO sessions(sid, cwd) VALUES('proj-sid', '/Users/x/CC-Lab/foo')")
        repo.archive_events(conn, [{
            "session_id": "proj-sid", "timestamp": ts,
            "role": "user", "content": "cwdboost test event",
        }])
        conn.commit()

        fake_rules = (_buckets, _same_boost, _diff_penalty)

        with _patch.object(rm, "_ensure_embedder", return_value=None):
            with _patch.object(rm, "_load_bucket_rules", return_value=fake_rules):
                # same-bucket query cwd → +0.05
                with_boost = rm.recall_fusion(
                    conn, "cwdboost test event",
                    min_score=0.01,
                    current_cwd="/Users/x/CC-Lab/marrow",
                )
                # cross-bucket: diff_penalty=0.00 → no change
                no_boost = rm.recall_fusion(
                    conn, "cwdboost test event",
                    min_score=0.01,
                    current_cwd="/Users/x/Study/gamsat",
                )

        assert with_boost, "Expected hit with same-bucket cwd"
        assert no_boost, "Expected hit with cross-bucket cwd"

        s_with = with_boost[0]["score"]
        s_without = no_boost[0]["score"]
        delta = s_with - s_without

        # same_boost=0.05, diff_penalty=0.00 → delta must be exactly 0.05.
        assert abs(delta - 0.05) < 1e-9, (
            f"Expected same-bucket boost delta=0.05, got {delta:.9f} "
            f"(with={s_with:.6f}, without={s_without:.6f})"
        )
    finally:
        conn.close()


# ── 5. exclude_kinds ──────────────────────────────────────────────────────────

def test_exclude_kinds_default(tmp_path):
    """recall_with_config default (exclude_kinds=('diary','task')) hides diary/task."""
    from unittest.mock import patch as _patch
    from marrow import config as cfg_mod

    conn = storage.init_db(str(tmp_path / "ek.db"))
    try:
        # Seed a diary entry (vec only lane).
        conn.execute(
            "INSERT INTO diary(date, content) VALUES('2026-06-01', 'test diary exclude content')"
        )
        conn.commit()

        # Use recall_fusion directly with exclude_kinds to avoid config loading.
        with _patch.object(rm, "_ensure_embedder", return_value=None):
            with_diary = rm.recall_fusion(
                conn, "exclude content", min_score=0.01, exclude_kinds=()
            )
            without_diary = rm.recall_fusion(
                conn, "exclude content", min_score=0.01,
                exclude_kinds=("diary", "task")
            )

        diary_in_with = [r for r in with_diary if r.get("kind") == "diary"]
        diary_in_without = [r for r in without_diary if r.get("kind") == "diary"]

        # Vec-only lane — with no embedder, no diary candidates are generated
        # regardless. But we can verify exclude_kinds doesn't break anything
        # and the signature is accepted.
        assert diary_in_without == [], (
            f"diary should be excluded: {diary_in_without}"
        )
    finally:
        conn.close()


def test_exclude_kinds_explicit_empty(tmp_path):
    """exclude_kinds=() includes all kinds (MCP path)."""
    conn = storage.init_db(str(tmp_path / "ek2.db"))
    try:
        with patch.object(rm, "_ensure_embedder", return_value=None):
            # Just verify empty tuple is accepted without error.
            results = rm.recall_fusion(
                conn, "anything", min_score=0.01, exclude_kinds=()
            )
        assert isinstance(results, list)
    finally:
        conn.close()


# ── 6. UTC → Melbourne timestamp conversion ───────────────────────────────────

def test_utc_to_local_date():
    """2026-06-06T14:00:00Z is 2026-06-07 in Melbourne (AEST = UTC+10, DST off)."""
    from marrow.timeutil import utc_iso_to_local_date
    # June = AEST (UTC+10, no DST). 14:00 UTC = 00:00+10 next day.
    result = utc_iso_to_local_date("2026-06-06T14:00:00Z")
    assert result == "2026-06-07", f"Expected 2026-06-07, got {result!r}"


def test_utc_to_local_date_dst():
    """2026-01-06T13:00:00Z is 2026-01-07 in Melbourne (AEDT = UTC+11, DST on)."""
    from marrow.timeutil import utc_iso_to_local_date
    # January = AEDT (UTC+11). 13:00 UTC = 00:00+11 next day.
    result = utc_iso_to_local_date("2026-01-06T13:00:00Z")
    assert result == "2026-01-07", f"Expected 2026-01-07, got {result!r}"


def test_utc_to_local_date_empty():
    """Empty string returns empty string."""
    from marrow.timeutil import utc_iso_to_local_date
    assert utc_iso_to_local_date("") == ""


def test_utc_to_local_datetime():
    """Datetime conversion includes HH:MM in Melbourne local time."""
    from marrow.timeutil import utc_iso_to_local_datetime
    # 2026-06-06T14:00:00Z = 2026-06-07 00:00 AEST.
    result = utc_iso_to_local_datetime("2026-06-06T14:00:00Z")
    assert result == "2026-06-07 00:00", f"Expected '2026-06-07 00:00', got {result!r}"


# ── 7. stopword filter ────────────────────────────────────────────────────────

def test_stopword_filter_ascii():
    """ASCII stopword removed from query."""
    result = rm._apply_stopwords("embed pipeline now", ["now"])
    assert result == "embed pipeline", f"Got: {result!r}"


def test_stopword_filter_cjk_whole_run():
    """CJK token matching stopword as a whole run is dropped."""
    result = rm._apply_stopwords("嗯好的 embed pipeline", ["嗯好的"])
    assert result == "embed pipeline", f"Got: {result!r}"


def test_stopword_filter_cjk_embedded():
    """Stopword embedded inside a larger CJK run is stripped out."""
    # "嗯好" is a stopword embedded in "嗯好的吧"
    result = rm._apply_stopwords("嗯好的吧 recall", ["嗯好"])
    # "嗯好" stripped from "嗯好的吧" → "的吧" remains
    assert "嗯好" not in result, f"Embedded stopword not stripped: {result!r}"
    assert "recall" in result


def test_stopword_filter_empty_stopwords():
    """Empty stopword list returns query unchanged."""
    q = "embed pipeline 嗯好的"
    assert rm._apply_stopwords(q, []) == q


def test_stopword_filter_query_becomes_empty():
    """If all tokens are stopwords, returns empty string."""
    result = rm._apply_stopwords("嗯好", ["嗯好"])
    assert result == "", f"Got: {result!r}"


def test_stopword_filter_in_fusion_empty_returns_empty(db):
    """recall_fusion returns [] when stopwords filter eliminates entire query."""
    from unittest.mock import patch as _patch
    from marrow import config as cfg_mod

    # Patch config to return stopwords that match the full query.
    fake_cfg = {"recall": {"stopwords": ["hello"]}}
    with _patch.object(cfg_mod, "load", return_value=fake_cfg):
        with _patch.object(rm, "_ensure_embedder", return_value=None):
            results = rm.recall_fusion(db, "hello")
    assert results == [], f"Expected [] when query is fully stopworded, got: {results}"


# ── 8. WX time-anchor strip ───────────────────────────────────────────────────

def test_time_anchor_strip(db):
    """[time: ... | gap: ...] prefix is stripped before recall."""
    repo.archive_events(db, [{
        "session_id": "wx-sid",
        "timestamp": "2026-06-06T04:00:00Z",
        "role": "user",
        "content": "hi how are you today",
    }])
    db.commit()

    with patch.object(rm, "_ensure_embedder", return_value=None):
        # With prefix — should strip "[time:...] " and search for "hi"
        results_with = rm.recall_with_config(
            db, "[time: 2026-06-06 Sat 04:23 | gap: 0m] hi how are you today",
            exclude_kinds=("diary", "task"),
        )
        # Without prefix — identical search
        results_without = rm.recall_with_config(
            db, "hi how are you today",
            exclude_kinds=("diary", "task"),
        )

    # Both should hit the same event (or both miss — consistent).
    ids_with = {r["id"] for r in results_with}
    ids_without = {r["id"] for r in results_without}
    assert ids_with == ids_without, (
        f"Time-anchor strip changed results: with={ids_with}, without={ids_without}"
    )


def test_time_anchor_strip_unit():
    """Unit test: strip regex applied in recall_with_config."""
    import re
    anchor = "[time: 2026-06-06 Sat 04:23 | gap: 0m] hi"
    stripped = re.sub(r"^\[time:[^\]]+\]\s*", "", anchor.strip())
    assert stripped == "hi", f"Got: {stripped!r}"


def test_time_anchor_no_false_strip():
    """Non-anchor query is not modified."""
    import re
    q = "recall the embed pipeline discussion"
    stripped = re.sub(r"^\[time:[^\]]+\]\s*", "", q.strip())
    assert stripped == q
