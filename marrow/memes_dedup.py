"""Memes dedup gate: protects the memes table from concept-overlap with
milestones / entities / other memes.

Order of checks (called from daemon dim(action=upsert) before INSERT):
  1. fast_skip_already_rejected — (key, type) accumulated ≥ N persistent
     rejects → return 'fast_skip' so caller drops silently, no work.
  2. string_dup_against_milestone_entity — exact case-insensitive match on
     milestones.title / entities_live.name / entities_live.aliases (JSON).
  3. cosine_dup_against_active — bge-m3 1024d embedding compared to active
     memes.key, milestones.title, entities_live.name. Threshold from config.

Persistent rejects (dup_milestone / dup_entity / cosine_dup) are logged via
log_reject so the next round fast-skips. Freq-gate rejects are time-relative
and intentionally NOT logged.

Embedder unavailable → cosine step degrades to no-op + alert. Exact-string
dedup still runs.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3

from . import config

_PERSISTENT_REASONS = ("dup_milestone", "dup_entity", "cosine_dup")


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dedup_cfg() -> dict:
    cfg = config.load().get("memes_dedup", {}) or {}
    return {
        "cosine_threshold": float(cfg.get("cosine_threshold", 0.85)),
        "fast_skip_count": int(cfg.get("fast_skip_count", 3)),
    }


def fast_skip_already_rejected(conn: sqlite3.Connection, key: str,
                               vtype: str) -> bool:
    """True if (key, type) has accumulated >= fast_skip_count persistent
    rejects. Caller drops the candidate silently — no further checks.
    """
    threshold = _dedup_cfg()["fast_skip_count"]
    placeholders = ",".join("?" * len(_PERSISTENT_REASONS))
    row = conn.execute(
        f"SELECT COALESCE(SUM(count), 0) AS n FROM memes_reject_log "
        f"WHERE key=? AND type=? AND reason IN ({placeholders})",
        (key, vtype, *_PERSISTENT_REASONS),
    ).fetchone()
    return int(row["n"] if row else 0) >= threshold


def log_reject(conn: sqlite3.Connection, key: str, vtype: str,
               reason: str) -> None:
    """Increment (key, type, reason) counter. Only persistent reasons —
    callers must not pass freq_gate rejects here.
    """
    if reason not in _PERSISTENT_REASONS:
        return
    ts = _now_utc()
    conn.execute(
        "INSERT INTO memes_reject_log (key, type, reason, count,"
        " last_rejected_at) VALUES (?, ?, ?, 1, ?) "
        "ON CONFLICT(key, type, reason) DO UPDATE SET "
        "  count = count + 1, last_rejected_at = excluded.last_rejected_at",
        (key, vtype, reason, ts),
    )


def string_dup_reason(conn: sqlite3.Connection, key: str) -> str | None:
    """Exact case-insensitive match check.
    Returns 'dup_milestone' / 'dup_entity' / None.
    Aliases column is JSON text — parsed per row (small, ~20 entities).
    """
    if not key:
        return None
    k_lc = key.strip().lower()
    if not k_lc:
        return None
    m = conn.execute(
        "SELECT 1 FROM milestones WHERE LOWER(title)=? LIMIT 1", (k_lc,),
    ).fetchone()
    if m:
        return "dup_milestone"
    e_name = conn.execute(
        "SELECT 1 FROM entities_live WHERE LOWER(name)=? LIMIT 1", (k_lc,),
    ).fetchone()
    if e_name:
        return "dup_entity"
    alias_rows = conn.execute(
        "SELECT aliases FROM entities_live WHERE aliases IS NOT NULL"
        " AND aliases != ''"
    ).fetchall()
    for r in alias_rows:
        try:
            aliases = json.loads(r["aliases"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(aliases, list):
            continue
        for a in aliases:
            if isinstance(a, str) and a.strip().lower() == k_lc:
                return "dup_entity"
    return None


def cosine_dup_score(conn: sqlite3.Connection, key: str) -> float | None:
    """Highest cosine sim of `key` against active memes.key,
    milestones.title, entities_live.name. Returns None if embedder absent
    (caller skips cosine check + raises alert).

    Embeds are L2-normalized → cosine = dot product.
    """
    from . import semantic_dedup
    targets: list[str] = []
    for r in conn.execute(
        "SELECT key FROM memes WHERE status='active' AND key IS NOT NULL"
        " AND key != ''"
    ).fetchall():
        targets.append(r["key"])
    for r in conn.execute(
        "SELECT title FROM milestones WHERE title IS NOT NULL AND title != ''"
    ).fetchall():
        targets.append(r["title"])
    for r in conn.execute(
        "SELECT name FROM entities_live WHERE name IS NOT NULL AND name != ''"
    ).fetchall():
        targets.append(r["name"])
    return semantic_dedup.cosine_max(conn, key, targets)


def cosine_dup_threshold() -> float:
    return _dedup_cfg()["cosine_threshold"]


def warn_embedder_missing(conn: sqlite3.Connection) -> None:
    """One-shot warn alert when cosine check can't run. Dedup gate still
    runs string checks; this is an observability nudge, not a hard fail.
    Idempotent on (type, source) — duplicate row skipped.
    """
    exists = conn.execute(
        "SELECT 1 FROM alerts WHERE type='memes_dedup_no_embedder'"
        " AND resolved=0 LIMIT 1"
    ).fetchone()
    if exists:
        return
    conn.execute(
        "INSERT INTO alerts (severity, type, message, source)"
        " VALUES (?, ?, ?, ?)",
        ("warn", "memes_dedup_no_embedder",
         "bge-m3 unavailable; memes cosine dedup skipped (exact-string only)",
         "memes_dedup.py:cosine_dup_score"),
    )
