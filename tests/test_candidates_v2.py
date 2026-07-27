"""Surviving candidates.py coverage after the daily-pipeline retirement:
- match_entity + _merge_aliases_into (dim upsert dedup + auto-learn aliases)
- bump_use_counts (per-turn meme use_count bump, wired via repo.archive_events)
"""
from __future__ import annotations

import json

import pytest

from marrow import candidates, storage


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    return conn


@pytest.fixture(autouse=True)
def _disable_cosine_layer(monkeypatch):
    """Target the string/alias dedup layer. The cosine layer is exercised in
    tests/test_semantic_dedup.py with explicit stubs. Force it off here so
    bge-m3 paraphrase scoring can't swallow rows the string layer keeps
    distinct (e.g. '阿澈' vs '阿澈新' score 0.87).
    """
    from marrow import semantic_dedup
    monkeypatch.setattr(
        semantic_dedup, "cosine_max", lambda conn, q, t: 0.0,
    )
    monkeypatch.setattr(
        semantic_dedup, "cosine_top_match", lambda conn, q, t: (-1, 0.0),
    )


# ── bump_use_counts ─────────────────────────────────────────────────────────

def _seed_meme(conn, key, *, vtype="paw", status="active", use_count=0):
    cur = conn.execute(
        "INSERT INTO memes (type, key, value, use_count, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (vtype, key, "v", use_count, status),
    )
    conn.commit()
    return cur.lastrowid


def _uc(conn, mid):
    return conn.execute(
        "SELECT use_count FROM memes WHERE id=?", (mid,)
    ).fetchone()["use_count"]


def test_bump_use_counts_single_event_single_meme(db):
    mid = _seed_meme(db, "野鸡")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T01:00:00Z",
             "role": "user", "content": "又是一个野鸡 codex"}]
    n = candidates.bump_use_counts(db, rows)
    assert n == 1 and _uc(db, mid) == 1


def test_bump_use_counts_same_event_double_mention_counts_once(db):
    mid = _seed_meme(db, "野鸡")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T02:00:00Z",
             "role": "user", "content": "野鸡野鸡都是野鸡"}]
    n = candidates.bump_use_counts(db, rows)
    assert n == 1 and _uc(db, mid) == 1


def test_bump_use_counts_multiple_events_accumulate(db):
    mid = _seed_meme(db, "野鸡")
    rows = [
        {"session_id": "s1", "timestamp": "2026-05-17T03:00:00Z",
         "role": "user", "content": "野鸡 one"},
        {"session_id": "s1", "timestamp": "2026-05-17T03:01:00Z",
         "role": "assistant", "content": "野鸡 two"},
    ]
    candidates.bump_use_counts(db, rows)
    assert _uc(db, mid) == 2


def test_bump_use_counts_non_matching_no_bump(db):
    mid = _seed_meme(db, "野鸡")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T04:00:00Z",
             "role": "user", "content": "完全没有这个词"}]
    candidates.bump_use_counts(db, rows)
    assert _uc(db, mid) == 0


def test_bump_use_counts_skips_non_user_assistant(db):
    mid = _seed_meme(db, "野鸡")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T05:00:00Z",
             "role": "system", "content": "野鸡 system noise"}]
    candidates.bump_use_counts(db, rows)
    assert _uc(db, mid) == 0


def test_bump_use_counts_case_insensitive(db):
    mid = _seed_meme(db, "Codex")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T06:00:00Z",
             "role": "user", "content": "tried codex today"}]
    candidates.bump_use_counts(db, rows)
    assert _uc(db, mid) == 1


def test_bump_use_counts_dormant_meme_skipped(db):
    mid = _seed_meme(db, "野鸡", status="dormant")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T07:00:00Z",
             "role": "user", "content": "野鸡 again"}]
    candidates.bump_use_counts(db, rows)
    assert _uc(db, mid) == 0


def test_archive_events_bumps_use_count_end_to_end(db):
    """Real wiring via repo.archive_events — single inserted event matching
    a seeded meme key must bump use_count.
    """
    from marrow import repo
    mid = _seed_meme(db, "野鸡", use_count=5)
    rows = [{"session_id": "s_wire", "timestamp": "2026-05-17T08:00:00Z",
             "role": "user", "content": "又来一只野鸡"}]
    repo.archive_events(db, rows)
    assert _uc(db, mid) == 6


