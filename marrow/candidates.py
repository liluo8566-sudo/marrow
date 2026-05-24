"""Shared candidate-block parser + writers.

Used by daily.py (ENTITY/MILESTONE/MEMES candidate extraction on day-
aggregated digests) and sessionend_async.py (AFFECT/TASK_CAND block
parser via extract_block). Writers are idempotent on their natural key.
"""
from __future__ import annotations

import datetime as _dt
import json

_ENTITY_KINDS = {"person", "pref", "place"}
_MILESTONE_SCOPES = {"me", "us"}

# Memes type enum (v2). Six values:
#   paw    — Lumi/dyad-exclusive inside jokes (绿茶豹, 大笨鸭子)
#   meme   — public/network memes (not Lumi's invention)
#   news   — topical public news
#   event  — PUBLIC events (earthquake, election, public concert)
#   fact   — Lumi's OWN persistent configuration/setup facts
#   others — catch-all reserved slot
_MEMES_TYPES = {"paw", "meme", "news", "event", "fact", "others"}

# Types subject to the 7d events_fts frequency gate (≥3 hits or drop).
_MEMES_FREQ_GATED = {"meme", "news", "event"}

# Types auto-pinned=1 regardless of LLM-emitted flag.
_MEMES_AUTO_PINNED = {"paw", "fact"}

# Memes keys that must never age out — persona names, intimate shorthand.
MEMES_ANCHOR_KEYS: frozenset[str] = frozenset({
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
        raw_aliases = it.get("aliases")
        aliases_json = None
        if isinstance(raw_aliases, list):
            cleaned = [str(a).strip() for a in raw_aliases if str(a).strip()]
            if cleaned:
                aliases_json = json.dumps(cleaned, ensure_ascii=False)
        with conn:
            conn.execute(
                "INSERT INTO entities (kind, name, fact, aliases, source)"
                " VALUES (?, ?, ?, ?, ?)",
                (kind, name, fact, aliases_json, source),
            )
        n += 1
    return n


def write_milestone_cand(conn, raw: str, date: str,
                         source: str = "daily") -> int:
    """Insert milestone candidates. Dedup on (scope, title, date) — second
    write with the same key is a no-op (matches entity skip-on-conflict).
    """
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
        exists = conn.execute(
            "SELECT 1 FROM milestones WHERE scope=? AND title=? AND date=?"
            " LIMIT 1", (scope, title, m_date),
        ).fetchone()
        if exists:
            continue
        with conn:
            conn.execute(
                "INSERT INTO milestones (scope, date, title, description,"
                " source_hash) VALUES (?, ?, ?, ?, ?)",
                (scope, m_date, title, desc, source),
            )
        n += 1
    return n


def _fts_phrase(q: str) -> str:
    # Mirror aging._fts_phrase: phrase match, FTS5-safe (trigram tokenizer).
    return '"' + q.replace('"', '""').strip() + '"'


def _events_fts_count_7d(conn, key: str, ref_date: str | None) -> int:
    """Count events_fts hits for `key` over the 7d window ending at ref_date
    (or now if None). Returns 0 on FTS error (malformed expression).
    """
    if ref_date:
        sql = (
            "SELECT COUNT(DISTINCT f.rowid) FROM events_fts f "
            "JOIN events e ON e.id = f.rowid "
            "WHERE events_fts MATCH ? "
            "AND e.timestamp >= datetime(?, '-7 days') "
            "AND e.timestamp < datetime(?, '+1 day')"
        )
        params = (_fts_phrase(key), ref_date, ref_date)
    else:
        sql = (
            "SELECT COUNT(DISTINCT f.rowid) FROM events_fts f "
            "JOIN events e ON e.id = f.rowid "
            "WHERE events_fts MATCH ? "
            "AND e.timestamp >= datetime('now', '-7 days')"
        )
        params = (_fts_phrase(key),)
    try:
        return conn.execute(sql, params).fetchone()[0]
    except Exception:
        return 0


def write_memes_cand(conn, raw: str, source: str = "daily",
                     anchor_keys: frozenset[str] = MEMES_ANCHOR_KEYS,
                     date: str | None = None) -> int:
    """Insert / bump memes rows from a MEMES_CAND block.

    - Type whitelist: {paw, meme, news, event, fact, others}. Items with
      type outside this set are dropped silently.
    - 7d events_fts frequency gate applied to meme/news/event only — key
      must appear ≥3 times in events over the 7d window ending at `date`
      (or now). paw/fact/others bypass the gate.
    - Auto pinned=1 for type=paw / type=fact. For other types, pinned
      comes from LLM emission OR anchor-key force list.
    - On existing row, pinned is upgrade-only (0→1 stays, 1→0 never).
    """
    items = extract_block(raw, "MEMES_CAND")
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
        vtype = (it.get("type") or "").strip()
        if vtype not in _MEMES_TYPES:
            continue
        value = (it.get("value") or "").strip() or None
        context = (it.get("context") or "").strip() or None
        try:
            llm_pinned = 1 if int(it.get("pinned", 0)) else 0
        except (TypeError, ValueError):
            llm_pinned = 0
        # Frequency gate — public-meme types must be repeated ≥3 times in
        # 7d events. paw/fact/others are direct insert.
        if vtype in _MEMES_FREQ_GATED:
            if _events_fts_count_7d(conn, key, date) < 3:
                continue
        # Pinned default by type. paw/fact force pinned=1; other types use
        # llm flag OR anchor list.
        if vtype in _MEMES_AUTO_PINNED:
            pinned = 1
        else:
            pinned = 1 if (llm_pinned or key in anchor_keys) else 0
        row = conn.execute(
            "SELECT id, use_count, pinned FROM memes WHERE type=? AND key=?"
            " LIMIT 1", (vtype, key),
        ).fetchone()
        ts_now = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        with conn:
            if row:
                new_pinned = 1 if (row["pinned"] or pinned) else 0
                conn.execute(
                    "UPDATE memes SET use_count=use_count+1, last_seen=?,"
                    " pinned=? WHERE id=?",
                    (ts_now, new_pinned, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO memes (type, key, value, context,"
                    " use_count, last_seen, pinned, source_hash)"
                    " VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                    (vtype, key, value, context, ts_now, pinned, source),
                )
        n += 1
    return n
