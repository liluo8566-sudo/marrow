"""Entity-aware force-include for recall: surface events linked to named entities.

Reverse-match approach: iterate entities_live rows; for each row check if
name.lower() is a substring of query.lower(). No tokenizer dependency, fully
CJK-safe (catches 2-char names like (南南) that the trigram tokenizer / char-
split tokens silently drop).

Force-include rows are prepended in recall_fusion before ms_cap reservation.
"""
from __future__ import annotations

import math
import sqlite3


def entity_force_include(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
) -> list[dict]:
    """Return events force-linked to entities whose name appears in query.

    - Reverse substring match: name.lower() in query.lower().
    - Inner event fetch: LIKE-scan for name <3 chars (trigram tokenizer floor);
      else try FTS5 first, fall back to LIKE on empty.
    - Scores: 1.0 + 0.1 * log1p(mention_count).
    - Cap at limit // 2 (min 1).
    - Dedup by event id.
    """
    q_lower = query.lower().strip()
    if not q_lower:
        return []

    rows = conn.execute(
        "SELECT id, kind, name, fact, aliases, mention_count, created_at "
        "FROM entities_live"
    ).fetchall()
    matched: list[dict] = []
    for r in rows:
        name = r["name"] or ""
        if not name:
            continue
        triggers = [name]
        raw_aliases = r["aliases"] if "aliases" in r.keys() else None
        if raw_aliases:
            try:
                import json as _json
                parsed = _json.loads(raw_aliases)
                if isinstance(parsed, list):
                    triggers.extend(str(a) for a in parsed if a)
            except Exception:
                pass
        if any(t and t.lower() in q_lower for t in triggers):
            matched.append({
                "id": r["id"],
                "kind": r["kind"] or "",
                "name": name,
                "fact": r["fact"] or "",
                "mention_count": r["mention_count"] or 0,
                "created_at": r["created_at"] or "",
            })

    if not matched:
        return []

    # Longer names first — more specific (rules out (南) matching when (南南) is present).
    matched.sort(key=lambda e: len(e["name"]), reverse=True)

    force_cap = max(1, limit // 2)
    results: list[dict] = []
    seen_eid: set[int] = set()

    for entity in matched:
        if len(results) >= force_cap:
            break
        name = entity["name"]
        ekind = entity["kind"]
        fact = entity["fact"]
        mc = entity["mention_count"]
        score = 1.0 + 0.1 * math.log1p(mc)

        # Entity-card: the entity row's own fact field. Outranks event score so
        # the identity sheet always lands first in the recall block.
        # Timestamp = entities.created_at so same-score cards order newest-first
        # (Outcome 2, 2026-05-25 goal).
        if fact:
            card_content = f"{name} ({ekind}): {fact}" if ekind else f"{name}: {fact}"
            results.append({
                "kind": "entity",
                "id": entity["id"],
                "session_id": None,
                "timestamp": entity.get("created_at", ""),
                "role": "entity",
                "content": card_content,
                "channel": None,
                "compressed": 0,
                "bm25": 1.0,
                "vec": 0.0,
                "fts_hit": True,
                "score": score + 0.5,
                "force_include": True,
            })
            if len(results) >= force_cap:
                break

        event_rows: list = []
        if len(name) >= 3:
            try:
                fts_q = '"' + name.replace('"', '""') + '"'
                event_rows = conn.execute(
                    "SELECT e.id, e.session_id, e.timestamp, e.role, "
                    "e.content, e.channel, e.compressed "
                    "FROM events_fts f JOIN events e ON e.id = f.rowid "
                    "WHERE events_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_q, force_cap * 2),
                ).fetchall()
            except Exception:
                event_rows = []

        if not event_rows:
            event_rows = conn.execute(
                "SELECT id, session_id, timestamp, role, content, "
                "channel, compressed FROM events "
                "WHERE content LIKE ? ORDER BY timestamp DESC LIMIT ?",
                (f"%{name}%", force_cap * 2),
            ).fetchall()

        for er in event_rows:
            if len(results) >= force_cap:
                break
            evid = er["id"]
            if evid in seen_eid:
                continue
            seen_eid.add(evid)
            results.append({
                "kind": "event",
                "id": evid,
                "session_id": er["session_id"],
                "timestamp": er["timestamp"],
                "role": er["role"],
                "content": er["content"],
                "channel": er["channel"],
                "compressed": er["compressed"],
                "bm25": 1.0,
                "vec": 0.0,
                "fts_hit": True,
                "score": score,
                "force_include": True,
            })

    # Tiebreaker: equal-score rows sort newest-first by timestamp (Outcome 2).
    # Empty / unparseable timestamps sort oldest (back of the tie).
    results.sort(
        key=lambda r: (
            -float(r.get("score") or 0.0),
            -_ts_sort_key(r.get("timestamp") or ""),
        ),
    )
    return results


def _ts_sort_key(ts: str) -> float:
    """Map an ISO timestamp to a sortable float; empty/bad ts sorts oldest."""
    if not ts:
        return 0.0
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(
            ts.replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        return 0.0
