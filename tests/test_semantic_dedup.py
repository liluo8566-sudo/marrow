"""Tests for the shared semantic_dedup helper + the entity cosine layer that
survives in candidates.match_entity (dim upsert dedup + auto-learn aliases).

Cosine is monkeypatched in these tests — no bge-m3 inference. Real
embedder round-trip is covered by tests/test_memes_dedup.py (slow path).
"""
from __future__ import annotations

import json

import pytest

from marrow import (
    candidates,
    semantic_dedup,
    storage,
)


@pytest.fixture()
def db(tmp_path):
    return storage.init_db(str(tmp_path / "sd.db"))


# ── cosine_top_match ────────────────────────────────────────────────────────

def test_cosine_top_match_empty_targets_returns_neutral(db, monkeypatch):
    # Force embedder present so we hit the empty-targets branch, not None.
    class _StubEmb:
        def embed(self, texts):  # pragma: no cover — not reached
            raise AssertionError("should not embed when targets empty")
    monkeypatch.setattr(
        "marrow.recall._ensure_embedder", lambda: _StubEmb(),
    )
    hit = semantic_dedup.cosine_top_match(db, "anything", [])
    assert hit == (-1, 0.0)


def test_cosine_top_match_embedder_missing(db, monkeypatch):
    monkeypatch.setattr("marrow.recall._ensure_embedder", lambda: None)
    hit = semantic_dedup.cosine_top_match(db, "anything", ["a", "b"])
    assert hit is None


def test_cosine_top_match_picks_highest(db, monkeypatch):
    import numpy as np

    # Stub embedder: returns hardcoded unit vectors; query closer to t1.
    class _StubEmb:
        def embed(self, texts):
            # query=texts[0]; targets follow. Build orthonormal-ish vecs.
            vmap = {
                "query": np.array([1.0, 0.0, 0.0]),
                "t0": np.array([0.2, 0.98, 0.0]),
                "t1": np.array([0.9, 0.43, 0.0]),
                "t2": np.array([0.0, 0.0, 1.0]),
            }
            return np.array([vmap[t] for t in texts])
    monkeypatch.setattr(
        "marrow.recall._ensure_embedder", lambda: _StubEmb(),
    )
    hit = semantic_dedup.cosine_top_match(db, "query", ["t0", "t1", "t2"])
    assert hit is not None
    idx, score = hit
    assert idx == 1  # t1 has highest dot
    assert score >= 0.85


# ── entities: match_entity cosine layer (dim upsert dedup) ──────────────────

def test_match_entity_cosine_hit_returns_row(db, monkeypatch):
    # Seed existing entity; cosine-high candidate "Stellan" matches "屿忱".
    db.execute(
        "INSERT INTO entities (kind, name, source) VALUES (?, ?, ?)",
        ("person", "屿忱", "test"),
    )
    db.commit()
    rid = db.execute(
        "SELECT id FROM entities WHERE name='屿忱'"
    ).fetchone()["id"]
    monkeypatch.setattr(
        semantic_dedup, "cosine_top_match", lambda conn, q, t: (0, 0.91),
    )
    hit = candidates.match_entity(db, "person", "Stellan", [])
    assert hit == rid


def test_match_entity_cosine_hit_merge_absorbs_alias(db, monkeypatch):
    db.execute(
        "INSERT INTO entities (kind, name, source) VALUES (?, ?, ?)",
        ("person", "屿忱", "test"),
    )
    db.commit()
    monkeypatch.setattr(
        semantic_dedup, "cosine_top_match", lambda conn, q, t: (0, 0.91),
    )
    hit = candidates.match_entity(db, "person", "Stellan", [])
    candidates._merge_aliases_into(db, hit, "Stellan", [])
    row = db.execute(
        "SELECT aliases FROM entities WHERE name='屿忱'"
    ).fetchone()
    aliases = json.loads(row["aliases"])
    assert "Stellan" in aliases
    cnt = db.execute(
        "SELECT COUNT(*) FROM entities WHERE kind='person'"
    ).fetchone()[0]
    assert cnt == 1


def test_match_entity_cosine_miss_returns_none(db, monkeypatch):
    db.execute(
        "INSERT INTO entities (kind, name, source) VALUES (?, ?, ?)",
        ("person", "屿忱", "test"),
    )
    db.commit()
    monkeypatch.setattr(
        semantic_dedup, "cosine_top_match", lambda conn, q, t: (0, 0.20),
    )
    hit = candidates.match_entity(db, "person", "某陌生人", [])
    assert hit is None
