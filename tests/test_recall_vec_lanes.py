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


def _make_diary(db, date: str, content: str) -> int:
    db.execute(
        "INSERT INTO diary(date, content) VALUES(?, ?)",
        (date, content),
    )
    db.commit()
    return db.execute(
        "SELECT rowid FROM diary WHERE date=?", (date,)
    ).fetchone()["rowid"]


def _make_task(db, title: str, category: str = "task",
               next_step: str = "", status: str = "active") -> int:
    db.execute(
        "INSERT INTO tasks(category, title, next_step, status) "
        "VALUES(?, ?, ?, ?)",
        (category, title, next_step, status),
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


def test_embed_pending_backfills_all_six_lanes(db):
    _make_event(db, "event row")
    _make_meme(db, "meme-key")
    _make_entity(db, "Stellan", fact="partner")
    _make_milestone(db, "milestone-title")
    _make_diary(db, "2026-05-23", "diary content for vec")
    _make_task(db, "GAMSAT prep", category="study", next_step="section 2")
    mock_emb = MagicMock()
    mock_emb.embed.side_effect = lambda texts: np.stack(
        [_fake_vec(100 + i) for i, _ in enumerate(texts)]
    )
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        n = rm.embed_pending(db, batch=10)
    assert n == 6
    for tbl in ("events_vec_meta", "memes_vec_meta",
                "entities_vec_meta", "milestones_vec_meta",
                "diary_vec_meta", "tasks_vec_meta"):
        assert db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] == 1


def test_events_lane_embed_strips_time_and_sticker_markers(db):
    # Events-lane embed text must match what recall_fusion actually surfaces
    # (recall.py row passthrough, ~line 1817): wx time-anchor prefix and bare
    # [sticker: ...] marker lines stripped before the text is embedded.
    _make_event(db, "[time: 2026-06-23 Tue 14:49 | gap: 0m]\n啥\nhow comes")
    captured = {}
    mock_emb = MagicMock()

    def _embed(texts):
        captured["texts"] = list(texts)
        return np.stack([_fake_vec(600 + i) for i, _ in enumerate(texts)])
    mock_emb.embed.side_effect = _embed
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        n = rm.embed_pending(db, batch=10)
    assert n == 1
    assert captured["texts"] == ["啥\nhow comes"]


def test_events_lane_embed_strips_sticker_line_mixed_with_dialogue(db):
    _make_event(
        db,
        "[sticker: emoji=⭐, set=lumi_stickers_by_Stellan_CYC_bot]\n"
        "？狗男人怎么接不住我的梗",
    )
    captured = {}
    mock_emb = MagicMock()

    def _embed(texts):
        captured["texts"] = list(texts)
        return np.stack([_fake_vec(650 + i) for i, _ in enumerate(texts)])
    mock_emb.embed.side_effect = _embed
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        rm.embed_pending(db, batch=10)
    assert captured["texts"] == ["？狗男人怎么接不住我的梗"]


