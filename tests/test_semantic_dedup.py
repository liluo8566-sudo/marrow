"""Tests for the shared semantic_dedup helper + tasks/milestones/entities
cosine layer added on top of the existing string-layer dedup.

Cosine is monkeypatched in these tests — no bge-m3 inference. Real
embedder round-trip is covered by tests/test_memes_dedup.py (slow path).
"""
from __future__ import annotations

import json

import pytest

from marrow import (
    candidates,
    reconcile,
    semantic_dedup,
    sessionend_writers,
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


# ── tasks: sessionend_writers.seg_task_cand cosine layer ────────────────────

_TASK_RAW = (
    "===TASK===\n"
    "[{{\"title\":\"{title}\",\"category\":\"Study\",\"status\":\"active\"}}]\n"
    "===END===\n"
)


def test_seg_task_cand_cosine_hit_skips_insert(db, monkeypatch):
    # Seed an existing active task.
    db.execute(
        "INSERT INTO tasks (category, title, status) VALUES (?, ?, 'active')",
        ("Study", "AT3 essay"),
    )
    db.commit()
    # Cosine forced to high score → new candidate must be skipped.
    monkeypatch.setattr(
        semantic_dedup, "cosine_max", lambda conn, q, t: 0.91,
    )
    n = sessionend_writers.seg_task_cand(
        db, _TASK_RAW.format(title="AT3 论文"),
    )
    assert n == 0
    cnt = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert cnt == 1


def test_seg_task_cand_cosine_miss_inserts(db, monkeypatch):
    db.execute(
        "INSERT INTO tasks (category, title, status) VALUES (?, ?, 'active')",
        ("Study", "AT3 essay"),
    )
    db.commit()
    monkeypatch.setattr(
        semantic_dedup, "cosine_max", lambda conn, q, t: 0.40,
    )
    n = sessionend_writers.seg_task_cand(
        db, _TASK_RAW.format(title="买菜 grocery run"),
    )
    assert n == 1
    cnt = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert cnt == 2


def test_seg_task_cand_cosine_embedder_missing_still_inserts(db, monkeypatch):
    db.execute(
        "INSERT INTO tasks (category, title, status) VALUES (?, ?, 'active')",
        ("Study", "existing one"),
    )
    db.commit()
    monkeypatch.setattr(
        semantic_dedup, "cosine_max", lambda conn, q, t: None,
    )
    n = sessionend_writers.seg_task_cand(
        db, _TASK_RAW.format(title="something fresh"),
    )
    assert n == 1
    # Warn alert raised once.
    alert = db.execute(
        "SELECT 1 FROM alerts WHERE type='tasks_dedup_no_embedder'"
    ).fetchone()
    assert alert is not None


# ── tasks: reconcile._insert_unanchored_tasks cosine layer ──────────────────

def test_reconcile_unanchored_cosine_hit_skips(db, monkeypatch):
    db.execute(
        "INSERT INTO tasks (category, title, status) VALUES (?, ?, 'active')",
        ("Study", "AT3 essay"),
    )
    db.commit()
    monkeypatch.setattr(
        semantic_dedup, "cosine_max", lambda conn, q, t: 0.92,
    )
    rpt = reconcile.ReconcileReport()
    reconcile._insert_unanchored_tasks(
        db, [(" ", "Study | AT3 论文")], rpt,
    )
    assert rpt.inserted == 0
    cnt = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert cnt == 1


def test_reconcile_unanchored_cosine_miss_inserts(db, monkeypatch):
    db.execute(
        "INSERT INTO tasks (category, title, status) VALUES (?, ?, 'active')",
        ("Study", "AT3 essay"),
    )
    db.commit()
    monkeypatch.setattr(
        semantic_dedup, "cosine_max", lambda conn, q, t: 0.30,
    )
    rpt = reconcile.ReconcileReport()
    reconcile._insert_unanchored_tasks(
        db, [(" ", "Projects | brand new thing")], rpt,
    )
    assert rpt.inserted == 1


# ── milestones: write_milestone_cand cosine layer ───────────────────────────

_MS_RAW = (
    "===MILESTONE_CAND===\n"
    "[{{\"title\":\"{title}\",\"scope\":\"me\",\"date\":\"{date}\","
    " \"description\":\"d\",\"conf\":0.95}}]\n"
    "===END===\n"
)


def test_milestone_cosine_hit_skips(db, monkeypatch):
    db.execute(
        "INSERT INTO milestones (scope, date, title) VALUES (?, ?, ?)",
        ("me", "2026-05-20", "WAM 92"),
    )
    db.commit()
    monkeypatch.setattr(
        semantic_dedup, "cosine_max", lambda conn, q, t: 0.93,
    )
    n = candidates.write_milestone_cand(
        db, _MS_RAW.format(title="期末成绩 WAM 九十二", date="2026-05-25"),
        "2026-05-25",
    )
    assert n == 0


def test_milestone_cosine_miss_inserts(db, monkeypatch):
    db.execute(
        "INSERT INTO milestones (scope, date, title) VALUES (?, ?, ?)",
        ("me", "2026-05-20", "WAM 92"),
    )
    db.commit()
    monkeypatch.setattr(
        semantic_dedup, "cosine_max", lambda conn, q, t: 0.40,
    )
    n = candidates.write_milestone_cand(
        db, _MS_RAW.format(title="bought a townhouse", date="2026-05-25"),
        "2026-05-25",
    )
    assert n == 1


# ── entities: write_entity_cand cosine layer ────────────────────────────────

def _entity_raw(name: str, kind: str = "person",
                aliases: list[str] | None = None) -> str:
    obj = {"kind": kind, "name": name, "conf": 0.9}
    if aliases is not None:
        obj["aliases"] = aliases
    return (
        "===ENTITY_CAND===\n"
        + json.dumps([obj], ensure_ascii=False)
        + "\n===END===\n"
    )


def test_entity_cosine_hit_merges_alias(db, monkeypatch):
    # Seed existing entity with no aliases.
    db.execute(
        "INSERT INTO entities (kind, name, source) VALUES (?, ?, ?)",
        ("person", "屿忱", "daily"),
    )
    db.commit()
    # Cosine returns high — new candidate "Stellan" should be absorbed
    # as an alias of "屿忱", no fresh row.
    monkeypatch.setattr(
        semantic_dedup, "cosine_top_match", lambda conn, q, t: (0, 0.91),
    )
    n = candidates.write_entity_cand(db, _entity_raw("Stellan"))
    assert n == 0  # no INSERT — merged
    row = db.execute(
        "SELECT aliases FROM entities WHERE name='屿忱'"
    ).fetchone()
    aliases = json.loads(row["aliases"])
    assert "Stellan" in aliases
    cnt = db.execute(
        "SELECT COUNT(*) FROM entities WHERE kind='person'"
    ).fetchone()[0]
    assert cnt == 1


def test_entity_cosine_miss_inserts_new_row(db, monkeypatch):
    db.execute(
        "INSERT INTO entities (kind, name, source) VALUES (?, ?, ?)",
        ("person", "屿忱", "daily"),
    )
    db.commit()
    monkeypatch.setattr(
        semantic_dedup, "cosine_top_match", lambda conn, q, t: (0, 0.20),
    )
    n = candidates.write_entity_cand(db, _entity_raw("某陌生人"))
    assert n == 1
    cnt = db.execute(
        "SELECT COUNT(*) FROM entities WHERE kind='person'"
    ).fetchone()[0]
    assert cnt == 2
