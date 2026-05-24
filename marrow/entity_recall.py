"""Entity-aware force-include for recall: surface events linked to named entities.

When the query contains an entity name (person/place/pref), pull events that
mention that entity via FTS5 match — bypassing the normal fusion score gate.
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
    """Return events force-linked to entities matched in query.

    - Tokenizes query with same logic as recall._query_tokens.
    - Matches entity names in entities_live via LIKE on each token.
    - Fetches events via FTS5 MATCH on entity name (trigram tokenizer).
    - Scores: 1.0 + 0.1 * log1p(mention_count) to outrank fusion scores.
    - Caps total returned at limit // 2 (min 1) to avoid flooding.
    - Deduplicates by event id.
    - Returns empty list when entities table is empty or no tokens match.
    """
    from .recall import _query_tokens

    tokens = _query_tokens(query)
    if not tokens:
        return []

    # Match entities by name LIKE token.
    matched_entities: list[dict] = []
    seen_eid: set[int] = set()
    seen_entity_id: set[int] = set()

    for token in tokens:
        if len(token) < 2:
            # Single-char tokens produce too many false positives.
            continue
        rows = conn.execute(
            "SELECT id, name, mention_count FROM entities_live "
            "WHERE name LIKE ?",
            (f"%{token}%",),
        ).fetchall()
        for r in rows:
            eid = r["id"]
            if eid not in seen_entity_id:
                seen_entity_id.add(eid)
                matched_entities.append({
                    "id": eid,
                    "name": r["name"],
                    "mention_count": r["mention_count"] or 0,
                })

    if not matched_entities:
        return []

    force_cap = max(1, limit // 2)
    results: list[dict] = []

    for entity in matched_entities:
        if len(results) >= force_cap:
            break
        name = entity["name"]
        mc = entity["mention_count"]
        score = 1.0 + 0.1 * math.log1p(mc)

        # FTS5 MATCH on entity name to find linked events.
        # Trigram tokenizer requires >=3 chars; fall back to LIKE for short names.
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

        if not event_rows and len(name) >= 2:
            # Fallback: LIKE scan when FTS5 returns nothing (short name / edge).
            event_rows = conn.execute(
                "SELECT id, session_id, timestamp, role, content, "
                "channel, compressed FROM events "
                "WHERE content LIKE ? LIMIT ?",
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

    return results