def test_events_lane_embed_skips_row_empty_after_shaping(db):
    # Bare marker with no body (should be junk-dropped pre-embed by repair /
    # ingest gate, but guard the embed path directly): shaping strips it to
    # empty, so it must be skipped entirely — no embed() call, no vec/meta row.
    eid = _make_event(db, "[time: 2026-06-23 Tue 14:49 | gap: 0m]")
    mock_emb = MagicMock()
    mock_emb.embed.side_effect = lambda texts: np.stack(
        [_fake_vec(700 + i) for i, _ in enumerate(texts)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        n = rm.embed_pending(db, batch=10)
    assert n == 0
    mock_emb.embed.assert_not_called()
    assert db.execute(
        "SELECT COUNT(*) FROM events_vec WHERE rowid=?", (eid,)
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM events_vec_meta WHERE rowid=?", (eid,)
    ).fetchone()[0] == 0


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


def test_embed_pending_skips_meta_only_tombstone_row(db):
    # Event with a meta row but NO vector = eviction tombstone (or pre-repair
    # poison). Dedup is NOT EXISTS vec AND NOT EXISTS meta, so it must be skipped
    # — not re-embedded on every backfill.
    eid = _make_event(db, "tombstoned event")
    db.execute(
        "INSERT INTO events_vec_meta(rowid, embedder_id, dim) "
        "VALUES(?, 'bge-m3', 1024)", (eid,))
    db.commit()
    mock_emb = MagicMock()
    mock_emb.embed.side_effect = lambda texts: np.stack(
        [_fake_vec(400 + i) for i, _ in enumerate(texts)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        n = rm.embed_pending(db, batch=10)
    assert n == 0
    assert db.execute("SELECT COUNT(*) FROM events_vec").fetchone()[0] == 0


def test_embed_pending_reembeds_after_poison_meta_removed(db):
    # The repair deletes the poisoned meta row (meta, no vec). Once gone, the
    # event is fresh (no meta, no vec) and embed_pending re-embeds it.
    eid = _make_event(db, "poisoned event")
    db.execute(
        "INSERT INTO events_vec_meta(rowid, embedder_id, dim) "
        "VALUES(?, 'bge-m3', 1024)", (eid,))
    db.commit()
    mock_emb = MagicMock()
    mock_emb.embed.side_effect = lambda texts: np.stack(
        [_fake_vec(500 + i) for i, _ in enumerate(texts)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        assert rm.embed_pending(db, batch=10) == 0  # skipped while meta present
        db.execute("DELETE FROM events_vec_meta WHERE rowid=?", (eid,))
        db.commit()
        assert rm.embed_pending(db, batch=10) == 1  # re-embedded after repair
    assert db.execute(
        "SELECT COUNT(*) FROM events_vec WHERE rowid=?", (eid,)
    ).fetchone()[0] == 1


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
    _make_diary(db, "2026-05-23", "vec disabled diary content")
    _make_task(db, "no vec task", category="study")
    with patch.object(rm, "_ensure_embedder", return_value=None):
        # Should not raise; no kw substring match on this query either.
        results = rm.recall_fusion(
            db, "wholly orthogonal sentence here", min_score=0.0,
        )
    # No memes / entity / milestone / diary / task vec-only rows surface.
    assert not any(r.get("kind") in ("memes", "milestone", "entity",
                                     "diary", "task")
                   for r in results)


# ── C. diary lane ────────────────────────────────────────────────────────────

def test_embed_diary_writes_and_idempotent(db):
    rid = _make_diary(db, "2026-05-23", "GAMSAT 准备 today, drained")
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([_fake_vec(20)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        assert rm.embed_diary(db, "2026-05-23", "GAMSAT 准备 today, drained") is True
        assert rm.embed_diary(db, "2026-05-23", "GAMSAT 准备 today, drained") is False
    assert db.execute(
        "SELECT COUNT(*) FROM diary_vec_meta WHERE rowid=?", (rid,)
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM diary_vec WHERE rowid=?", (rid,)
    ).fetchone()[0] == 1


def test_embed_diary_unknown_date_noop(db):
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([_fake_vec(21)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        assert rm.embed_diary(db, "1999-01-01", "nope") is False
    assert db.execute(
        "SELECT COUNT(*) FROM diary_vec_meta"
    ).fetchone()[0] == 0


@pytest.mark.skip(reason="diary/task lanes disabled in passive recall 2026-06-01; surface via mcp__marrow__recall(kind=) instead")
def test_diary_vec_lane_surfaces_via_monkeypatch(db, monkeypatch):
    rid = _make_diary(db, "2026-05-22", "long-form prose about a feeling")
    qvec = _fake_vec(22)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])

    def fake_diary_hits(c, b, k):
        return [{
            "id": rid, "date": "2026-05-22",
            "content": "long-form prose about a feeling",
            "vec_score": 0.7,
        }]

    monkeypatch.setattr(rm, "_diary_vec_hits", fake_diary_hits)
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(
            db, "totally unrelated query", min_score=0.0,
        )
    diary_hits = [r for r in results if r.get("kind") == "diary"]
    assert len(diary_hits) >= 1
    assert "long-form prose" in diary_hits[0]["content"]
    assert diary_hits[0]["score"] > 0


def test_diary_vec_lane_gated_by_floor(db, monkeypatch):
    rid = _make_diary(db, "2026-05-22", "low sim diary entry")
    qvec = _fake_vec(23)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])
    # 0.30 < _VEC_ONLY_FLOOR (0.40) — should be gated out.
    monkeypatch.setattr(rm, "_diary_vec_hits", lambda c, b, k: [{
        "id": rid, "date": "2026-05-22",
        "content": "low sim diary entry", "vec_score": 0.30,
    }])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(
            db, "anything goes here", min_score=0.0,
        )
    assert not any(r.get("kind") == "diary" for r in results)


@pytest.mark.skip(reason="diary lane disabled in passive recall 2026-06-01")
def test_diary_slot_reserved_when_limit_gt_5(db, monkeypatch):
    """With limit>5 and no strong FTS, at least one diary slot is reserved
    even when events would otherwise saturate the limit via vec-only scores."""
    rid = _make_diary(db, "2026-05-22", "diary entry to reserve")
    # Add events that only surface via the vec lane (no FTS overlap with the
    # query string), so strong_fts_count stays 0 and the diary cap applies.
    event_ids = [
        _make_event(db, f"alpha bravo charlie {i}") for i in range(8)
    ]
    qvec = _fake_vec(24)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])

    # Fake events_vec to return all 8 with mid sim — pushes 8 event candidates
    # into the pool without any FTS hit.
    def fake_execute(orig_execute):
        def wrapped(sql, *args, **kwargs):
            if "events_vec" in sql and "MATCH" in sql:
                # Return synthetic vec rows: (id, ..., distance)
                # Use a tiny in-memory fetcher mimicking sqlite rows.
                class _Row(dict):
                    def __getitem__(self, k): return dict.__getitem__(self, k)

                rows = []
                for i, eid in enumerate(event_ids):
                    rows.append(_Row(
                        id=eid, session_id="s1",
                        timestamp="2026-05-19T10:00:00Z",
                        role="user", content=f"alpha bravo charlie {i}",
                        channel=None, compressed=0,
                        distance=0.2 + i * 0.01,
                    ))

                class _Cur:
                    def fetchall(self_inner): return rows
                return _Cur()
            return orig_execute(sql, *args, **kwargs)
        return wrapped

    monkeypatch.setattr(rm, "_diary_vec_hits", lambda c, b, k: [{
        "id": rid, "date": "2026-05-22",
        "content": "diary entry to reserve", "vec_score": 0.6,
    }])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(
            db, "zzz nonmatching query", limit=6, min_score=0.0,
        )
    diary_hits = [r for r in results if r.get("kind") == "diary"]
    assert len(diary_hits) >= 1, (
        f"diary slot not reserved; results kinds = "
        f"{[r.get('kind') for r in results]}"
    )


def test_diary_orphan_sweep(db):
    """Rewriting a diary row (DELETE+INSERT) reassigns rowid; the previous
    vec_meta row should be swept by _embed_pending_lane before backfill."""
    rid1 = _make_diary(db, "2026-05-23", "first version")
    mock_emb = MagicMock()
    mock_emb.embed.side_effect = lambda texts: np.stack(
        [_fake_vec(400 + i) for i, _ in enumerate(texts)]
    )
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        rm.embed_pending(db, batch=10)
    assert db.execute(
        "SELECT COUNT(*) FROM diary_vec_meta WHERE rowid=?", (rid1,)
    ).fetchone()[0] == 1
    # Simulate daily.py rewrite: delete then re-insert. SQLite rowid will
    # very likely shift (vacuum-less, autoincrement-less default).
    db.execute("DELETE FROM diary WHERE date=?", ("2026-05-23",))
    db.execute(
        "INSERT INTO diary(date, content) VALUES(?, ?)",
        ("2026-05-23", "second version content"),
    )
    db.commit()
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        rm.embed_pending(db, batch=10)
    # Old rowid orphan is gone; only the current row remains.
    cur_rid = db.execute(
        "SELECT rowid FROM diary WHERE date=?", ("2026-05-23",)
    ).fetchone()["rowid"]
    rowids = {r["rowid"] for r in db.execute(
        "SELECT rowid FROM diary_vec_meta"
    ).fetchall()}
    assert rowids == {cur_rid}


# ── D. tasks lane ────────────────────────────────────────────────────────────

def test_embed_task_writes_and_idempotent(db):
    tid = _make_task(db, "GAMSAT prep", category="study",
                     next_step="section 2")
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([_fake_vec(30)])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        assert rm.embed_task(db, tid, "study: GAMSAT prep — section 2") is True
        assert rm.embed_task(db, tid, "study: GAMSAT prep — section 2") is False
    assert db.execute(
        "SELECT COUNT(*) FROM tasks_vec_meta WHERE rowid=?", (tid,)
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM tasks_vec WHERE rowid=?", (tid,)
    ).fetchone()[0] == 1


def test_embed_pending_skips_archived_tasks(db):
    live_tid = _make_task(db, "live task", category="study", status="active")
    done_tid = _make_task(db, "done task", category="task", status="done")
    _make_task(db, "archived task", category="task", status="archived")
    mock_emb = MagicMock()
    mock_emb.embed.side_effect = lambda texts: np.stack(
        [_fake_vec(500 + i) for i, _ in enumerate(texts)]
    )
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        rm.embed_pending(db, batch=10)
    rowids = {r["rowid"] for r in db.execute(
        "SELECT rowid FROM tasks_vec_meta"
    ).fetchall()}
    assert rowids == {live_tid, done_tid}


@pytest.mark.skip(reason="task lane disabled in passive recall 2026-06-01; tasks already surfaced via SessionStart Open Tasks block")
def test_tasks_vec_lane_surfaces_via_monkeypatch(db, monkeypatch):
    tid = _make_task(db, "GAMSAT chemistry", category="study",
                     next_step="finish 30 MCQs")
    qvec = _fake_vec(31)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])

    def fake_tasks_hits(c, b, k):
        return [{
            "id": tid, "category": "study",
            "title": "GAMSAT chemistry", "next_step": "finish 30 MCQs",
            "status": "active",
            "created_at": "2026-05-01T00:00:00Z", "vec_score": 0.7,
        }]

    monkeypatch.setattr(rm, "_tasks_vec_hits", fake_tasks_hits)
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(
            db, "totally unrelated query", min_score=0.0,
        )
    task_hits = [r for r in results if r.get("kind") == "task"]
    assert len(task_hits) >= 1
    assert "GAMSAT chemistry" in task_hits[0]["content"]
    assert "finish 30 MCQs" in task_hits[0]["content"]
    assert task_hits[0]["score"] > 0


def test_tasks_vec_lane_gated_by_floor(db, monkeypatch):
    tid = _make_task(db, "low sim task", category="study")
    qvec = _fake_vec(32)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])
    # 0.30 < _VEC_ONLY_FLOOR (0.40) — should be gated out.
    monkeypatch.setattr(rm, "_tasks_vec_hits", lambda c, b, k: [{
        "id": tid, "category": "study",
        "title": "low sim task", "next_step": "",
        "status": "active",
        "created_at": "2026-05-01T00:00:00Z", "vec_score": 0.30,
    }])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(
            db, "anything goes here", min_score=0.0,
        )
    assert not any(r.get("kind") == "task" for r in results)


@pytest.mark.skip(reason="task lane disabled in passive recall 2026-06-01")
def test_tasks_slot_reserved_when_limit_gt_5(db, monkeypatch):
    """With limit>5 and no strong FTS, at least one task slot is reserved
    even when events would otherwise saturate the limit via vec-only scores."""
    tid = _make_task(db, "task to reserve", category="study",
                     next_step="step")
    for i in range(8):
        _make_event(db, f"alpha bravo charlie {i}")
    qvec = _fake_vec(33)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = np.array([qvec])

    monkeypatch.setattr(rm, "_tasks_vec_hits", lambda c, b, k: [{
        "id": tid, "category": "study",
        "title": "task to reserve", "next_step": "step",
        "status": "active",
        "created_at": "2026-05-01T00:00:00Z", "vec_score": 0.6,
    }])
    with patch.object(rm, "_ensure_embedder", return_value=mock_emb):
        results = rm.recall_fusion(
            db, "zzz nonmatching query", limit=6, min_score=0.0,
        )
    task_hits = [r for r in results if r.get("kind") == "task"]
    assert len(task_hits) >= 1, (
        f"task slot not reserved; results kinds = "
        f"{[r.get('kind') for r in results]}"
    )
