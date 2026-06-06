"""Entity recall helpers: mention-count bump for indexed rows."""
from __future__ import annotations

import json
import sqlite3


def _entity_triggers(name: str, raw_aliases) -> list[str]:
    """Return lower-cased trigger strings for an entity (name + JSON aliases)."""
    triggers: list[str] = []
    if name:
        triggers.append(name.lower())
    if raw_aliases:
        try:
            parsed = json.loads(raw_aliases)
            if isinstance(parsed, list):
                for a in parsed:
                    if a:
                        triggers.append(str(a).lower())
        except Exception:
            pass
    return triggers


def bump_mention_counts(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Scan event contents against entities_live; +1 per entity per matching event.

    Same reverse-substring + alias logic as entity_force_include. Each event
    contributes at most +1 per entity (set-dedup per row). Returns total bumps.
    Caller owns the transaction (no commit here).
    """
    if not rows:
        return 0
    ents = conn.execute(
        "SELECT id, name, aliases FROM entities_live"
    ).fetchall()
    if not ents:
        return 0
    triggers = [(r["id"], _entity_triggers(r["name"] or "", r["aliases"])) for r in ents]
    bumps: dict[int, int] = {}
    for row in rows:
        if row.get("role") not in ("user", "assistant"):
            continue
        content = (row.get("content") or "").lower()
        if not content:
            continue
        hit: set[int] = set()
        for eid, trigs in triggers:
            if eid in hit:
                continue
            if any(t and t in content for t in trigs):
                hit.add(eid)
        for eid in hit:
            bumps[eid] = bumps.get(eid, 0) + 1
    for eid, n in bumps.items():
        conn.execute(
            "UPDATE entities SET mention_count = mention_count + ? "
            "WHERE id = ? AND superseded_by IS NULL",
            (n, eid),
        )
    return sum(bumps.values())


