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

import datetime
import json
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


def _body_nonempty(body) -> bool:
    """True iff `body` carries at least one non-whitespace character.

    Used by recall fusion to drop entity force-include rows whose content
    is None / "" / whitespace-only — surfacing such rows wastes prompt
    tokens without adding signal.
    """
    if not body:
        return False
    if not isinstance(body, str):
        return True  # non-string truthy — preserve, downstream owns shape
    return bool(body.strip())


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
    # Diary table is keyed by `date TEXT PRIMARY KEY`, not INTEGER. vec0 rowid
    # must be INTEGER, so we ride SQLite's implicit `rowid` column (auto-assigned
    # to every table without an explicit INTEGER PRIMARY KEY). Stable across
    # re-opens; reassigned on DELETE+INSERT (daily.py rewrites by date). Orphan
    # rows in diary_vec_meta whose rowid no longer exists in diary are swept by
    # _embed_pending_lane before the per-lane backfill query runs.
    "diary": {
        "vec_table": "diary_vec",
        "meta_table": "diary_vec_meta",
        "pending_sql": (
            "SELECT d.rowid AS id, "
            "  TRIM(d.date || ': ' || COALESCE(d.content,'')) AS text "
            "FROM diary d WHERE COALESCE(d.content,'') NOT IN ('','—') "
            "AND NOT EXISTS (SELECT 1 FROM diary_vec_meta x "
            "                WHERE x.rowid=d.rowid) "
            "ORDER BY d.rowid DESC LIMIT ?"
        ),
    },
    # Tasks lane covers study + projects (both live in `tasks` filtered by
    # category). Embed active + done so finished work stays surfaceable;
    # skip archived (aging.py auto-applies after 30d of zero mentions).
    "tasks": {
        "vec_table": "tasks_vec",
        "meta_table": "tasks_vec_meta",
        "pending_sql": (
            "SELECT t.id AS id, "
            "  TRIM(COALESCE(t.category,'') || ': ' || COALESCE(t.title,'') || "
            "       CASE WHEN COALESCE(t.next_step,'')!='' "
            "            THEN ' — ' || t.next_step ELSE '' END || "
            "       CASE WHEN COALESCE(t.last_session_summary,'')!='' "
            "            THEN ' (' || t.last_session_summary || ')' ELSE '' END"
            "  ) AS text "
            "FROM tasks t WHERE t.status IN ('active','done') "
            "AND NOT EXISTS (SELECT 1 FROM tasks_vec_meta x "
            "                WHERE x.rowid=t.id) "
            "ORDER BY t.id DESC LIMIT ?"
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


def embed_diary(
    conn: sqlite3.Connection,
    date: str,
    text: str,
    embedder_id: str = "bge-m3",
    dim: int = 1024,
) -> bool:
    """Embed one diary entry into diary_vec + diary_vec_meta. Idempotent.

    Resolves the diary row's rowid by date first — diary's PK is TEXT, vec0
    needs INTEGER. Returns False if the date is not in the diary table.
    """
    row = conn.execute(
        "SELECT rowid FROM diary WHERE date=?", (date,)
    ).fetchone()
    if row is None:
        return False
    return _embed_one(conn, "diary", int(row["rowid"]), text, embedder_id, dim)


def embed_task(
    conn: sqlite3.Connection,
    task_id: int,
    text: str,
    embedder_id: str = "bge-m3",
    dim: int = 1024,
) -> bool:
    """Embed one task row into tasks_vec + tasks_vec_meta. Idempotent."""
    return _embed_one(conn, "tasks", task_id, text, embedder_id, dim)


def _sweep_diary_orphans(conn: sqlite3.Connection) -> None:
    """Drop diary_vec / diary_vec_meta rows whose rowid no longer maps to a
    diary row. daily.run_day rewrites diary by DELETE+INSERT, which reassigns
    rowid and orphans the previous vec embedding. Cheap full-scan join — diary
    is small (≤ ~365 rows/yr).
    """
    try:
        stale = [r[0] for r in conn.execute(
            "SELECT m.rowid FROM diary_vec_meta m "
            "WHERE NOT EXISTS (SELECT 1 FROM diary d WHERE d.rowid=m.rowid)"
        ).fetchall()]
    except sqlite3.Error:
        return
    if not stale:
        return
    with conn:
        for rid in stale:
            conn.execute("DELETE FROM diary_vec WHERE rowid=?", (rid,))
            conn.execute("DELETE FROM diary_vec_meta WHERE rowid=?", (rid,))


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
    if lane == "diary":
        _sweep_diary_orphans(conn)
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
    """Backfill all six lanes (events + memes + entities + milestones + diary
    + tasks).

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
    try:
        ts = datetime.datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        days = max(0.0, (now - ts).total_seconds() / 86400.0)
    except Exception:
        days = 0.0
    return math.exp(-days / 30.0)



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


def _vec_score_map(
    conn: sqlite3.Connection, sql: str, qblob: bytes, k: int
) -> dict[int, float]:
    """Execute sql(qblob, k), return {id: 1-distance} score map."""
    try:
        rows = conn.execute(sql, (qblob, k)).fetchall()
    except sqlite3.Error:
        return {}
    return {r["id"]: max(0.0, 1.0 - r["distance"]) for r in rows}


def _vec_cards(
    conn: sqlite3.Connection, sql: str, qblob: bytes, k: int,
    defaults: dict | None = None,
) -> list[dict]:
    """Execute sql(qblob, k), return list of dicts with vec_score added."""
    try:
        rows = conn.execute(sql, (qblob, k)).fetchall()
    except sqlite3.Error:
        return []
    defs = defaults or {}
    out: list[dict] = []
    for r in rows:
        vs = max(0.0, 1.0 - r["distance"])
        card = {col: (r[col] if r[col] is not None else defs.get(col, "")) for col in r.keys() if col != "distance"}
        card["vec_score"] = vs
        out.append(card)
    return out


def _memes_vec_hits(conn: sqlite3.Connection, qblob: bytes, k: int) -> dict[int, float]:
    return _vec_score_map(
        conn,
        "SELECT m.id AS id, v.distance AS distance "
        "FROM memes_vec v JOIN memes m ON m.id = v.rowid "
        "WHERE m.status='active' AND embedding MATCH ? AND k = ? "
        "ORDER BY v.distance",
        qblob, k,
    )


def _milestones_vec_hits(conn: sqlite3.Connection, qblob: bytes, k: int) -> dict[int, float]:
    return _vec_score_map(
        conn,
        "SELECT mi.id AS id, v.distance AS distance "
        "FROM milestones_vec v JOIN milestones mi ON mi.id = v.rowid "
        "WHERE embedding MATCH ? AND k = ? "
        "ORDER BY v.distance",
        qblob, k,
    )


def _diary_vec_hits(conn: sqlite3.Connection, qblob: bytes, k: int) -> list[dict]:
    return _vec_cards(
        conn,
        "SELECT d.rowid AS id, d.date, d.content, v.distance "
        "FROM diary_vec v JOIN diary d ON d.rowid = v.rowid "
        "WHERE embedding MATCH ? AND k = ? "
        "ORDER BY v.distance",
        qblob, k,
        {"date": "", "content": ""},
    )


def _tasks_vec_hits(conn: sqlite3.Connection, qblob: bytes, k: int) -> list[dict]:
    return _vec_cards(
        conn,
        "SELECT t.id, t.category, t.title, t.next_step, t.status, "
        "       t.created_at, v.distance "
        "FROM tasks_vec v JOIN tasks t ON t.id = v.rowid "
        "WHERE t.status IN ('active','done') "
        "  AND embedding MATCH ? AND k = ? "
        "ORDER BY v.distance",
        qblob, k,
        {"category": "", "title": "", "next_step": "", "status": "", "created_at": ""},
    )


def _entities_vec_hits(conn: sqlite3.Connection, qblob: bytes, k: int) -> list[dict]:
    return _vec_cards(
        conn,
        "SELECT e.id, e.kind, e.name, e.fact, e.mention_count, "
        "       e.created_at, v.distance "
        "FROM entities_vec v JOIN entities e ON e.id = v.rowid "
        "WHERE e.superseded_by IS NULL "
        "  AND embedding MATCH ? AND k = ? "
        "ORDER BY v.distance",
        qblob, k,
        {"kind": "", "name": "", "fact": "", "mention_count": 0, "created_at": ""},
    )


# ── milestone keyword scan ────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")
# Milestone pinned-row boost added to raw fusion score before min_score gate.
_MILESTONE_PINNED_BOOST = 0.10
# Anchor bias: small additive lift for milestone + memes rows so identity /
# stake / lore anchors stay ahead of similarly-scored events on borderline
# queries. Conservative first pass (2026-05-25 Lumi); raise / drop after
# observing prod recall. Entity force-include cards already carry +0.5 and
# do not need this lift.
_ANCHOR_BIAS = 0.10


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
    """Two-way keyword scan over active memes rows.

    Forward (kw=1.0): key.lower() is substring of query.lower() — short
    key inside long query (e.g. (Plan) in (我的 plan 是什么)).
    Reverse (kw=hits/tokens): any query token appears in key/value/context —
    long key with short query (e.g. query (大龙虾) hits key
    (Openclaw / 大龙虾)).
    Shape parallels milestone candidates; kind="memes".
    """
    q_lower = query.lower().strip()
    if not q_lower:
        return []
    tokens = _query_tokens(query)
    rows = conn.execute(
        "SELECT id, type, key, value, context, pinned, use_count "
        "FROM memes WHERE status='active'"
    ).fetchall()
    if not rows:
        return []
    out: list[dict] = []
    for r in rows:
        key = r["key"] or ""
        value = r["value"] or ""
        ctx = r["context"] or ""
        key_l = key.lower()
        hay = (key + " " + value + " " + ctx).lower()
        forward = bool(key_l) and key_l in q_lower
        token_hits = sum(1 for t in tokens if t in hay) if tokens else 0
        if not forward and not token_hits:
            continue
        kw_score = 1.0 if forward else (token_hits / max(1, len(tokens)))
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
            "bm25": kw_score,
            "vec": 0.0,
            "fts_hit": True,
            "pinned": int(r["pinned"] or 0),
            "type": r["type"],
            "use_count": int(r["use_count"] or 0),
        })
    out.sort(
        key=lambda c: (c["pinned"], c["bm25"], c["use_count"]),
        reverse=True,
    )
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
    w_diary_vec: float = 0.55,
    w_tasks_vec: float = 0.55,
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
    diary_vec_cards: list[dict] = []
    tasks_vec_cards: list[dict] = []
    if vec_available:
        k_lane = max(limit * 2, 5)
        memes_vec_map = _memes_vec_hits(conn, qblob, k_lane)
        milestones_vec_map = _milestones_vec_hits(conn, qblob, k_lane)
        entities_vec_cards = _entities_vec_hits(conn, qblob, k_lane)
        diary_vec_cards = _diary_vec_hits(conn, qblob, k_lane)
        tasks_vec_cards = _tasks_vec_hits(conn, qblob, k_lane)

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

    # Diary: vec-only lane (no kw scan for long-form prose). Build candidates
    # gated by _VEC_ONLY_FLOOR; scored with w_diary_vec, no bm25/recency.
    diary_cands: list[dict] = []
    for card in diary_vec_cards:
        vs = card["vec_score"]
        if vs < _VEC_ONLY_FLOOR:
            continue
        if not card["content"] or card["content"] == "—":
            continue
        date = card["date"]
        ts = date if "T" in date else (date + "T00:00:00Z" if date else "")
        diary_cands.append({
            "kind": "diary", "id": card["id"],
            "session_id": None, "timestamp": ts,
            "role": "diary", "content": card["content"],
            "channel": None, "compressed": 0,
            "bm25": 0.0, "vec": vs, "fts_hit": False,
            "date": date,
        })

    # Tasks: vec-only lane (no kw scan for now — titles are short, can land
    # later if needed). Evergreen — no recency on the recall ranking either.
    tasks_cands: list[dict] = []
    for card in tasks_vec_cards:
        vs = card["vec_score"]
        if vs < _VEC_ONLY_FLOOR:
            continue
        title = card["title"]
        if not title:
            continue
        next_step = card["next_step"]
        category = card["category"]
        body = f"{category}: {title}" if category else title
        if next_step:
            body = f"{body} — {next_step}"
        tasks_cands.append({
            "kind": "task", "id": card["id"],
            "session_id": None, "timestamp": card["created_at"],
            "role": "task", "content": body,
            "channel": None, "compressed": 0,
            "bm25": 0.0, "vec": vs, "fts_hit": False,
            "category": category, "status": card["status"],
        })

    # No early-return: even when fusion lanes are empty, entity force-include
    # (substring/LIKE) may still surface an entity card alone — gated below.

    # ── dormant revive + scoring ──────────────────────────────────────────────
    now = datetime.datetime.now(datetime.timezone.utc)

    scored: list[tuple[float, dict]] = []
    for eid, c in candidates.items():
        ts = c["timestamp"]
        try:
            t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age_days = max(0.0, (now - t).total_seconds() / 86400.0)
        except Exception:
            age_days = 0.0

        # Single affect_live fetch covers importance, entities, source.
        af_row = conn.execute(
            "SELECT importance, entities, source FROM affect_live WHERE event_id=?",
            (eid,),
        ).fetchone()
        importance = af_row["importance"] if af_row else None
        af_entities_raw = af_row["entities"] if af_row else None
        source = af_row["source"] if af_row else None

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
        imp = importance or 0
        affect_b = min(0.10, imp / 100.0)

        raw = (
            w_vec * c["vec"]
            + w_bm25 * c["bm25"]
            + w_recency * recency
            + w_affect * affect_b
        )

        # mention_count booster via affect.entities JSON column.
        if af_entities_raw:
            try:
                ent_list = json.loads(af_entities_raw)
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

        floor = _decay_floor(importance, source, age_days)
        final = max(raw, floor) if floor > 0 else raw

        if final >= min_score:
            scored.append((final, {**c, "score": final}))

    # ── milestone scoring (recency/affect dropped — evergreen anchor —
    # bm25 + vec drive rank; no min_score gate so long queries don't dilute
    # the match into oblivion). Anchor bias adds a small static lift so
    # identity / lore stays ahead of similarly-scored events.
    for mc in milestone_cands:
        raw = (
            w_bm25 * mc["bm25"]
            + w_milestones_vec * mc.get("vec", 0.0)
            + _ANCHOR_BIAS
        )
        if mc["pinned"]:
            raw += _MILESTONE_PINNED_BOOST
        scored.append((raw, {**mc, "score": raw}))

    # ── memes scoring (mirror milestone: bm25 + vec + anchor bias + pinned) ─
    for vc in memes_cands:
        raw = (
            w_bm25 * vc["bm25"]
            + w_memes_vec * vc.get("vec", 0.0)
            + _ANCHOR_BIAS
        )
        if vc["pinned"]:
            raw += _MILESTONE_PINNED_BOOST
        scored.append((raw, {**vc, "score": raw}))

    # ── diary scoring (vec only — evergreen long-form prose) ─────────────────
    for dc in diary_cands:
        raw = w_diary_vec * dc.get("vec", 0.0)
        scored.append((raw, {**dc, "score": raw}))

    # ── tasks scoring (vec only — evergreen study + project surface) ────────
    for tc in tasks_cands:
        raw = w_tasks_vec * tc.get("vec", 0.0)
        scored.append((raw, {**tc, "score": raw}))

    scored.sort(key=lambda x: x[0], reverse=True)

    # ── entity force-include (prepend before ms_cap reservation) ─────────────
    # Two streams: substring/LIKE via entity_recall, semantic via entities_vec.
    # Dedup by entity id; substring score wins when both fire (it carries the
    # +0.5 card boost already). body_nonempty filter drops rows whose
    # content is None / "" / whitespace-only — surface them in recall would
    # waste prompt tokens and crowd out signal.
    from .entity_recall import entity_force_include
    force_rows = [r for r in entity_force_include(conn, q, limit)
                  if _body_nonempty(r.get("content"))]
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
    # Strong-FTS count: real bm25 hits only. Force-include rows carry
    # bm25=1.0 as a marker, not an FTS rank — exclude so a noisy query
    # whose only signal is entity force-include doesn't starve memes/milestone
    # reservation.
    strong_fts_count = sum(
        1 for _, r in scored
        if r.get("kind") not in ("milestone", "memes", "diary", "task")
        and r.get("bm25", 0.0) >= 0.5
        and not r.get("force_include")
    )
    if strong_fts_count >= 3:
        ms_cap = 1
        memes_cap = 0
        diary_cap = 0
        tasks_cap = 0
    else:
        ms_cap = max(1, (limit + 2) // 3)
        memes_cap = 1 if limit <= 5 else 2
        # Diary = long-form companion surface. Reserve 1 slot when limit > 5
        # so it isn't starved by event/memes/milestone density.
        diary_cap = 1 if limit > 5 else 0
        # Tasks = study + project surface. Same reservation rule as diary.
        tasks_cap = 1 if limit > 5 else 0
    ms_scored = [(s, r) for s, r in scored if r.get("kind") == "milestone"]
    memes_scored = [(s, r) for s, r in scored if r.get("kind") == "memes"]
    diary_scored = [(s, r) for s, r in scored if r.get("kind") == "diary"]
    tasks_scored = [(s, r) for s, r in scored if r.get("kind") == "task"]
    ev_scored = [
        (s, r) for s, r in scored
        if r.get("kind") not in ("milestone", "memes", "diary", "task")
    ]
    ms_picks = ms_scored[:ms_cap]
    memes_picks = memes_scored[:memes_cap]
    diary_picks = diary_scored[:diary_cap]
    tasks_picks = tasks_scored[:tasks_cap]
    reserved = (
        len(ms_picks) + len(memes_picks) + len(diary_picks) + len(tasks_picks)
    )
    ev_picks = ev_scored[: max(0, limit - reserved)]
    picks = sorted(
        ms_picks + memes_picks + diary_picks + tasks_picks + ev_picks,
        key=lambda x: x[0], reverse=True,
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
    _weight_keys = {
        "w_vec", "w_bm25", "w_recency", "w_affect",
        "w_memes_vec", "w_entities_vec", "w_milestones_vec",
        "w_diary_vec", "w_tasks_vec", "min_score",
    }
    return recall_fusion(
        conn, query,
        limit=int(limit if limit is not None else rcfg.get("limit", 15)),
        budget_chars=int(
            budget_chars if budget_chars is not None
            else rcfg.get("budget_chars", 4000)
        ),
        **{k: float(rcfg[k]) for k in _weight_keys if k in rcfg},
    )
