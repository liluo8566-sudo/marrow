"""Shared candidate-block parser + writers.

Used by daily.py (ENTITY/MILESTONE/MEMES candidate extraction on day-
aggregated digests) and sessionend_writers.py (AFFECT block parser via
extract_block). Writers are idempotent on their natural key.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
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

# Types subject to the 14d frequency gate (≥3 distinct event days or drop).
# All six types gated — low-frequency mentions belong in recall, not memes.
_MEMES_FREQ_GATED = {"paw", "meme", "news", "event", "fact", "others"}

# Types auto-pinned=1 regardless of LLM-emitted flag.
_MEMES_AUTO_PINNED = {"paw", "fact"}

# Memes keys that must never age out — configured persona/intimate shorthand.
MEMES_ANCHOR_KEYS: frozenset[str] = frozenset()


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
    names (bge-m3). Missing embedder → warn + name-only result. Sole matcher for
    both daily candidate ingest and the dim(action=upsert) MCP tool.
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
        raw_aliases = it.get("aliases")
        aliases_list: list[str] = []
        if isinstance(raw_aliases, list):
            aliases_list = [str(a).strip() for a in raw_aliases if str(a).strip()]
        # Gate: alias/name overlap + cosine dedup vs same-kind active names.
        # Hit → merge new name + aliases into the matched row (auto-learn
        # alias), not block: entity.aliases JSON exists exactly for this case.
        hit_id = match_entity(conn, kind, name, aliases_list)
        if hit_id is not None:
            _merge_aliases_into(conn, hit_id, name, aliases_list)
            continue
        fact = (it.get("note") or "").strip() or None
        aliases_json = (json.dumps(aliases_list, ensure_ascii=False)
                        if aliases_list else None)
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
        # Anti-revive: skip if Lumi has already dropped this milestone
        # (reconcile writes a tombstone keyed on milestones|scope|date|title).
        nk = f"milestones|{scope}|{m_date}|{title}"
        nh = hashlib.sha256(nk.encode()).hexdigest()
        tomb = conn.execute(
            "SELECT 1 FROM audit_log WHERE target_table='milestones'"
            " AND action='tombstone' AND summary LIKE ? LIMIT 1",
            (f"%{nh}%",),
        ).fetchone()
        if tomb:
            continue
        # Cosine dedup vs all milestones.title (all-in — table stays small,
        # <1 row/month, cheap to scan; protects against re-creation under
        # paraphrased wording).
        from . import semantic_dedup
        cos_targets = [
            r["title"] for r in conn.execute(
                "SELECT title FROM milestones WHERE title IS NOT NULL"
                " AND title != ''"
            ).fetchall()
        ]
        cos = semantic_dedup.cosine_max(conn, title, cos_targets)
        if cos is None:
            with conn:
                semantic_dedup.warn_embedder_missing(
                    conn, "milestones_dedup_no_embedder",
                    "candidates.write_milestone_cand",
                )
        elif cos >= semantic_dedup.threshold_for("milestones"):
            continue
        with conn:
            conn.execute(
                "INSERT INTO milestones (scope, date, title, description,"
                " source_hash, pinned) VALUES (?, ?, ?, ?, ?, 1)",
                (scope, m_date, title, desc, source),
            )
        n += 1
    return n


def _events_like_count_14d(conn, key: str, ref_date: str | None) -> int:
    """LEGACY text fallback — used only when embedder unavailable."""
    pat = "%" + key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    if ref_date:
        sql = (
            "SELECT COUNT(DISTINCT date(timestamp)) FROM events "
            "WHERE content LIKE ? ESCAPE '\\' "
            "AND timestamp >= datetime(?, '-14 days') "
            "AND timestamp < datetime(?, '+1 day')"
        )
        params = (pat, ref_date, ref_date)
    else:
        sql = (
            "SELECT COUNT(DISTINCT date(timestamp)) FROM events "
            "WHERE content LIKE ? ESCAPE '\\' "
            "AND timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-14 days')"
        )
        params = (pat,)
    try:
        return conn.execute(sql, params).fetchone()[0]
    except Exception:
        return 0


_FREQ_COSINE_THRESHOLD = 0.65
_FREQ_VEC_K = 200


def _events_semantic_count_14d(conn, key: str, ref_date: str | None) -> int:
    """Count distinct calendar days with semantically similar events in the
    14d window. Combines LIKE (exact text) and vec KNN (semantic) — returns
    the max of both so neither partial vec coverage nor paraphrasing causes
    false negatives. Cosine threshold 0.65 (looser than dedup 0.85 — here
    we want 'mentioned the topic', not 'exact duplicate').
    """
    like_count = _events_like_count_14d(conn, key, ref_date)

    from . import recall
    emb = recall._ensure_embedder()
    if emb is None:
        return like_count

    import math
    qvec = emb.embed([key])[0]
    qblob = recall._vec_to_blob(qvec)

    try:
        rows = conn.execute(
            "SELECT e.timestamp, v.distance "
            "FROM events_vec v JOIN events e ON e.id = v.rowid "
            "WHERE embedding MATCH ? AND k = ? "
            "ORDER BY v.distance",
            (qblob, _FREQ_VEC_K),
        ).fetchall()
    except Exception:
        return like_count

    # vec0 L2 distance on L2-normalized vecs: cos_sim = 1 - dist²/2
    max_l2 = math.sqrt(2 * (1 - _FREQ_COSINE_THRESHOLD))

    if ref_date:
        rd = _dt.date.fromisoformat(ref_date)
        win_start = (rd - _dt.timedelta(days=14)).isoformat()
        win_end = (rd + _dt.timedelta(days=1)).isoformat()
    else:
        now = _dt.datetime.now(_dt.timezone.utc)
        win_start = (now - _dt.timedelta(days=14)).isoformat()
        win_end = None

    dates: set[str] = set()
    for row in rows:
        dist = row["distance"]
        if dist > max_l2:
            break
        ts = row["timestamp"]
        if not ts:
            continue
        if ref_date:
            if ts < win_start or ts >= win_end:
                continue
        elif ts < win_start:
            continue
        dates.add(ts[:10])
    return max(like_count, len(dates))


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


def write_memes_cand(conn, raw: str, source: str = "daily",
                     anchor_keys: frozenset[str] | None = None,
                     date: str | None = None) -> int:
    """Insert / bump memes rows from a MEMES_CAND block.

    - Type whitelist: {paw, meme, news, event, fact, others}. Items with
      type outside this set are dropped silently.
    - Persistent-reject fast-skip (memes_reject_log SUM(count) ≥ N): drop
      the candidate before any further work — protects sonnet tokens from
      re-extracting a known dup next round.
    - 14d events_fts frequency gate applied to all six types — key
      must appear on ≥3 distinct days in events over the 14d window ending
      at `date` (or now).
    - Dedup against milestones.title / entities_live.name+aliases (exact
      case-insensitive) and cosine ≥ threshold against active memes.key /
      milestones.title / entities_live.name (bge-m3). Hits → reject + log.
    - Auto pinned=1 for type=paw / type=fact. For other types, pinned
      comes from LLM emission OR anchor-key force list.
    - On existing row, pinned is upgrade-only (0→1 stays, 1→0 never).
    """
    from . import config, memes_dedup  # local: avoid heavy import unless used
    if anchor_keys is None:
        anchor_keys = config.anchor_keys_set()
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
        # Fast-skip BEFORE any other gate — if this (key, type) has been
        # rejected ≥N times for persistent reasons (dup_milestone /
        # dup_entity / cosine_dup), don't burn cycles or sonnet tokens.
        if memes_dedup.fast_skip_already_rejected(conn, key, vtype):
            continue
        value = (it.get("value") or "").strip() or None
        try:
            llm_pinned = 1 if int(it.get("pinned", 0)) else 0
        except (TypeError, ValueError):
            llm_pinned = 0
        existing = conn.execute(
            "SELECT id, use_count, pinned FROM memes WHERE type=? AND key=?"
            " LIMIT 1", (vtype, key),
        ).fetchone()
        # Existing row → bump path (already accepted historically, no dedup).
        if existing:
            # Pinned default by type. paw/fact force pinned=1; other types
            # use llm flag OR anchor list.
            if vtype in _MEMES_AUTO_PINNED:
                pinned = 1
            else:
                pinned = 1 if (llm_pinned or key in anchor_keys) else 0
            ts_now = _dt.datetime.now(_dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            with conn:
                new_pinned = 1 if (existing["pinned"] or pinned) else 0
                # updated_at is NOT touched here — a re-mention with no
                # content change is not a content change. It only moves
                # when type/key/value actually change (elsewhere),
                # so reconcile's DB-wins check doesn't clobber hand md edits.
                conn.execute(
                    "UPDATE memes SET use_count=use_count+1, last_seen=?,"
                    " pinned=? WHERE id=?",
                    (ts_now, new_pinned, existing["id"]),
                )
            n += 1
            continue
        # New row path. Frequency gate first, then dedup (string + cosine).
        # Semantic match (vec KNN) counts topic mentions across paraphrases;
        # falls back to LIKE when embedder unavailable.
        if vtype in _MEMES_FREQ_GATED:
            if _events_semantic_count_14d(conn, key, date) < 3:
                continue
        # Exact-string dedup against milestones / entities (incl. aliases).
        reason = memes_dedup.string_dup_reason(conn, key)
        if reason is not None:
            with conn:
                memes_dedup.log_reject(conn, key, vtype, reason)
            continue
        # Cosine dedup (bge-m3). Skip on missing embedder (alert raised).
        cos = memes_dedup.cosine_dup_score(conn, key)
        if cos is None:
            with conn:
                memes_dedup.warn_embedder_missing(conn)
        elif cos >= memes_dedup.cosine_dup_threshold():
            with conn:
                memes_dedup.log_reject(conn, key, vtype, "cosine_dup")
            continue
        # Pinned default by type. paw/fact force pinned=1; other types use
        # llm flag OR anchor list.
        if vtype in _MEMES_AUTO_PINNED:
            pinned = 1
        else:
            pinned = 1 if (llm_pinned or key in anchor_keys) else 0
        ts_now = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        with conn:
            conn.execute(
                "INSERT INTO memes (type, key, value,"
                " use_count, last_seen, pinned, source_hash)"
                " VALUES (?, ?, ?, 1, ?, ?, ?)",
                (vtype, key, value, ts_now, pinned, source),
            )
        n += 1
    return n
