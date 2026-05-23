"""Shared candidate-block parser + writers.

Used by daily.py (ENTITY/MILESTONE/VOCAB candidate extraction on day-
aggregated digests) and sessionend_async.py (AFFECT/TASK_CAND block
parser via extract_block). Writers are idempotent on their natural key.
"""
from __future__ import annotations

import datetime as _dt
import json

_ENTITY_KINDS = {"person", "pref", "place"}
_MILESTONE_SCOPES = {"me", "us"}

# Vocab keys that must never age out — persona names, intimate shorthand.
# Type='cipher' is force-pinned regardless of key.
VOCAB_ANCHOR_KEYS: frozenset[str] = frozenset({
    "鸭子", "念念", "老公", "老婆", "Lumi", "屿忱", "Stellan",
})


def extract_block(text: str, marker: str) -> list | None:
    """Pull JSON list between ===<marker>=== and the next ===END===.
    Returns None on miss or parse error.
    """
    open_tag = f"==={marker}==="
    i = text.find(open_tag)
    if i == -1:
        return None
    tail = text[i + len(open_tag):]
    j = tail.find("===END===")
    body = tail[:j].strip() if j != -1 else tail.strip()
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def write_entity_cand(conn, raw: str, source: str = "daily") -> int:
    items = extract_block(raw, "ENTITY_CAND")
    if not items:
        return 0
    n = 0
    seen: set[tuple[str, str]] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            conf = float(it.get("conf", 0))
        except (TypeError, ValueError):
            conf = 0
        if conf < 0.8:
            continue
        kind = (it.get("kind") or "").strip()
        name = (it.get("name") or "").strip()
        if kind not in _ENTITY_KINDS or not name:
            continue
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)
        exists = conn.execute(
            "SELECT 1 FROM entities WHERE kind=? AND name=?"
            " AND superseded_by IS NULL LIMIT 1", (kind, name),
        ).fetchone()
        if exists:
            continue
        fact = (it.get("note") or "").strip() or None
        with conn:
            conn.execute(
                "INSERT INTO entities (kind, name, fact, source)"
                " VALUES (?, ?, ?, ?)",
                (kind, name, fact, source),
            )
        n += 1
    return n


def write_milestone_cand(conn, raw: str, date: str,
                         source: str = "daily") -> int:
    items = extract_block(raw, "MILESTONE_CAND")
    if not items:
        return 0
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            conf = float(it.get("conf", 0))
        except (TypeError, ValueError):
            conf = 0
        if conf < 0.85:
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        scope = it.get("scope") or "me"
        if scope not in _MILESTONE_SCOPES:
            scope = "me"
        m_date = it.get("date") or date
        desc = (it.get("description") or "").strip() or None
        with conn:
            conn.execute(
                "INSERT INTO milestones (scope, date, title, description,"
                " source_hash) VALUES (?, ?, ?, ?, ?)",
                (scope, m_date, title, desc, source),
            )
        n += 1
    return n


def write_vocab_cand(conn, raw: str, source: str = "daily",
                     anchor_keys: frozenset[str] = VOCAB_ANCHOR_KEYS) -> int:
    """Insert / bump vocab rows from a VOCAB_CAND block.

    pinned = 1 if anchor_keys hit OR type='cipher' OR LLM emitted pinned=1.
    For existing rows, pinned is upgraded (0→1) but never downgraded —
    once anchored, stays anchored even if a later session forgets the flag.
    """
    items = extract_block(raw, "VOCAB_CAND")
    if not items:
        return 0
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            conf = float(it.get("conf", 0))
        except (TypeError, ValueError):
            conf = 0
        if conf < 0.7:
            continue
        key = (it.get("key") or "").strip()
        if not key:
            continue
        vtype = it.get("type") or "phrase"
        value = (it.get("value") or "").strip() or None
        context = (it.get("context") or "").strip() or None
        try:
            llm_pinned = 1 if int(it.get("pinned", 0)) else 0
        except (TypeError, ValueError):
            llm_pinned = 0
        forced = key in anchor_keys or vtype == "cipher"
        pinned = 1 if (llm_pinned or forced) else 0
        row = conn.execute(
            "SELECT id, use_count, pinned FROM vocab WHERE type=? AND key=?"
            " LIMIT 1", (vtype, key),
        ).fetchone()
        ts_now = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        with conn:
            if row:
                new_pinned = 1 if (row["pinned"] or pinned) else 0
                conn.execute(
                    "UPDATE vocab SET use_count=use_count+1, last_seen=?,"
                    " pinned=? WHERE id=?",
                    (ts_now, new_pinned, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO vocab (type, key, value, context,"
                    " use_count, last_seen, pinned, source_hash)"
                    " VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                    (vtype, key, value, context, ts_now, pinned, source),
                )
        n += 1
    return n
