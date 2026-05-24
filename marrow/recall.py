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

    Returns rows already shaped for fusion: timestamp (date as ISO),
    content (title[: description]), bm25 (kw_score), pinned.
    """
    tokens = _query_tokens(query)
    if not tokens:
        return []
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
        hits = sum(1 for t in tokens if t in hay)
        if not hits:
            continue
        kw_score = hits / len(tokens)
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

    # ── milestone candidates (small table, LIKE scan, no vec/recency) ────────
    milestone_cands = _milestone_candidates(conn, q, limit)

    if not candidates and not milestone_cands:
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

    # ── milestone scoring (no vec/recency/affect; evergreen anchor —
    # any token hit enters, kw_score+pinned only decide rank, no min_score
    # gate so long queries don't dilute the match into oblivion). ─────────────
    for mc in milestone_cands:
        raw = w_bm25 * mc["bm25"]
        if mc["pinned"]:
            raw += _MILESTONE_PINNED_BOOST
        scored.append((raw, {**mc, "score": raw}))

    scored.sort(key=lambda x: x[0], reverse=True)

    # ── entity force-include (prepend before ms_cap reservation) ─────────────
    from .entity_recall import entity_force_include
    force_rows = entity_force_include(conn, q, limit)
    force_ids = {r["id"] for r in force_rows}
    # Remove any fusion duplicates that force-include already covers.
    scored = [(s, r) for s, r in scored if r.get("id") not in force_ids]
    # Prepend force rows (score already set, kind already "event").
    force_pairs = [(r["score"], r) for r in force_rows]
    scored = force_pairs + scored
    scored.sort(key=lambda x: x[0], reverse=True)

    # ── reserved milestone slots ──────────────────────────────────────────────
    # Events can outrank milestones on score (recency + affect + fts_hit),
    # so a pure top-K cut starves milestones on long/noisy queries. Reserve
    # up to ceil(limit/3) slots for best-matched milestones; remainder goes
    # to events. Final order re-sorted by score.
    # Adaptive ms_cap: when >=3 strong FTS hits exist, drop to 1 milestone
    # slot so entity-dense queries don't waste budget on milestones.
    strong_fts_count = sum(
        1 for _, r in scored
        if r.get("kind") != "milestone" and r.get("bm25", 0.0) >= 0.5
    )
    if strong_fts_count >= 3:
        ms_cap = 1
    else:
        ms_cap = max(1, (limit + 2) // 3)
    ms_scored = [(s, r) for s, r in scored if r.get("kind") == "milestone"]
    ev_scored = [(s, r) for s, r in scored if r.get("kind") != "milestone"]
    # Force-include rows are already in ev_scored (kind="event"); they skip
    # ms_cap reservation naturally since they are events.
    ms_picks = ms_scored[:ms_cap]
    ev_picks = ev_scored[: max(0, limit - len(ms_picks))]
    picks = sorted(ms_picks + ev_picks, key=lambda x: x[0], reverse=True)

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
        min_score=float(rcfg.get("min_score", 0.35)),
    )
