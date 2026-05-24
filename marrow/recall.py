"""Recall module: bge-m3 embedding + weighted scalar fusion retrieval.

Write path: embed_event(conn, event_id, text) -> events_vec + events_vec_meta.
Read path: recall_fusion(conn, query, ...) -> scored, decayed, deduped results.
Embedder: BAAI/bge-m3 via onnxruntime (no torch). Lazy-loaded singleton.

DECISIONS Phase 2: B3, B7, decay tier rules.
Fusion weights init: vec=0.55, bm25=0.30, recency=0.15, affect=0.10.

Milestones leg: small table (<=30 rows), LIKE-scan over title+description,
no FTS5/vec index. Tokenize query into CJK chars + ASCII runs; score by
matched-token ratio. Pinned rows get an additive boost. No vec/recency
weight — milestones are evergreen identity anchors.
"""
from __future__ import annotations

import math
import re
import sqlite3
import struct
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

if TYPE_CHECKING:
    from numpy.typing import NDArray


# ── embedder singleton ────────────────────────────────────────────────────────

_BGE_M3_HF_ID = "BAAI/bge-m3"
_BGE_M3_ONNX_SUBDIR = "onnx"
_EMBEDDER_LOCK = threading.Lock()
_EMBEDDER: "_BgeM3Embedder | None" = None


def _hf_cache_snapshot(repo_id: str) -> Path | None:
    """Return the snapshot dir for repo_id from HF hub cache, or None."""
    slug = repo_id.replace("/", "--")
    for base in (
        Path.home() / ".cache" / "huggingface" / "hub",
        Path("/tmp/fastembed_cache"),  # fastembed alt cache
    ):
        candidate = base / f"models--{slug}"
        snapshots = candidate / "snapshots"
        if snapshots.is_dir():
            snaps = sorted(snapshots.iterdir())
            if snaps:
                return snaps[-1]
    return None


class _BgeM3Embedder:
    """ONNX bge-m3: CLS-pool, L2-normalized, 1024d."""

    def __init__(self, model_dir: Path) -> None:
        self._tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tok.enable_padding(pad_id=0, pad_token="[PAD]")
        self._tok.enable_truncation(max_length=512)
        self._sess = ort.InferenceSession(
            str(model_dir / "model.onnx"),
            providers=["CPUExecutionProvider"],
        )

    def embed(self, texts: list[str]) -> NDArray[np.float32]:
        """Return shape (N, 1024), L2-normalized."""
        enc = self._tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        out = self._sess.run(None, {"input_ids": ids, "attention_mask": mask})
        # output[1] = sentence_embedding: already CLS-pooled + L2-normalized
        return out[1].astype(np.float32)


def _ensure_embedder() -> "_BgeM3Embedder | None":
    """Load embedder lazily; return None if model files absent."""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    with _EMBEDDER_LOCK:
        if _EMBEDDER is not None:
            return _EMBEDDER
        snap = _hf_cache_snapshot(_BGE_M3_HF_ID)
        if snap is None:
            return None
        onnx_dir = snap / _BGE_M3_ONNX_SUBDIR
        model_onnx = onnx_dir / "model.onnx"
        model_data = onnx_dir / "model.onnx_data"
        tok_json = onnx_dir / "tokenizer.json"
        if not (model_onnx.exists() and model_data.exists() and tok_json.exists()):
            return None
        _EMBEDDER = _BgeM3Embedder(onnx_dir)
    return _EMBEDDER


# ── vec serialization ─────────────────────────────────────────────────────────

def _vec_to_blob(v: NDArray[np.float32]) -> bytes:
    return struct.pack(f"{len(v)}f", *v.tolist())


def _blob_to_vec(b: bytes) -> NDArray[np.float32]:
    n = len(b) // 4
    return np.array(struct.unpack(f"{n}f", b), dtype=np.float32)


# ── write path ────────────────────────────────────────────────────────────────