def test_archive_events_idempotent_rerun_no_double_bump(db):
    """Re-archiving the same event (dedup by source_hash) must NOT re-bump."""
    from marrow import repo
    mid = _seed_meme(db, "野鸡", use_count=0)
    rows = [{"session_id": "s_idem", "timestamp": "2026-05-17T09:00:00Z",
             "role": "user", "content": "野鸡 only once"}]
    repo.archive_events(db, rows)
    repo.archive_events(db, rows)
    assert _uc(db, mid) == 1


# ── match_entity + _merge_aliases_into ──────────────────────────────────────

def _seed_entity(conn, name, kind="person", aliases=None):
    aliases_json = json.dumps(aliases, ensure_ascii=False) if aliases else None
    cur = conn.execute(
        "INSERT INTO entities (kind, name, aliases, source) VALUES (?,?,?,?)",
        (kind, name, aliases_json, "test"),
    )
    conn.commit()
    return cur.lastrowid


def _aliases(conn, rid):
    raw = conn.execute(
        "SELECT aliases FROM entities WHERE id=?", (rid,)
    ).fetchone()["aliases"]
    return json.loads(raw) if raw else []


def test_match_entity_incoming_name_hits_existing_alias(db):
    rid = _seed_entity(db, "阿澈", aliases=["言澈", "Stellan"])
    hit = candidates.match_entity(db, "person", "言澈", ["小屿"])
    assert hit == rid


def test_match_entity_incoming_alias_hits_existing_name(db):
    rid = _seed_entity(db, "阿澈")
    hit = candidates.match_entity(db, "person", "言澈", ["阿澈", "Stellan"])
    assert hit == rid


def test_match_entity_unrelated_name_no_hit(db):
    _seed_entity(db, "阿澈", aliases=["言澈"])
    hit = candidates.match_entity(db, "person", "陈奶奶", ["邻居陈"])
    assert hit is None


def test_match_entity_case_insensitive(db):
    rid = _seed_entity(db, "Stellan", aliases=["言澈"])
    hit = candidates.match_entity(db, "person", "stellan", ["雪狼"])
    assert hit == rid


def test_match_entity_scoped_by_kind(db):
    _seed_entity(db, "Bendigo", kind="place")
    hit = candidates.match_entity(db, "pref", "Bendigo", [])
    assert hit is None


def test_match_entity_superseded_row_does_not_hit(db):
    rid = _seed_entity(db, "阿澈", aliases=["言澈"])
    other = _seed_entity(db, "阿澈新")
    db.execute("UPDATE entities SET superseded_by=? WHERE id=?", (other, rid))
    db.commit()
    hit = candidates.match_entity(db, "person", "言澈", [])
    assert hit is None


def test_merge_aliases_into_adds_new_dedups_existing(db):
    rid = _seed_entity(db, "阿澈", aliases=["言澈", "Stellan"])
    candidates._merge_aliases_into(db, rid, "言澈", ["小屿"])
    aliases = _aliases(db, rid)
    assert "小屿" in aliases
    assert aliases.count("言澈") == 1  # already present, not duplicated


def test_merge_aliases_into_canonical_name_not_added(db):
    rid = _seed_entity(db, "阿澈")
    candidates._merge_aliases_into(db, rid, "言澈", ["阿澈", "Stellan"])
    aliases = _aliases(db, rid)
    assert "言澈" in aliases and "Stellan" in aliases
    assert "阿澈" not in aliases  # canonical name never duplicated into aliases


def test_merge_aliases_into_case_insensitive_name(db):
    rid = _seed_entity(db, "Stellan", aliases=["言澈"])
    candidates._merge_aliases_into(db, rid, "stellan", ["雪狼"])
    aliases = _aliases(db, rid)
    assert "雪狼" in aliases
    # 'stellan' matches canonical 'Stellan' case-insensitively → not added
    assert "stellan" not in [a.lower() for a in aliases]
