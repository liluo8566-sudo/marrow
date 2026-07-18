"""Entity-match + alias-merge helpers, plus meme use_count bump.

Survivors after the daily-candidate pipeline retirement:
- match_entity / _merge_aliases_into / _alias_dedup_lookup — the dim(action=
  upsert) MCP tool's entity dedup (daemon.py).
- bump_use_counts — per-turn meme use_count bump from repo.archive_events.
"""
from __future__ import annotations

import datetime as _dt
import json

_ENTITY_KINDS = {"person", "pref", "place"}


def _alias_dedup_lookup(conn, kind: str, name: str,
                        new_aliases: list[str]) -> int | None:
    """Find an active entity row in `kind` whose name or aliases overlap
    (case-insensitive) with the incoming `name` ∪ `new_aliases`. Returns
    the row id on hit, None on miss. Synonym-aware sibling of the legacy
    (kind, name) exact check — prevents alias entities re-inserting as
    fresh rows.
    """
    needles = {name.strip().lower()}
    for a in new_aliases:
        s = a.strip().lower()
        if s:
            needles.add(s)
    if not needles:
        return None
    rows = conn.execute(
        "SELECT id, name, aliases FROM entities"
        " WHERE kind=? AND superseded_by IS NULL", (kind,),
    ).fetchall()
    for r in rows:
        if (r["name"] or "").strip().lower() in needles:
            return r["id"]
        raw = r["aliases"]
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, list):
            continue
        for a in parsed:
            if isinstance(a, str) and a.strip().lower() in needles:
                return r["id"]
    return None


def _merge_aliases_into(conn, row_id: int, incoming_name: str,
                        incoming_aliases: list[str]) -> None:
    """Merge `incoming_name` (if distinct from row.name) and
    `incoming_aliases` into row.aliases JSON. Case-insensitive dedup;
    no-op when nothing new. Caller owns the transaction context.
    """
    row = conn.execute(
        "SELECT name, aliases FROM entities WHERE id=?", (row_id,),
    ).fetchone()
    if row is None:
        return
    existing: list[str] = []
    raw = row["aliases"]
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                existing = [str(a).strip() for a in parsed if str(a).strip()]
        except (TypeError, ValueError, json.JSONDecodeError):
            existing = []
    canonical_lc = (row["name"] or "").strip().lower()
    seen_lc = {canonical_lc} | {a.lower() for a in existing}
    additions: list[str] = []
    candidates_in = [incoming_name, *incoming_aliases]
    for cand in candidates_in:
        s = cand.strip()
        if not s:
            continue
        if s.lower() in seen_lc:
            continue
        seen_lc.add(s.lower())
        additions.append(s)
    if not additions:
        return
    merged = existing + additions
    with conn:
        conn.execute(
            "UPDATE entities SET aliases=?, updated_at=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False),
             _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             row_id),
        )


def match_entity(conn, kind: str, name: str, aliases_list: list[str],
                 *, source: str = "candidates.write_entity_cand") -> int | None:
    """Return the id of an active entity in `kind` matching `name`/aliases, or
    None. Two-stage gate: alias/name overlap, then cosine top-match on same-kind
    names (bge-m3). Missing embedder → warn + name-only result. Matcher for the
    dim(action=upsert) MCP tool.
    """
    hit_id = _alias_dedup_lookup(conn, kind, name, aliases_list)
    if hit_id is not None:
        return hit_id
    from . import semantic_dedup
    kind_rows = conn.execute(
        "SELECT id, name FROM entities WHERE kind=?"
        " AND superseded_by IS NULL", (kind,),
    ).fetchall()
    if not kind_rows:
        return None
    target_names = [r["name"] for r in kind_rows]
    match = semantic_dedup.cosine_top_match(conn, name, target_names)
    if match is None:
        with conn:
            semantic_dedup.warn_embedder_missing(
                conn, "entities_dedup_no_embedder", source,
            )
        return None
    idx, score = match
    if idx >= 0 and score >= semantic_dedup.threshold_for("entities"):
        return kind_rows[idx]["id"]
    return None


def bump_use_counts(conn, rows: list[dict]) -> int:
    """Scan inserted event contents against active memes; +1 use_count per
    meme per matching event (set-dedup per row). Caller owns the transaction
    (no commit here). Mirrors entity_recall.bump_mention_counts.

    Substring match (case-insensitive) on memes.key — same approach as the
    insertion-time gate, so a key that passed the gate also bumps reliably.
    Only status='active' rows participate; dormant memes don't auto-revive
    here (aging.py owns revive).
    """
    if not rows:
        return 0
    memes = conn.execute(
        "SELECT id, key FROM memes WHERE status='active'"
    ).fetchall()
    if not memes:
        return 0
    triggers = [(m["id"], (m["key"] or "").lower()) for m in memes if m["key"]]
    bumps: dict[int, int] = {}
    for row in rows:
        if row.get("role") not in ("user", "assistant"):
            continue
        content = (row.get("content") or "").lower()
        if not content:
            continue
        hit: set[int] = set()
        for mid, key_lc in triggers:
            if mid in hit:
                continue
            if key_lc and key_lc in content:
                hit.add(mid)
        for mid in hit:
            bumps[mid] = bumps.get(mid, 0) + 1
    if not bumps:
        return 0
    ts_now = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    for mid, n in bumps.items():
        conn.execute(
            "UPDATE memes SET use_count = use_count + ?, last_seen = ?, updated_at = ? "
            "WHERE id = ? AND status='active'",
            (n, ts_now, ts_now, mid),
        )
    return sum(bumps.values())