# Cross-table vec lane recipes (2026-05-25). Each lane = (vec_table, meta_table,
# pending SQL). The pending query MUST select `id`, `text` columns; rowid in
# the vec/meta tables maps to id in the main table.
_LANES: dict[str, dict[str, str]] = {
    "events": {
        "vec_table": "events_vec",
        "meta_table": "events_vec_meta",
        "pending_sql": (
            "SELECT e.id AS id, e.content AS text FROM events e "
            "WHERE NOT EXISTS (SELECT 1 FROM events_vec_meta m "
            "                  WHERE m.rowid=e.id) "
            "ORDER BY e.id DESC LIMIT ?"
        ),
    },
    "memes": {
        "vec_table": "memes_vec",
        "meta_table": "memes_vec_meta",
        "pending_sql": (
            "SELECT m.id AS id, "
            "  TRIM(COALESCE(m.key,'') || "
            "       CASE WHEN COALESCE(m.value,'')!='' "
            "            THEN ': ' || m.value ELSE '' END || "
            "       CASE WHEN COALESCE(m.context,'')!='' "
            "            THEN ' (' || m.context || ')' ELSE '' END) AS text "
            "FROM memes m WHERE m.status='active' "
            "AND NOT EXISTS (SELECT 1 FROM memes_vec_meta x "
            "                WHERE x.rowid=m.id) "
            "ORDER BY m.id DESC LIMIT ?"
        ),
    },
    "entities": {
        "vec_table": "entities_vec",
        "meta_table": "entities_vec_meta",
        "pending_sql": (
            "SELECT e.id AS id, "
            "  TRIM(COALESCE(e.name,'') || "
            "       CASE WHEN COALESCE(e.kind,'')!='' "
            "            THEN ' (' || e.kind || ')' ELSE '' END || "
            "       CASE WHEN COALESCE(e.fact,'')!='' "
            "            THEN ': ' || e.fact ELSE '' END || "
            "       CASE WHEN COALESCE(e.aliases,'') NOT IN ('','[]') "
            "            THEN ' aliases:' || e.aliases ELSE '' END) AS text "
            "FROM entities e WHERE e.superseded_by IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM entities_vec_meta x "
            "                WHERE x.rowid=e.id) "
            "ORDER BY e.id DESC LIMIT ?"
        ),
    },
    "milestones": {
        "vec_table": "milestones_vec",
        "meta_table": "milestones_vec_meta",
        "pending_sql": (
            "SELECT mi.id AS id, "
            "  TRIM(COALESCE(mi.title,'') || "
            "       CASE WHEN COALESCE(mi.description,'')!='' "
            "            THEN ': ' || mi.description ELSE '' END) AS text "
            "FROM milestones mi "
            "WHERE NOT EXISTS (SELECT 1 FROM milestones_vec_meta x "
            "                  WHERE x.rowid=mi.id) "
            "ORDER BY mi.id DESC LIMIT ?"
        ),
    },
}


def _embed_one(
    conn: sqlite3.Connection,
    lane: str,
    rowid: int,
    text: str,
    embedder_id: str,
    dim: int,
) -> bool:
    """Idempotent single-row embed for any lane."""
    emb = _ensure_embedder()
    if emb is None:
        return False
    cfg = _LANES[lane]
    exists = conn.execute(
        f"SELECT 1 FROM {cfg['meta_table']} WHERE rowid=?", (rowid,)
    ).fetchone()
    if exists:
        return False
    vec = emb.embed([text])[0]
    blob = _vec_to_blob(vec)
    with conn:
        conn.execute(
            f"INSERT INTO {cfg['vec_table']}(rowid, embedding) VALUES(?, ?)",
            (rowid, blob),
        )
        conn.execute(
            f"INSERT INTO {cfg['meta_table']}(rowid, embedder_id, dim) "
            f"VALUES(?, ?, ?)",
            (rowid, embedder_id, dim),
        )
    return True


def embed_event(
    conn: sqlite3.Connection,
    event_id: int,
    text: str,
    embedder_id: str = "bge-m3",
    dim: int = 1024,
) -> bool:
    """Embed one event and write events_vec + events_vec_meta. Idempotent."""
    return _embed_one(conn, "events", event_id, text, embedder_id, dim)


def embed_meme(
    conn: sqlite3.Connection,
    meme_id: int,
    text: str,
    embedder_id: str = "bge-m3",
    dim: int = 1024,
) -> bool:
    """Embed one meme row into memes_vec + memes_vec_meta. Idempotent."""
    return _embed_one(conn, "memes", meme_id, text, embedder_id, dim)


