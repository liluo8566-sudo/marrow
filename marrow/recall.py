"""Recall module: bge-m3 embedding + weighted scalar fusion retrieval.

Write path: embed_event(conn, event_id, text) -> events_vec + events_vec_meta.
Read path: recall_fusion(conn, query, ...) -> scored, decayed, deduped results.
Embedder: BAAI/bge-m3 via onnxruntime (no torch). Lazy-loaded singleton.

DECISIONS Phase 2: B3, B7, decay tier rules.
Fusion weights init: vec=0.55, bm25=0.30, recency=0.15, affect=0.10.
"""
from __future__ import annotations

import math
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

def embed_event(
    conn: sqlite3.Connection,
    event_id: int,
    text: str,
    embedder_id: str = "bge-m3",
    dim: int = 1024,
) -> bool:
    """Embed one event and write events_vec + events_vec_meta.

    Idempotent: skips if rowid already in events_vec_meta.
    Returns True if written, False if skipped or embedder unavailable.
    """
    emb = _ensure_embedder()
    if emb is None:
        return False
    exists = conn.execute(
        "SELECT 1 FROM events_vec_meta WHERE rowid=?", (event_id,)
    ).fetchone()
    if exists:
        return False
    vec = emb.embed([text])[0]
    blob = _vec_to_blob(vec)
    with conn:
        conn.execute(
            "INSERT INTO events_vec(rowid, embedding) VALUES(?, ?)",
            (event_id, blob),
        )
        conn.execute(
            "INSERT INTO events_vec_meta(rowid, embedder_id, dim) VALUES(?, ?, ?)",
            (event_id, embedder_id, dim),
        )
    return True


def embed_pending(
    conn: sqlite3.Connection,
    batch: int = 50,
    embedder_id: str = "bge-m3",
    dim: int = 1024,
) -> int:
    """Embed events not yet in events_vec. Returns count written."""
    emb = _ensure_embedder()
    if emb is None:
        return 0
    rows = conn.execute(
        "SELECT e.id, e.content FROM events e "
        "WHERE NOT EXISTS (SELECT 1 FROM events_vec_meta m WHERE m.rowid=e.id) "
        "ORDER BY e.id DESC LIMIT ?",
        (batch,),
    ).fetchall()
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    texts = [r["content"] or "" for r in rows]
    vecs = emb.embed(texts)
    written = 0
    with conn:
        for eid, vec in zip(ids, vecs):
            conn.execute(
                "INSERT OR IGNORE INTO events_vec(rowid, embedding) VALUES(?, ?)",
                (eid, _vec_to_blob(vec)),
            )
            conn.execute(
                "INSERT OR IGNORE INTO events_vec_meta(rowid, embedder_id, dim) "
                "VALUES(?, ?, ?)",
                (eid, embedder_id, dim),
            )
            written += 1
    return written


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

    if not candidates:
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

    scored.sort(key=lambda x: x[0], reverse=True)

    # ── budget truncation ─────────────────────────────────────────────────────
    out: list[dict] = []
    used = 0
    for _, row in scored[:limit]:
        content = row["content"] or ""
        if used + len(content) > budget_chars:
            content = content[: max(0, budget_chars - used)]
        row = {**row, "content": content}
        out.append(row)
        used += len(content)
        if used >= budget_chars:
            break

    return out
