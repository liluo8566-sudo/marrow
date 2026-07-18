"""Shared semantic-dedup primitives (bge-m3 cosine).

Callers: candidates.match_entity (dim upsert), reconcile, memes_dedup.

Embeds are L2-normalized → cosine = dot product. Embedder absent → returns
None; callers raise a one-shot warn alert and skip the cosine layer (string
checks still run).
"""
from __future__ import annotations

import sqlite3

from . import config

_DEFAULT_THRESHOLD = 0.85


def threshold_for(table: str) -> float:
    """Per-table cosine threshold; falls back to 0.85.
    Config key: `[<table>_dedup] cosine_threshold`. memes/milestones/entities
    read the same shape; absent section falls back to the default.
    """
    cfg = config.load().get(f"{table}_dedup", {}) or {}
    try:
        return float(cfg.get("cosine_threshold", _DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD


def cosine_top_match(conn: sqlite3.Connection, query: str,
                     targets: list[str]) -> tuple[int, float] | None:
    """Highest-cosine target for `query`. Returns (idx, score) or None
    when embedder absent. Empty targets → (-1, 0.0). Case-insensitive
    target dedup to cut inference cost.
    """
    from . import recall  # lazy: heavy onnxruntime import
    emb = recall._ensure_embedder()
    if emb is None:
        return None
    if not targets:
        return (-1, 0.0)
    # Preserve original index → caller can map back to source row.
    seen: dict[str, int] = {}
    uniq: list[str] = []
    uniq_to_orig: list[int] = []
    for i, t in enumerate(targets):
        if not t:
            continue
        lc = t.lower()
        if lc in seen:
            continue
        seen[lc] = i
        uniq.append(t)
        uniq_to_orig.append(i)
    if not uniq:
        return (-1, 0.0)
    vecs = emb.embed([query, *uniq])
    q = vecs[0]
    best_idx = -1
    best_score = 0.0
    for j, v in enumerate(vecs[1:]):
        s = float((q * v).sum())
        if s > best_score:
            best_score = s
            best_idx = uniq_to_orig[j]
    return (best_idx, best_score)


def cosine_max(conn: sqlite3.Connection, query: str,
               targets: list[str]) -> float | None:
    """Convenience wrapper: just the score, not the index. Used by callers
    that only need a hit/miss decision (memes_dedup, tasks, milestones).
    """
    hit = cosine_top_match(conn, query, targets)
    if hit is None:
        return None
    return hit[1]


def warn_embedder_missing(conn: sqlite3.Connection, alert_type: str,
                          source: str) -> None:
    """One-shot warn alert when cosine check can't run. Idempotent on
    (type, unresolved). Caller still proceeds with string-only dedup.
    """
    exists = conn.execute(
        "SELECT 1 FROM alerts WHERE type=? AND resolved=0 LIMIT 1",
        (alert_type,),
    ).fetchone()
    if exists:
        return
    conn.execute(
        "INSERT INTO alerts (severity, type, message, source)"
        " VALUES (?, ?, ?, ?)",
        ("warn", alert_type,
         f"bge-m3 unavailable; {alert_type} cosine dedup skipped",
         source),
    )
