"""TDD red tests for entity-recall + memes-leg + milestone reverse-substring bugs.

- entity_recall.entity_force_include must match 2-CJK-char names (南南, 小胖).
- recall_fusion must surface memes rows (cipher / nickname / phrase) when key
  appears in query (substring).
- _milestone_candidates must also reverse-match title against query (fallback).
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest

from marrow import entity_recall as er, recall as rm, repo, storage


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

    # 2-char CJK entity (南南).
    nn_eid = add_event("s_nn_1", "今天和南南一起吃饭，聊得很开心。", 0)
    add_event("s_nn_2", "南南又来了，带了奶茶。", 4)
    conn.execute(
        "INSERT INTO entities (kind, name, mention_count, source) "
        "VALUES ('person', '南南', 5, 'test')"
    )

    # ASCII entity (Amber).
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
    yield conn, nn_eid, amber_eid
    conn.close()


def test_entity_force_include_2char_cjk_name(seeded_db):
    """2-CJK-char entity (南南) must be found via reverse substring."""
    conn, nn_eid, _ = seeded_db
    rows = er.entity_force_include(conn, "你还记得南南么", limit=10)
    nn_hits = [r for r in rows if "南南" in (r.get("content") or "")]
    assert len(nn_hits) >= 1, (
        f"Expected at least 1 (南南) hit, got {len(rows)} total rows"
    )


def test_entity_force_include_ascii_name(seeded_db):
    """ASCII entity name (Amber) must still match."""
    conn, _, amber_eid = seeded_db
    rows = er.entity_force_include(conn, "Amber 在干嘛", limit=10)
    hits = [r for r in rows if "Amber" in (r.get("content") or "")]
    assert len(hits) >= 1


def test_entity_force_include_does_not_match_unrelated_query(seeded_db):
    """Query without any entity-name substring returns no force-include rows."""
    conn, _, _ = seeded_db
    rows = er.entity_force_include(conn, "今天天气", limit=10)
    assert rows == []


def test_memes_leg_returns_cipher_on_query_match(seeded_db):
    """recall_fusion must surface memes row when key matches query (substring)."""
    conn, _, _ = seeded_db
    with patch.object(rm, "_ensure_embedder", return_value=None):
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
    """Milestone title (Bendigo placement) must hit with kw_score=1.0 even
    when query has extra noise tokens — reverse substring is strongest signal."""
    conn, _, _ = seeded_db
    cands = rm._milestone_candidates(
        conn, "今天 Bendigo placement 怎么样", limit=10
    )
    bendigo = [c for c in cands if c["content"].startswith("Bendigo")]
    assert bendigo, f"Expected Bendigo milestone candidate, got {cands}"
    assert bendigo[0]["bm25"] == 1.0, (
        f"Expected kw_score=1.0 via reverse-substring boost, "
        f"got {bendigo[0]['bm25']}"
    )