def embed_entity(
    conn: sqlite3.Connection,
    entity_id: int,
    text: str,
    embedder_id: str = "bge-m3",
    dim: int = 1024,
) -> bool:
    """Embed one entity row into entities_vec + entities_vec_meta. Idempotent."""
    return _embed_one(conn, "entities", entity_id, text, embedder_id, dim)


def embed_milestone(
    conn: sqlite3.Connection,
    milestone_id: int,
    text: str,
    embedder_id: str = "bge-m3",
    dim: int = 1024,
) -> bool:
    """Embed one milestone into milestones_vec + milestones_vec_meta. Idempotent."""
    return _embed_one(conn, "milestones", milestone_id, text, embedder_id, dim)


def _embed_pending_lane(
    conn: sqlite3.Connection,
    lane: str,
    batch: int,
    embedder_id: str,
    dim: int,
) -> int:
    """Backfill one lane. Returns count written. Caller ensures embedder loaded."""
    emb = _ensure_embedder()
    if emb is None:
        return 0
    cfg = _LANES[lane]
    rows = conn.execute(cfg["pending_sql"], (batch,)).fetchall()
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    texts = [(r["text"] or "") for r in rows]
    vecs = emb.embed(texts)
    written = 0
    vt = cfg["vec_table"]
    mt = cfg["meta_table"]
    with conn:
        for rid, vec in zip(ids, vecs):
            conn.execute(
                f"INSERT OR IGNORE INTO {vt}(rowid, embedding) VALUES(?, ?)",
                (rid, _vec_to_blob(vec)),
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {mt}(rowid, embedder_id, dim) "
                f"VALUES(?, ?, ?)",
                (rid, embedder_id, dim),
            )
            written += 1
    return written


def embed_pending(
    conn: sqlite3.Connection,
    batch: int = 50,
    embedder_id: str = "bge-m3",
    dim: int = 1024,
) -> int:
    """Backfill all four lanes (events + memes + entities + milestones).

    Per-lane budget = `batch` so a large events backlog cannot starve the
    cross-table lanes on a single hook firing. Returns total rows written.
    """
    if _ensure_embedder() is None:
        return 0
    total = 0
    for lane in _LANES:
        total += _embed_pending_lane(conn, lane, batch, embedder_id, dim)
    return total


# ── decay helpers ─────────────────────────────────────────────────────────────

def _recency_score(timestamp_iso: str) -> float:
    """exp(-days/30), using event timestamp."""
    import datetime as _dt
    try:
        ts = _dt.datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        now = _dt.datetime.now(_dt.timezone.utc)
        days = max(0.0, (now - ts).total_seconds() / 86400.0)
    except Exception:
        days = 0.0
    return math.exp(-days / 30.0)


