"""Acceptance tests for Bug #0: recall outlet starvation + entity force-include.

Covers:
- Test 1: recall_with_config returns >=10 events for entity with 12 mentions.
- Test 2: both events for a 2-mention entity are returned.
- Test 3: force-include cap works — query for entity A does not return only B.
- Test 4: backward compat — recall_fusion with explicit limit=5 still works.
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest

from marrow import recall as rm, repo, storage


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def bug0_db(tmp_path):
    conn = storage.init_db(str(tmp_path / "bug0.db"))
    base_date = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)

    # 12 events mentioning (李小云).
    lxy_ids: list[int] = []
    for i in range(12):
        ts = (base_date + dt.timedelta(hours=i * 8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        repo.archive_events(conn, [{
            "session_id": f"s_lxy_{i}",
            "timestamp": ts,
            "role": "user",
            "content": f"今天聊到了李小云，事件编号{i}，内容略有不同。",
        }])
        row = conn.execute(
            "SELECT id FROM events WHERE session_id=?", (f"s_lxy_{i}",)
        ).fetchone()
        lxy_ids.append(row["id"])

    # 2 events mentioning (大龙虾).
    dlx_ids: list[int] = []
    for i in range(2):
        ts = (base_date + dt.timedelta(days=1, hours=i * 4)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        repo.archive_events(conn, [{
            "session_id": f"s_dlx_{i}",
            "timestamp": ts,
            "role": "user",
            "content": f"大龙虾今天又出现了，第{i}次。",
        }])
        row = conn.execute(
            "SELECT id FROM events WHERE session_id=?", (f"s_dlx_{i}",)
        ).fetchone()
        dlx_ids.append(row["id"])

    # 20 unrelated noise events.
    for i in range(20):
        ts = (base_date + dt.timedelta(days=2, hours=i)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        repo.archive_events(conn, [{
            "session_id": f"s_noise_{i}",
            "timestamp": ts,
            "role": "user",
            "content": f"这是完全不相关的噪音事件，编号{i}，随机内容。",
        }])

    # Seed entities table: (李小云) mention_count=12, (大龙虾) mention_count=2.
    conn.execute(
        "INSERT INTO entities (kind, name, mention_count, source) "
        "VALUES ('person', '李小云', 12, 'test')"
    )
    lxy_entity_id = conn.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO entities (kind, name, mention_count, source) "
        "VALUES ('person', '大龙虾', 2, 'test')"
    )
    dlx_entity_id = conn.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]
    conn.commit()

    # Seed affect rows linking lxy events, with entities JSON.
    import json
    for eid in lxy_ids:
        conn.execute(
            "INSERT INTO affect (date, ep, event_id, valence, arousal, "
            "importance, entities, source) VALUES (?, 1, ?, 0.7, 0.6, 6, ?, ?)",
            (
                "2026-05-19",
                eid,
                json.dumps(["李小云"]),
                "test",
            ),
        )
    for eid in dlx_ids:
        conn.execute(
            "INSERT INTO affect (date, ep, event_id, valence, arousal, "
            "importance, entities, source) VALUES (?, 1, ?, 0.6, 0.5, 5, ?, ?)",
            (
                "2026-05-20",
                eid,
                json.dumps(["大龙虾"]),
                "test",
            ),
        )
    conn.commit()

    yield conn, lxy_ids, dlx_ids, lxy_entity_id, dlx_entity_id
    conn.close()


# ── tests ─────────────────────────────────────────────────────────────────────

def test_lxy_surfaces_at_least_10_events(bug0_db):
    """Test 1: 12 (李小云) events → recall returns >=10 of them.

    Explicit limit=15 — bug0 predates the 2026-06-01 default cut (10→6),
    but the regression it guards is CJK FTS recall, not the cap value.
    """
    conn, lxy_ids, _, _, _ = bug0_db
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_with_config(conn, "李小云", limit=15)
    lxy_hits = [
        r for r in results
        if "李小云" in (r.get("content") or "")
    ]
    assert len(lxy_hits) >= 10, (
        f"Expected >=10 李小云 events, got {len(lxy_hits)} "
        f"(total results: {len(results)})"
    )


def test_dlx_both_events_surface(bug0_db):
    """Test 2: 2 (大龙虾) events → both are returned."""
    conn, _, dlx_ids, _, _ = bug0_db
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_with_config(conn, "大龙虾")
    result_ids = {r["id"] for r in results}
    missing = [eid for eid in dlx_ids if eid not in result_ids]
    assert not missing, (
        f"Missing (大龙虾) event ids: {missing}. "
        f"Got ids: {result_ids}"
    )


def test_force_include_cap_no_monopoly(bug0_db):
    """Test 3: force-include cap — (李小云) query does not return only (大龙虾) events."""
    conn, lxy_ids, dlx_ids, _, _ = bug0_db
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_with_config(conn, "李小云")
    dlx_only = all("大龙虾" in (r.get("content") or "") for r in results)
    assert not dlx_only, "(李小云) query returned only (大龙虾) events — force cap broken"
    lxy_hits = [r for r in results if "李小云" in (r.get("content") or "")]
    assert len(lxy_hits) > 0, "No (李小云) hits returned at all"


def test_backward_compat_explicit_limit(bug0_db):
    """Test 4: recall_fusion with explicit limit=5 still works."""
    conn, _, _, _, _ = bug0_db
    with patch.object(rm, "_ensure_embedder", return_value=None):
        results = rm.recall_fusion(conn, "marrow", limit=5)
    # Should not raise; returns list (possibly empty for unrelated query).
    assert isinstance(results, list)
    assert len(results) <= 5