def _affect_bonus(conn: sqlite3.Connection, event_id: int | None) -> float:
    """Affect bonus from affect_live for the linked event, capped 0.10."""
    if event_id is None:
        return 0.0
    row = conn.execute(
        "SELECT importance FROM affect_live WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if not row:
        return 0.0
    imp = row["importance"] or 0
    return min(0.10, imp / 100.0)


def _decay_floor(importance: int | None, source: str | None, age_days: float) -> float:
    """DECISIONS Phase 2 decay FLOOR tiers (read-time)."""
    imp = importance or 0
    if source == "override" or imp >= 8:
        return 0.5   # Permanent
    if 4 <= imp <= 7:
        return 0.18
    # imp <= 3 & age > 90d -> dormant (excluded upstream, floor irrelevant)
    return 0.0


def _is_dormant(importance: int | None, age_days: float) -> bool:
    """Demote-sink: excluded from recall candidate pool."""
    imp = importance or 0
    return imp <= 3 and age_days > 90


# ── cross-table vec lane lookups ──────────────────────────────────────────────

# Minimum cosine similarity for a vec-only (no keyword match) row to surface.
# Stops bge-m3 noise (~0.25-0.35 sim on unrelated CN/EN queries) from polluting
# the candidate pool.
_VEC_ONLY_FLOOR = 0.40


def _memes_vec_hits(
    conn: sqlite3.Connection, qblob: bytes, k: int
) -> dict[int, float]:
    """Return {id: vec_score} for active memes matched by qblob."""
    try:
        rows = conn.execute(
            "SELECT m.id AS id, v.distance AS distance "
            "FROM memes_vec v JOIN memes m ON m.id = v.rowid "
            "WHERE m.status='active' AND embedding MATCH ? AND k = ? "
            "ORDER BY v.distance",
            (qblob, k),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {r["id"]: max(0.0, 1.0 - r["distance"]) for r in rows}


def _milestones_vec_hits(
    conn: sqlite3.Connection, qblob: bytes, k: int
) -> dict[int, float]:
    """Return {id: vec_score} for milestones matched by qblob."""
    try:
        rows = conn.execute(
            "SELECT mi.id AS id, v.distance AS distance "
            "FROM milestones_vec v JOIN milestones mi ON mi.id = v.rowid "
            "WHERE embedding MATCH ? AND k = ? "
            "ORDER BY v.distance",
            (qblob, k),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {r["id"]: max(0.0, 1.0 - r["distance"]) for r in rows}


def _entities_vec_hits(
    conn: sqlite3.Connection, qblob: bytes, k: int
) -> list[dict]:
    """Return entity cards (live only) matched by qblob, including vec_score."""
    try:
        rows = conn.execute(
            "SELECT e.id, e.kind, e.name, e.fact, e.mention_count, "
            "       e.created_at, v.distance "
            "FROM entities_vec v JOIN entities e ON e.id = v.rowid "
            "WHERE e.superseded_by IS NULL "
            "  AND embedding MATCH ? AND k = ? "
            "ORDER BY v.distance",
            (qblob, k),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for r in rows:
        vs = max(0.0, 1.0 - r["distance"])
        out.append({
            "id": r["id"],
            "kind": r["kind"] or "",
            "name": r["name"] or "",
            "fact": r["fact"] or "",
            "mention_count": r["mention_count"] or 0,
            "created_at": r["created_at"] or "",
            "vec_score": vs,
        })
    return out


# ── milestone keyword scan ────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")
# Milestone pinned-row boost added to raw fusion score before min_score gate.
_MILESTONE_PINNED_BOOST = 0.10


def _query_tokens(q: str) -> list[str]:
    """Split query into CJK chars + ASCII alnum runs, lowercased, deduped."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _TOKEN_RE.finditer(q):
        t = m.group(0).lower()
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _milestone_candidates(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[dict]:
    """LIKE-scan milestones; rank by matched-token ratio + pinned boost.

    Reverse-substring fallback: if title.lower() is a substring of query.lower(),
    boost kw_score to 1.0 — catches direct title typing past noisy tokens.

    Returns rows already shaped for fusion: timestamp (date as ISO),
    content (title[: description]), bm25 (kw_score), pinned.
    """
    tokens = _query_tokens(query)
    q_lower = query.lower()
    rows = conn.execute(
        "SELECT id, scope, date, title, description, pinned "
        "FROM milestones"
    ).fetchall()
    if not rows:
        return []
    scored: list[dict] = []
    for r in rows:
        title = r["title"] or ""
        desc = r["description"] or ""
        hay = (title + " " + desc).lower()
        title_l = title.lower()
        title_match = bool(title_l) and title_l in q_lower
        hits = sum(1 for t in tokens if t in hay) if tokens else 0
        if not hits and not title_match:
            continue
        kw_score = 1.0 if title_match else (hits / len(tokens))
        # Pinned only matters as a tiebreaker once final raw is computed;
        # carry the flag through so the fusion loop can add the boost.
        date = r["date"] or ""
        ts = date if "T" in date else (date + "T00:00:00Z" if date else "")
        content = title if not desc else f"{title}: {desc}"
        scored.append({
            "kind": "milestone",
            "id": r["id"],
            "session_id": None,
            "timestamp": ts,
            "role": "milestone",
            "content": content,
            "channel": None,
            "compressed": 0,
            "bm25": kw_score,
            "vec": 0.0,
            "fts_hit": True,
            "pinned": int(r["pinned"] or 0),
            "scope": r["scope"],
        })
    scored.sort(key=lambda c: (c["pinned"], c["bm25"]), reverse=True)
    return scored[: limit * 3]


# ── memes keyword scan ───────────────────────────────────────────────────────

def _memes_candidates(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[dict]:
    """Reverse-substring scan over active memes rows.

    Matches when key.lower() is a substring of query.lower(). Used so memes /
    cipher / nickname / phrase rows surface alongside events + milestones.
    Shape parallels milestone candidates; kind="memes".
    """
    q_lower = query.lower().strip()
    if not q_lower:
        return []
    rows = conn.execute(
        "SELECT id, type, key, value, context, pinned, use_count "
        "FROM memes WHERE status='active'"
    ).fetchall()
    if not rows:
        return []
    out: list[dict] = []
    for r in rows:
        key = r["key"] or ""
        if not key or key.lower() not in q_lower:
            continue
        value = r["value"] or ""
        ctx = r["context"] or ""
        content = f"{key}: {value}" if value else key
        if ctx:
            content = f"{content} ({ctx})"
        out.append({
            "kind": "memes",
            "id": r["id"],
            "session_id": None,
            "timestamp": "",
            "role": "memes",
            "content": content,
            "channel": None,
            "compressed": 0,
            "bm25": 1.0,
            "vec": 0.0,
            "fts_hit": True,
            "pinned": int(r["pinned"] or 0),
            "type": r["type"],
            "use_count": int(r["use_count"] or 0),
        })
    out.sort(key=lambda c: (c["pinned"], c["use_count"]), reverse=True)
    return out[: limit * 2]


# ── fusion retrieval ──────────────────────────────────────────────────────────

def recall_fusion(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    budget_chars: int = 4000,
    *,
    w_vec: float = 0.55,
    w_bm25: float = 0.30,
    w_recency: float = 0.15,
    w_affect: float = 0.10,
    w_memes_vec: float = 0.55,
    w_entities_vec: float = 0.55,
    w_milestones_vec: float = 0.55,
    min_score: float = 0.35,
) -> list[dict]:
    """Single weighted scalar fusion: vec + bm25 + recency + affect.

    Excludes dormant rows (imp<=3, age>90d, no FTS keyword revive).
    Applies FLOOR tiers to final score.
    FTS keyword hit on dormant row clears dormant flag before scoring.
    Returns rows sorted by score desc, truncated by budget_chars.
    """
    q = query.strip()
    if not q:
        return []

    emb = _ensure_embedder()
    vec_available = emb is not None

    # ── FTS candidates ────────────────────────────────────────────────────────
    fts_q = '"' + q.replace('"', '""') + '"'
    fts_rows = conn.execute(
        "SELECT e.id, e.session_id, e.timestamp, e.role, e.content, e.channel, "
        "e.compressed, rank AS fts_rank "
        "FROM events_fts f JOIN events e ON e.id = f.rowid "
        "WHERE events_fts MATCH ? ORDER BY rank LIMIT ?",
        (fts_q, limit * 3),
    ).fetchall()

    # ── vec candidates ────────────────────────────────────────────────────────
    vec_rows: list[sqlite3.Row] = []
    if vec_available:
        qvec = emb.embed([q])[0]
        qblob = _vec_to_blob(qvec)
        vec_rows = conn.execute(
            "SELECT e.id, e.session_id, e.timestamp, e.role, e.content, e.channel, "
            "e.compressed, v.distance "
            "FROM events_vec v JOIN events e ON e.id = v.rowid "
            "WHERE embedding MATCH ? AND k = ? "
            "ORDER BY v.distance",
            (qblob, limit * 3),
        ).fetchall()

    # ── merge candidates by event_id ──────────────────────────────────────────
    candidates: dict[int, dict] = {}

    # BM25: FTS5 rank is negative; smallest abs = best.
    # Normalize: best gets 1.0 (min_abs/rank_i), worst approaches 0.
    fts_ranks = [abs(r["fts_rank"]) for r in fts_rows]
    min_fts = min(fts_ranks) if fts_ranks else 1.0

    for i, r in enumerate(fts_rows):
        eid = r["id"]
        # min_fts / fts_rank[i]: best (=min) -> 1.0, worse ranks -> <1.0
        bm25_score = min_fts / fts_ranks[i] if fts_ranks[i] else 1.0
        candidates[eid] = {
            "id": eid, "session_id": r["session_id"],
            "timestamp": r["timestamp"], "role": r["role"],
            "content": r["content"], "channel": r["channel"],
            "compressed": r["compressed"],
            "bm25": bm25_score,
            "vec": 0.0, "fts_hit": True,
        }

    # Vec scores: distance is cosine distance (0=identical, 1=orthogonal)
    if vec_available:
        vec_dists = [r["distance"] for r in vec_rows]
        for i, r in enumerate(vec_rows):
            eid = r["id"]
            vec_score = max(0.0, 1.0 - vec_dists[i])
            if eid in candidates:
                candidates[eid]["vec"] = vec_score
            else:
                candidates[eid] = {
                    "id": eid, "session_id": r["session_id"],
                    "timestamp": r["timestamp"], "role": r["role"],
                    "content": r["content"], "channel": r["channel"],
                    "compressed": r["compressed"],
                    "bm25": 0.0, "vec": vec_score, "fts_hit": False,
                }

    # ── milestone + memes candidates (small tables, LIKE / substring scan) ──
    milestone_cands = _milestone_candidates(conn, q, limit)
    memes_cands = _memes_candidates(conn, q, limit)

    # ── cross-table vec lanes ────────────────────────────────────────────────
    # Vec hits fill in semantic matches the substring scans miss (e.g.
    # (我的猫) → entity (小胖)). Vec-only adds (no kw match) gated by
    # _VEC_ONLY_FLOOR to keep bge-m3 noise out.
    memes_vec_map: dict[int, float] = {}
    milestones_vec_map: dict[int, float] = {}
    entities_vec_cards: list[dict] = []
    if vec_available:
        k_lane = max(limit * 2, 5)
        memes_vec_map = _memes_vec_hits(conn, qblob, k_lane)
        milestones_vec_map = _milestones_vec_hits(conn, qblob, k_lane)
        entities_vec_cards = _entities_vec_hits(conn, qblob, k_lane)

    # Memes: merge vec hits into the substring pool by id.
    memes_by_id = {c["id"]: c for c in memes_cands}
    for mid, vs in memes_vec_map.items():
        if mid in memes_by_id:
            memes_by_id[mid]["vec"] = vs
        elif vs >= _VEC_ONLY_FLOOR:
            r = conn.execute(
                "SELECT id, type, key, value, context, pinned, use_count "
                "FROM memes WHERE id=? AND status='active'",
                (mid,),
            ).fetchone()
            if not r:
                continue
            key = r["key"] or ""
            value = r["value"] or ""
            ctx = r["context"] or ""
            content = f"{key}: {value}" if value else key
            if ctx:
                content = f"{content} ({ctx})"
            memes_by_id[mid] = {
                "kind": "memes", "id": mid,
                "session_id": None, "timestamp": "",
                "role": "memes", "content": content,
                "channel": None, "compressed": 0,
                "bm25": 0.0, "vec": vs, "fts_hit": False,
                "pinned": int(r["pinned"] or 0),
                "type": r["type"],
                "use_count": int(r["use_count"] or 0),
            }
    memes_cands = list(memes_by_id.values())

    # Milestones: merge vec hits into the keyword pool by id.
    ms_by_id = {c["id"]: c for c in milestone_cands}
    for mid, vs in milestones_vec_map.items():
        if mid in ms_by_id:
            ms_by_id[mid]["vec"] = vs
        elif vs >= _VEC_ONLY_FLOOR:
            r = conn.execute(
                "SELECT id, scope, date, title, description, pinned "
                "FROM milestones WHERE id=?",
                (mid,),
            ).fetchone()
            if not r:
                continue
            title = r["title"] or ""
            desc = r["description"] or ""
            date = r["date"] or ""
            ts = date if "T" in date else (date + "T00:00:00Z" if date else "")
            content = title if not desc else f"{title}: {desc}"
            ms_by_id[mid] = {
                "kind": "milestone", "id": mid,
                "session_id": None, "timestamp": ts,
                "role": "milestone", "content": content,
                "channel": None, "compressed": 0,
                "bm25": 0.0, "vec": vs, "fts_hit": False,
                "pinned": int(r["pinned"] or 0),
                "scope": r["scope"],
            }
    milestone_cands = list(ms_by_id.values())

    if (not candidates and not milestone_cands and not memes_cands
            and not entities_vec_cards):
        return []

    # ── dormant revive + scoring ──────────────────────────────────────────────
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    scored: list[tuple[float, dict]] = []
    for eid, c in candidates.items():
        ts = c["timestamp"]
        try:
            t = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age_days = max(0.0, (now - t).total_seconds() / 86400.0)
        except Exception:
            age_days = 0.0

        # Dormant check: look up affect importance for this event
        af_row = conn.execute(
            "SELECT importance FROM affect_live WHERE event_id=?", (eid,)
        ).fetchone()
        importance = af_row["importance"] if af_row else None

        if _is_dormant(importance, age_days):
            if c["fts_hit"]:
                # FTS keyword hit revives dormant: clear dormant flag
                conn.execute(
                    "UPDATE affect SET superseded_by=NULL "
                    "WHERE event_id=? AND superseded_by IS NULL "
                    "AND importance<=3",
                    (eid,),
                )
                # Re-read importance after revive (same row, cleared)
            else:
                continue  # exclude dormant without FTS hit

        recency = _recency_score(ts)
        affect_b = _affect_bonus(conn, eid)

        raw = (
            w_vec * c["vec"]
            + w_bm25 * c["bm25"]
            + w_recency * recency
            + w_affect * affect_b
        )

        # Fix C: mention_count booster via affect.entities JSON column.
        af_ent_row = conn.execute(
            "SELECT entities FROM affect_live WHERE event_id=?", (eid,)
        ).fetchone()
        if af_ent_row and af_ent_row["entities"]:
            try:
                import json as _json
                ent_list = _json.loads(af_ent_row["entities"])
                if isinstance(ent_list, list) and ent_list:
                    # Prod format = ["name", ...]; legacy/dict = [{"name": ...}].
                    names = []
                    for e in ent_list:
                        if isinstance(e, str):
                            names.append(e)
                        elif isinstance(e, dict) and e.get("name"):
                            names.append(e["name"])
                    if names:
                        placeholders = ",".join("?" * len(names))
                        mc_rows = conn.execute(
                            f"SELECT mention_count FROM entities_live "
                            f"WHERE name IN ({placeholders})",
                            names,
                        ).fetchall()
                        sum_mc = sum(r["mention_count"] or 0 for r in mc_rows)
                        if sum_mc > 0:
                            raw += min(0.1, 0.02 * math.log1p(sum_mc))
            except Exception:
                pass  # malformed JSON or missing column: skip booster

        source = None
        af_src = conn.execute(
            "SELECT source FROM affect_live WHERE event_id=?", (eid,)
        ).fetchone()
        if af_src:
            source = af_src["source"]

        floor = _decay_floor(importance, source, age_days)
        final = max(raw, floor) if floor > 0 else raw

        if final >= min_score:
            scored.append((final, {**c, "score": final}))

    # ── milestone scoring (recency/affect dropped — evergreen anchor —
    # bm25 + vec drive rank; no min_score gate so long queries don't dilute
    # the match into oblivion). ──────────────────────────────────────────────
    for mc in milestone_cands:
        raw = w_bm25 * mc["bm25"] + w_milestones_vec * mc.get("vec", 0.0)
        if mc["pinned"]:
            raw += _MILESTONE_PINNED_BOOST
        scored.append((raw, {**mc, "score": raw}))

    # ── memes scoring (mirror milestone: bm25 + vec + pinned boost) ──────────
    for vc in memes_cands:
        raw = w_bm25 * vc["bm25"] + w_memes_vec * vc.get("vec", 0.0)
        if vc["pinned"]:
            raw += _MILESTONE_PINNED_BOOST
        scored.append((raw, {**vc, "score": raw}))

    scored.sort(key=lambda x: x[0], reverse=True)

    # ── entity force-include (prepend before ms_cap reservation) ─────────────
    # Two streams: substring/LIKE via entity_recall, semantic via entities_vec.
    # Dedup by entity id; substring score wins when both fire (it carries the
    # +0.5 card boost already).
    from .entity_recall import entity_force_include
    force_rows = entity_force_include(conn, q, limit)
    seen_entity_ids = {
        r["id"] for r in force_rows if r.get("kind") == "entity"
    }
    for card in entities_vec_cards:
        if card["id"] in seen_entity_ids:
            continue
        vs = card["vec_score"]
        if vs < _VEC_ONLY_FLOOR:
            continue
        fact = card["fact"]
        if not fact:
            continue
        name = card["name"]
        ekind = card["kind"]
        content = f"{name} ({ekind}): {fact}" if ekind else f"{name}: {fact}"
        score = (
            w_entities_vec * vs
            + 0.5
            + 0.1 * math.log1p(card["mention_count"])
        )
        force_rows.append({
            "kind": "entity", "id": card["id"],
            "session_id": None,
            "timestamp": card["created_at"],
            "role": "entity", "content": content,
            "channel": None, "compressed": 0,
            "bm25": 0.0, "vec": vs, "fts_hit": False,
            "score": score, "force_include": True,
        })
        seen_entity_ids.add(card["id"])

    force_ids = {r["id"] for r in force_rows}
    # Remove any fusion duplicates that force-include already covers.
    scored = [(s, r) for s, r in scored if r.get("id") not in force_ids]
    # Prepend force rows (score already set, kind already "event").
    force_pairs = [(r["score"], r) for r in force_rows]
    scored = force_pairs + scored
    scored.sort(key=lambda x: x[0], reverse=True)

    # ── reserved milestone + memes slots ──────────────────────────────────────
    # Events can outrank milestones / memes on score (recency + affect +
    # fts_hit). Reserve slots so anchor rows aren't starved on long queries.
    # Adaptive: when >=3 strong FTS hits exist, drop both caps so entity-dense
    # queries don't waste budget on anchors.
    strong_fts_count = sum(
        1 for _, r in scored
        if r.get("kind") not in ("milestone", "memes")
        and r.get("bm25", 0.0) >= 0.5
    )
    if strong_fts_count >= 3:
        ms_cap = 1
        memes_cap = 0
    else:
        ms_cap = max(1, (limit + 2) // 3)
        memes_cap = 1 if limit <= 5 else 2
    ms_scored = [(s, r) for s, r in scored if r.get("kind") == "milestone"]
    memes_scored = [(s, r) for s, r in scored if r.get("kind") == "memes"]
    ev_scored = [
        (s, r) for s, r in scored
        if r.get("kind") not in ("milestone", "memes")
    ]
    ms_picks = ms_scored[:ms_cap]
    memes_picks = memes_scored[:memes_cap]
    reserved = len(ms_picks) + len(memes_picks)
    ev_picks = ev_scored[: max(0, limit - reserved)]
    picks = sorted(
        ms_picks + memes_picks + ev_picks, key=lambda x: x[0], reverse=True
    )

    # ── budget truncation ─────────────────────────────────────────────────────
    # Per-item cap = budget_chars // limit so one long hit can't starve the
    # rest. Global budget still enforced as a backstop.
    per_item_cap = max(1, budget_chars // max(1, limit))
    out: list[dict] = []
    used = 0
    for _, row in picks[:limit]:
        content = row["content"] or ""
        if len(content) > per_item_cap:
            content = content[:per_item_cap]
        if used + len(content) > budget_chars:
            content = content[: max(0, budget_chars - used)]
        row = {**row, "content": content}
        out.append(row)
        used += len(content)
        if used >= budget_chars:
            break

    return out


# ── config-driven entrypoint (used by hook + MCP daemon) ─────────────────────

def recall_with_config(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int | None = None,
    budget_chars: int | None = None,
) -> list[dict]:
    """Run recall_fusion with weights + thresholds from [recall] config.

    Single shared path so hook (UserPromptSubmit) and MCP daemon return the
    same shape for the same query. Caller may override limit/budget per call.
    """
    from . import config as _config
    rcfg = _config.load().get("recall", {})
    return recall_fusion(
        conn, query,
        limit=int(limit if limit is not None else rcfg.get("limit", 15)),
        budget_chars=int(
            budget_chars if budget_chars is not None
            else rcfg.get("budget_chars", 4000)
        ),
        w_vec=float(rcfg.get("w_vec", 0.55)),
        w_bm25=float(rcfg.get("w_bm25", 0.30)),
        w_recency=float(rcfg.get("w_recency", 0.15)),
        w_affect=float(rcfg.get("w_affect", 0.10)),
        w_memes_vec=float(rcfg.get("w_memes_vec", 0.55)),
        w_entities_vec=float(rcfg.get("w_entities_vec", 0.55)),
        w_milestones_vec=float(rcfg.get("w_milestones_vec", 0.55)),
        min_score=float(rcfg.get("min_score", 0.35)),
    )
