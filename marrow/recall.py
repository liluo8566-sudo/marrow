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
    "stickers": {
        "vec_table": "stickers_vec",
        "meta_table": "stickers_vec_meta",
        "pending_sql": (
            "SELECT s.id AS id, COALESCE(s.desc, '') AS text "
            "FROM stickers s "
            "WHERE COALESCE(s.desc, '') NOT IN ('', '(pending)') "
            "AND NOT EXISTS (SELECT 1 FROM stickers_vec_meta x "
            "                WHERE x.rowid=s.id) "
            "ORDER BY s.id DESC LIMIT ?"
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


def embed_sticker(
    conn: sqlite3.Connection,
    sticker_id: int,
    text: str,
    embedder_id: str = "bge-m3",
    dim: int = 1024,
) -> bool:
    """Embed one sticker row into stickers_vec + stickers_vec_meta. Idempotent."""
    return _embed_one(conn, "stickers", sticker_id, text, embedder_id, dim)


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
    _media_tag = re.compile(r'\s*<(?:image|file)\s+path="[^"]*?"[^>]*>\s*')
    texts = [_media_tag.sub(" ", r["text"] or "").strip() for r in rows]
    vecs = emb.embed(texts)
    written = 0
    vt = cfg["vec_table"]
    mt = cfg["meta_table"]
    with conn:
        for rid, vec in zip(ids, vecs):
            try:
                conn.execute(
                    f"INSERT INTO {vt}(rowid, embedding) VALUES(?, ?)",
                    (rid, _vec_to_blob(vec)),
                )
            except sqlite3.IntegrityError:
                pass
            except sqlite3.OperationalError as e:
                if "UNIQUE constraint" not in str(e):
                    raise
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
    """Backfill all seven lanes (events + memes + entities + milestones + diary
    + tasks + stickers).

    Per-lane budget = `batch` so a large events backlog cannot starve the
    cross-table lanes on a single hook firing. Returns total rows written.
    """
    if _ensure_embedder() is None:
        return 0
    total = 0
    for lane in _LANES:
        total += _embed_pending_lane(conn, lane, batch, embedder_id, dim)
    return total


# ── recall-count bump ────────────────────────────────────────────────────────

def bump_recall_counts(event_ids: list[int], db: str | None = None) -> None:
    """Best-effort: increment recall_count + set last_recalled_at for event rows.

    Called after recall hits are confirmed injected (passive hook) or returned
    (MCP daemon). Failure MUST NEVER propagate — wrapped in try/except throughout.
    Uses a separate short-lived connection to avoid disturbing the caller's txn.
    """
    if not event_ids:
        return
    try:
        from . import storage as _storage, config as _config
        db = db or _config.db_path()
        conn = _storage.connect(db)
        try:
            ts = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            with conn:
                conn.executemany(
                    "UPDATE events SET"
                    " recall_count = recall_count + 1,"
                    " last_recalled_at = ?"
                    " WHERE id = ?",
                    [(ts, eid) for eid in event_ids],
                )
        finally:
            conn.close()
    except Exception:
        pass  # stats write must never block or fail recall


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
    """DECISIONS Phase 2 decay FLOOR tiers (read-time). Scale: 1-5."""
    imp = importance or 0
    if source == "override" or imp == 5:
        return 0.5   # Permanent
    if 3 <= imp <= 4:
        return 0.18
    # imp <= 2 & age > 90d -> dormant (excluded upstream, floor irrelevant)
    return 0.0


def _is_dormant(importance: int | None, age_days: float) -> bool:
    """Demote-sink: excluded from recall candidate pool. Scale: 1-5."""
    imp = importance or 0
    return imp <= 2 and age_days > 90


# ── cross-table vec lane lookups ──────────────────────────────────────────────

# Minimum similarity for a vec-only (no keyword match) row to surface.
# Under (2-d)/2 normalization, orthogonal pairs (dist≈1) score 0.5, so the
# floor must be set above 0.5 to reject "barely-related" noise. Empirical:
# unrelated CN/EN entities cluster around 0.44 sim; weakly-related around
# 0.50-0.55; clearly-related ≥ 0.60. 0.55 is the cleanest gate.
_VEC_ONLY_FLOOR = 0.55


def _vec_score_map(
    conn: sqlite3.Connection, sql: str, qblob: bytes, k: int
) -> dict[int, float]:
    """Execute sql(qblob, k), return {id: similarity} score map.
    sqlite-vec cosine distance ∈ [0, 2]; normalize to similarity ∈ [0, 1]
    as (2 - dist) / 2 so orthogonal pairs (dist=1) score 0.5, not 0."""
    try:
        rows = conn.execute(sql, (qblob, k)).fetchall()
    except sqlite3.Error:
        return {}
    return {r["id"]: max(0.0, (2.0 - r["distance"]) / 2.0) for r in rows}


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
        vs = max(0.0, (2.0 - r["distance"]) / 2.0)
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


# ── FTS5 query term extraction ───────────────────────────────────────────────
# All anchor tables (memes / milestones / entities) now go through FTS5 +
# vec, matching the full row body. No more `_anchor_triggers` reverse-substring
# path — ASCII short triggers (OT / in / the / ed) used to hit "others" /
# "caching" / "cached" / "tier" via raw substring. FTS5 trigram tokenizer +
# minimum-length term gate kills that noise.
#
# Term rules: ASCII alnum runs and CJK runs captured whole; then
# `_fts_terms` keeps ASCII ≥3 chars as-is and splits CJK runs into sliding
# 3-char windows that align with the trigram tokenizer. Shorter fragments
# are dropped — trigram MATCH silently returns 0 hits on <3-char queries.
_FTS_TERM_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]+")
# Vec pre-gate for anchor lanes (milestone / memes / entity): rows whose vec
# similarity is below this floor are dropped BEFORE scoring — they cannot ride
# bias up past min_score with no real topical match.
_ANCHOR_VEC_FLOOR = 0.50  # was _VEC_ONLY_FLOOR (0.55); dropped for zh↔en paraphrase tolerance
# Anchor scoring bias: events get recency+affect (≈+0.25 ceiling) on top of
# vec+bm25; anchor lanes only have vec+bm25. This +0.10 bias rebalances so a
# vec-floor-cleared (or strong-hit) anchor can compete with high-recency events.
# Only applies to rows that already passed the vec floor or were strong-hit —
# unrelated anchors are still filtered out before this bias is added.
_ANCHOR_BIAS = 0.10

# Tokenizer for stopword filtering: ASCII runs and CJK runs (same as FTS_TERM_RE).
_SW_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]+")


def _apply_stopwords(q: str, stopwords: list[str]) -> str:
    """Remove stopword tokens from query q.

    For ASCII tokens: drop if token.lower() in stopwords_set.
    For CJK runs: drop the whole run if its lowercase form is a stopword;
    also slide a window over the run to drop any embedded stopword substrings.
    Returns space-joined remaining tokens. Empty result returns "".
    """
    if not stopwords or not q:
        return q
    sw_set = {s.lower() for s in stopwords if s}
    tokens = _SW_TOKEN_RE.findall(q)
    kept: list[str] = []
    for tok in tokens:
        lower = tok.lower()
        # Is the whole token a stopword?
        if lower in sw_set:
            continue
        # For CJK runs: slide window and strip any embedded stopword.
        if re.match(r"^[一-鿿]+$", tok):
            filtered = tok
            for sw in sw_set:
                if re.match(r"^[一-鿿]+$", sw):
                    filtered = filtered.replace(sw, "")
            if not filtered:
                continue
            kept.append(filtered)
        else:
            kept.append(tok)
    return " ".join(kept)

# cwd → recall bucket mapping. Same-bucket events get +same_boost, known
# cross-bucket events take -diff_penalty (soft cut, still possible to win
# on strong raw score). Anchors (milestones / memes / diary / tasks / entity
# force-include) are evergreen and skip the bucket bias entirely.
#
# Defaults below are the FALLBACK shape used when no [recall.buckets] config
# section is present (e.g. test fixtures, fork without config). Live config
# wins via _load_bucket_rules() — see config.default.toml [recall.buckets].
_DEFAULT_BUCKETS: tuple[tuple[str, str], ...] = (
    ("/cc-lab", "project"),
    ("/desktop/ny", "daily"),
    ("/study", "study"),
)
_DEFAULT_SAME_BOOST = 0.10
_DEFAULT_DIFF_PENALTY = 0.10


def _load_bucket_rules() -> tuple[tuple[tuple[str, str], ...], float, float]:
    """Read [recall.buckets] from live config. Falls back to _DEFAULT_* on
    missing section / parse error. Returns (needle->bucket tuples, same_boost,
    diff_penalty). Empty needle lists mean the bucket is disabled.
    """
    try:
        from . import config as _config
        bcfg = _config.load().get("recall", {}).get("buckets", {})
    except Exception:
        bcfg = {}
    if not bcfg:
        return _DEFAULT_BUCKETS, _DEFAULT_SAME_BOOST, _DEFAULT_DIFF_PENALTY
    pairs: list[tuple[str, str]] = []
    for bucket in ("project", "daily", "study"):
        for needle in (bcfg.get(bucket) or []):
            if isinstance(needle, str) and needle:
                pairs.append((needle.lower(), bucket))
    same = float(bcfg.get("same_boost", _DEFAULT_SAME_BOOST))
    diff = float(bcfg.get("diff_penalty", _DEFAULT_DIFF_PENALTY))
    if not pairs:
        return _DEFAULT_BUCKETS, same, diff
    return tuple(pairs), same, diff


def _cwd_bucket(cwd: str | None, rules: tuple[tuple[str, str], ...] | None = None) -> str:
    """Classify a cwd path into a recall bucket.

    Empty / None / unmatched cwd → "" (neutral; no boost, no penalty).
    Matching is substring against the lowercased path so worktrees under
    `<repo>/.claude/worktrees/...` still classify into the parent bucket.
    Pass an explicit `rules` tuple to avoid re-reading config in hot loops.
    """
    if not cwd:
        return ""
    p = cwd.lower()
    pairs = rules if rules is not None else _DEFAULT_BUCKETS
    for needle, bucket in pairs:
        if needle in p:
            return bucket
    return ""


def _fts_terms(q: str) -> list[str]:
    """Extract FTS5 trigram-safe query terms.

    - ASCII alnum runs ≥3 chars kept whole (one trigram MATCH covers it).
    - CJK runs split into sliding 3-char windows aligned with the trigram
      tokenizer — a long CJK prompt like (老公你知道鸭子梗么) yields windows
      that include (鸭子梗), letting the OR query hit a row whose body
      contains (鸭子梗) without requiring the full prompt to appear verbatim.
    - Anything <3 chars dropped (trigram MATCH returns 0 on short queries —
      this is the "OT" pathology gate).

    Lowercased, dedup preserves first occurrence order.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _FTS_TERM_RE.finditer(q):
        t = m.group(0).lower()
        if len(t) < 3:
            continue
        if t.isascii():
            if t not in seen:
                seen.add(t)
                out.append(t)
        else:
            for i in range(len(t) - 2):
                win = t[i:i + 3]
                if win not in seen:
                    seen.add(win)
                    out.append(win)
    return out


def _fts_query(terms: list[str]) -> str:
    """Build FTS5 MATCH expression: each term as quoted phrase, OR-joined.

    Empty → empty string (caller must short-circuit). FTS5 quoting: embedded
    double-quote escapes to "".
    """
    if not terms:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


# ── anchor strong-hit (substring scan, bypass vec floor) ──────────────────────
# Direction: anchor row body → query. For each anchor row, expand a needle set
# from its full body (entity name+fact+aliases / memes key+value /
# milestone title+description). If any needle appears as a substring of the
# lowercased query, the row is a strong-hit and gets:
#   - vec floor bypass (vec_val < _ANCHOR_VEC_FLOOR no longer drops it)
#   - strong-hit only bypasses vec floor; natural bm25+vec score determines rank — no forced score floor.
# Catches dims that trigram FTS5 misses (2-char CJK names like 妈妈) or that
# vec ranks low (long query vs short anchor body collapses CLS-pool cosine).
_NEEDLE_SPLIT_RE = re.compile(r'[^\w一-鿿]+')
_CJK_RUN_RE = re.compile(r'[一-鿿]+')
_ASCII_RUN_RE = re.compile(r'[a-z0-9]+')

# Strong-hit tier floors (post-bias). A query hitting a row's name/key/title
# is the highest-confidence signal we have — pin it well above the noise band.
# Body hits (fact/value/description) rank above the min_score line but don't
# get top billing.
_STRONG_NAME_FLOOR = 0.55
_STRONG_BODY_FLOOR = 0.45
# 2-char CJK body windows present in >= this many rows of the same table are
# generic chatter and dropped; rare ones (减肥) stay as anchors.
_BODY_DF_MAX = 3
# CJK function chars: a 2-char body window containing any of these is a
# generic fragment (你说/可以/现在/我写), never a real anchor. Content-word
# windows (减肥/马自/卡罗) contain none of them. DF can't catch these in
# small tables (a generic bigram may appear in only 1-2 rows), so this is
# the deterministic layer; DF handles table-specific repeats.
_CJK_FUNC_CHARS = frozenset(
    "的了是在我你他她它这那就都不很还又再也么呢吧吗啊哦嗯呀"
    "什怎为之其与及或被把给跟和对可以有没要会能而但然因所于"
)
# Generic content bigrams the char filter can't catch (both chars are
# content chars). Probed 2026-06-12: only a handful actually anchor in the
# live tables; extend one word per line as leaks surface.
_CJK_STOP_BIGRAMS = frozenset((
    "如果", "时候", "问题", "觉得", "感觉", "东西", "事情", "开始",
    "已经", "今天", "明天", "昨天", "现在", "什么", "怎么", "这样",
    "那样", "一下", "一个", "有点", "比较", "真的", "直接", "或者",
    "就是", "可能", "应该", "需要", "知道", "出现", "发现", "继续",
    "希望", "一句", "句话",
))


def _expand_needles(text: str, cjk_min: int = 2, cjk_max: int = 4,
                    ascii_min: int = 2) -> set[str]:
    """Build substring needles from an anchor body.

    Tokenize by non-word/non-CJK chars; then:
    - Pure ASCII alnum tokens ≥ ascii_min: keep whole (保住 max/xhs/SSU/5x/bbb)
    - CJK runs inside tokens: sliding cjk_min..cjk_max windows (让 (在一起)
      能从 (我们在...正式在一起了) 里挖出)
    Returns lowercased dedup set.
    """
    if not text:
        return set()
    out: set[str] = set()
    for tok in _NEEDLE_SPLIT_RE.split(text.lower()):
        if not tok:
            continue
        if tok.isascii() and len(tok) >= ascii_min:
            out.add(tok)
        elif not tok.isascii():
            # mixed CJK/ascii token ((马自达suv)): pull ascii runs too
            for arun in _ASCII_RUN_RE.findall(tok):
                if len(arun) >= ascii_min:
                    out.add(arun)
        for run in _CJK_RUN_RE.findall(tok):
            n_max = min(cjk_max, len(run))
            for n in range(cjk_min, n_max + 1):
                for i in range(len(run) - n + 1):
                    out.add(run[i:i + n])
    return out


def _filter_generic_cjk(needles: set[str]) -> set[str]:
    """Drop generic CJK needles from a name/key/title needle set.

    Rules:
    - Non-CJK (pure ASCII or len-1): pass through unchanged.
    - len 2: drop if in _CJK_STOP_BIGRAMS OR contains any char in _CJK_FUNC_CHARS.
    - len 3-4: drop if ≥2 chars from _CJK_FUNC_CHARS OR any 2-char substring is
      in _CJK_STOP_BIGRAMS.
    - len >4 or len 1: keep.
    """
    out: set[str] = set()
    for n in needles:
        if n.isascii() or len(n) == 1:
            out.add(n)
            continue
        if len(n) == 2:
            if n in _CJK_STOP_BIGRAMS:
                continue
            if any(ch in _CJK_FUNC_CHARS for ch in n):
                continue
            out.add(n)
        elif len(n) in (3, 4):
            func_count = sum(1 for ch in n if ch in _CJK_FUNC_CHARS)
            if func_count >= 2:
                continue
            bigrams = {n[i:i + 2] for i in range(len(n) - 1)}
            if bigrams & _CJK_STOP_BIGRAMS:
                continue
            out.add(n)
        else:
            out.add(n)
    return out


def _needles_match(needles: set[str], query_lower: str) -> bool:
    """True if any needle hits the query.

    CJK needles: plain substring. ASCII needles: letter-boundary match —
    (nd) must not hit inside (handover), but (gpt) still hits (gpt4画画)
    since digits don't count as a boundary breaker.
    """
    for n in needles:
        if n.isascii():
            if re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", query_lower):
                return True
        elif n in query_lower:
            return True
    return False


def _body_needles(bodies: list[str]) -> list[set[str]]:
    """Per-row body needles with table-level DF filtering on 2-char CJK.

    Frequency replaces length as the noise filter: a 2-char window appearing
    in >= _BODY_DF_MAX rows is generic chatter and dropped, while rare ones
    (减肥 in one meme) survive as real anchors. ASCII needles are never DF'd —
    legit short tags repeat across rows (ED in 2 memes) and they already have
    letter-boundary protection.
    """
    per_row = [_expand_needles(b) for b in bodies]
    df: dict[str, int] = {}
    for s in per_row:
        for n in s:
            if len(n) == 2 and not n.isascii():
                df[n] = df.get(n, 0) + 1

    def _keep(n: str) -> bool:
        if n.isascii():
            return True
        if len(n) == 2:
            if n in _CJK_STOP_BIGRAMS:
                return False
            if any(ch in _CJK_FUNC_CHARS for ch in n):
                return False
            return df.get(n, 0) < _BODY_DF_MAX
        if len(n) in (3, 4):
            func_count = sum(1 for ch in n if ch in _CJK_FUNC_CHARS)
            if func_count >= 2:
                return False
            bigrams = {n[i:i + 2] for i in range(len(n) - 1)}
            if bigrams & _CJK_STOP_BIGRAMS:
                return False
            return True
        return True

    return [{n for n in s if _keep(n)} for s in per_row]


def _entity_strong_hits(
    conn: sqlite3.Connection, query_lower: str
) -> list[tuple[sqlite3.Row, str]]:
    """Scan all live entities; return (row, tier) for needle matches.

    Tier (name): name/aliases needles — full 2-4 CJK windows, the feature's
    point (short CN names below the trigram floor). Tier (body): fact needles,
    DF-filtered via _body_needles.
    """
    rows = conn.execute(
        "SELECT id, kind, name, fact, aliases, mention_count, created_at "
        "FROM entities WHERE superseded_by IS NULL"
    ).fetchall()
    name_sets: list[set[str]] = []
    for r in rows:
        name_parts = [r["name"] or ""]
        aliases_raw = r["aliases"]
        if aliases_raw and aliases_raw not in ("", "[]"):
            try:
                al = json.loads(aliases_raw)
                if isinstance(al, list):
                    name_parts.extend(str(a) for a in al if a)
            except Exception:
                pass
        name_sets.append(_filter_generic_cjk(_expand_needles(" ".join(name_parts))))
    body_sets = _body_needles([r["fact"] or "" for r in rows])
    hits: list[tuple[sqlite3.Row, str]] = []
    for r, ns, bs in zip(rows, name_sets, body_sets):
        if _needles_match(ns, query_lower):
            hits.append((r, "name"))
        elif _needles_match(bs, query_lower):
            hits.append((r, "body"))
    return hits


def _memes_strong_hits(
    conn: sqlite3.Connection, query_lower: str
) -> list[tuple[sqlite3.Row, str]]:
    """Scan active memes; key = name tier, value = DF-filtered body tier."""
    rows = conn.execute(
        "SELECT id, type, key, value, context, pinned, use_count "
        "FROM memes WHERE status='active'"
    ).fetchall()
    body_sets = _body_needles([r["value"] or "" for r in rows])
    hits: list[tuple[sqlite3.Row, str]] = []
    for r, bs in zip(rows, body_sets):
        if _needles_match(_filter_generic_cjk(_expand_needles(r["key"] or "")), query_lower):
            hits.append((r, "name"))
        elif _needles_match(bs, query_lower):
            hits.append((r, "body"))
    return hits


def _milestone_strong_hits(
    conn: sqlite3.Connection, query_lower: str
) -> list[tuple[sqlite3.Row, str]]:
    """Scan all milestones; title = name tier, description = body tier."""
    rows = conn.execute(
        "SELECT id, scope, date, title, description, pinned FROM milestones"
    ).fetchall()
    body_sets = _body_needles([r["description"] or "" for r in rows])
    hits: list[tuple[sqlite3.Row, str]] = []
    for r, bs in zip(rows, body_sets):
        if _needles_match(_filter_generic_cjk(_expand_needles(r["title"] or "")), query_lower):
            hits.append((r, "name"))
        elif _needles_match(bs, query_lower):
            hits.append((r, "body"))
    return hits


def _fts_lane_hits(
    conn: sqlite3.Connection, sql: str, fts_q: str, k: int
) -> list[sqlite3.Row]:
    """Execute an FTS lane SQL with (fts_q, k). Returns rows or [] on error."""
    if not fts_q:
        return []
    try:
        return conn.execute(sql, (fts_q, k)).fetchall()
    except sqlite3.Error:
        return []


def _bm25_normalize(ranks: list[float]) -> list[float]:
    """Map FTS5 rank list to [0,1]: best (smallest abs) -> 1.0."""
    abs_ranks = [abs(r) for r in ranks]
    if not abs_ranks:
        return []
    min_r = min(abs_ranks)
    return [(min_r / r) if r else 1.0 for r in abs_ranks]


def _milestone_candidates(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[dict]:
    """FTS5 scan over milestones_fts (title+description body). Pure forward
    search — query terms matched against full row content. No reverse-substring.
    """
    terms = _fts_terms(query)
    fts_q = _fts_query(terms)
    rows = _fts_lane_hits(
        conn,
        "SELECT mi.id, mi.scope, mi.date, mi.title, mi.description, mi.pinned, "
        "       rank AS fts_rank "
        "FROM milestones_fts f JOIN milestones mi ON mi.id = f.rowid "
        "WHERE milestones_fts MATCH ? ORDER BY rank LIMIT ?",
        fts_q, limit * 3,
    )
    if not rows:
        return []
    bm25_scores = _bm25_normalize([r["fts_rank"] for r in rows])
    out: list[dict] = []
    for i, r in enumerate(rows):
        title = r["title"] or ""
        desc = r["description"] or ""
        date = r["date"] or ""
        ts = date if "T" in date else (date + "T00:00:00Z" if date else "")
        content = title if not desc else f"{title}: {desc}"
        out.append({
            "kind": "milestone", "id": r["id"],
            "session_id": None, "timestamp": ts,
            "role": "milestone", "content": content,
            "channel": None, "compressed": 0,
            "bm25": bm25_scores[i], "vec": 0.0, "fts_hit": True,
            "pinned": int(r["pinned"] or 0),
            "scope": r["scope"],
        })
    return out


def _memes_candidates(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[dict]:
    """FTS5 scan over memes_fts (key+value+context body). Active rows only."""
    terms = _fts_terms(query)
    fts_q = _fts_query(terms)
    rows = _fts_lane_hits(
        conn,
        "SELECT m.id, m.type, m.key, m.value, m.context, m.pinned, m.use_count, "
        "       rank AS fts_rank "
        "FROM memes_fts f JOIN memes m ON m.id = f.rowid "
        "WHERE memes_fts MATCH ? AND m.status='active' "
        "ORDER BY rank LIMIT ?",
        fts_q, limit * 3,
    )
    if not rows:
        return []
    bm25_scores = _bm25_normalize([r["fts_rank"] for r in rows])
    out: list[dict] = []
    for i, r in enumerate(rows):
        key = r["key"] or ""
        value = r["value"] or ""
        ctx = r["context"] or ""
        content = f"{key}: {value}" if value else key
        if ctx:
            content = f"{content} ({ctx})"
        out.append({
            "kind": "memes", "id": r["id"],
            "session_id": None, "timestamp": "",
            "role": "memes", "content": content,
            "channel": None, "compressed": 0,
            "bm25": bm25_scores[i], "vec": 0.0, "fts_hit": True,
            "pinned": int(r["pinned"] or 0),
            "type": r["type"],
            "use_count": int(r["use_count"] or 0),
        })
    return out


def _entity_candidates(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[dict]:
    """FTS5 scan over entities_fts (name+fact+aliases body). Live rows only."""
    terms = _fts_terms(query)
    fts_q = _fts_query(terms)
    rows = _fts_lane_hits(
        conn,
        "SELECT e.id, e.kind, e.name, e.fact, e.mention_count, e.created_at, "
        "       rank AS fts_rank "
        "FROM entities_fts f JOIN entities e ON e.id = f.rowid "
        "WHERE e.superseded_by IS NULL AND entities_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        fts_q, limit * 3,
    )
    if not rows:
        return []
    bm25_scores = _bm25_normalize([r["fts_rank"] for r in rows])
    out: list[dict] = []
    for i, r in enumerate(rows):
        fact = r["fact"] or ""
        if not fact.strip():
            continue
        name = r["name"] or ""
        ekind = r["kind"] or ""
        content = f"{name} ({ekind}): {fact}" if ekind else f"{name}: {fact}"
        out.append({
            "kind": "entity", "id": r["id"],
            "session_id": None, "timestamp": r["created_at"] or "",
            "role": "entity", "content": content,
            "channel": None, "compressed": 0,
            "bm25": bm25_scores[i], "vec": 0.0, "fts_hit": True,
            "mention_count": int(r["mention_count"] or 0),
            "entity_kind": ekind,
        })
    return out


# ── fusion retrieval ──────────────────────────────────────────────────────────

def recall_fusion(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    budget_chars: int | None = None,
    *,
    w_vec: float = 0.55,
    w_bm25: float = 0.30,
    w_recency: float = 0.15,
    w_affect: float = 0.10,
    w_memes_vec: float = 0.60,
    w_entities_vec: float = 0.60,
    w_milestones_vec: float = 0.60,
    w_diary_vec: float = 0.55,
    w_tasks_vec: float = 0.55,
    min_score: float = 0.35,
    current_cwd: str | None = None,
    exclude_kinds: tuple[str, ...] = (),
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Single weighted scalar fusion: vec + bm25 + recency + affect.

    Excludes dormant rows (imp<=2, age>90d, no FTS keyword revive).
    Applies FLOOR tiers to final score.
    FTS keyword hit on dormant row clears dormant flag before scoring.
    Returns rows sorted by score desc, truncated by budget_chars.
    `exclude_kinds`: lane kinds to skip entirely (e.g. ("diary", "task")).
    `since`/`until`: UTC ISO strings; when set, events and diary are filtered
    to this window. Anchor lanes (memes/entities/milestones/tasks) unaffected.
    """
    q = query.strip()
    if not q:
        return []

    # Strip emotion punctuation (?/!/？/！, single + repeated) — no FTS/vec signal.
    q = re.sub(r"[？?！!]+", " ", q).strip()
    if not q:
        return []

    # Strip CC harness markers (command tags, image refs, stdout blocks).
    from .transcript import strip_harness_markers as _strip_harness
    q = _strip_harness(q)
    if not q:
        return []

    # Stopword filter: strip config-driven tokens before FTS + vec.
    # List is empty by default; populated by Lumi after reviewing candidates.
    try:
        from . import config as _cfg
        _sw = _cfg.load().get("recall", {}).get("stopwords", []) or []
    except Exception:
        _sw = []
    if _sw:
        q = _apply_stopwords(q, _sw)
        if not q:
            return []

    emb = _ensure_embedder()
    vec_available = emb is not None

    # cwd bias setup: load rules once, classify the query's bucket. Per-event
    # classification happens in the scoring loop using the same `rules`.
    bucket_rules, same_boost, diff_penalty = _load_bucket_rules()
    cur_bucket = _cwd_bucket(current_cwd, bucket_rules)

    # ── FTS candidates ────────────────────────────────────────────────────────
    fts_q = '"' + q.replace('"', '""') + '"'
    if since and until:
        fts_rows = conn.execute(
            "SELECT e.id, e.session_id, e.timestamp, e.role, e.content, e.channel, "
            "e.compressed, s.cwd AS session_cwd, rank AS fts_rank "
            "FROM events_fts f JOIN events e ON e.id = f.rowid "
            "LEFT JOIN sessions s ON s.sid = e.session_id "
            "WHERE events_fts MATCH ? AND e.timestamp >= ? AND e.timestamp < ? "
            "ORDER BY rank LIMIT ?",
            (fts_q, since, until, limit * 3),
        ).fetchall()
    else:
        fts_rows = conn.execute(
            "SELECT e.id, e.session_id, e.timestamp, e.role, e.content, e.channel, "
            "e.compressed, s.cwd AS session_cwd, rank AS fts_rank "
            "FROM events_fts f JOIN events e ON e.id = f.rowid "
            "LEFT JOIN sessions s ON s.sid = e.session_id "
            "WHERE events_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_q, limit * 3),
        ).fetchall()

    # ── vec candidates ────────────────────────────────────────────────────────
    vec_rows: list[sqlite3.Row] = []
    if vec_available:
        qvec = emb.embed([q])[0]
        qblob = _vec_to_blob(qvec)
        # When a time window is active, fetch a larger candidate set and filter
        # in Python. sqlite-vec KNN (MATCH+k=) cannot reliably apply arbitrary
        # WHERE predicates on the joined table inside the virtual-table scan.
        vec_k = limit * 6 if (since and until) else limit * 3
        all_vec_rows = conn.execute(
            "SELECT e.id, e.session_id, e.timestamp, e.role, e.content, e.channel, "
            "e.compressed, s.cwd AS session_cwd, v.distance "
            "FROM events_vec v JOIN events e ON e.id = v.rowid "
            "LEFT JOIN sessions s ON s.sid = e.session_id "
            "WHERE embedding MATCH ? AND k = ? "
            "ORDER BY v.distance",
            (qblob, vec_k),
        ).fetchall()
        if since and until:
            vec_rows = [r for r in all_vec_rows
                        if r["timestamp"] and r["timestamp"] >= since and r["timestamp"] < until]
        else:
            vec_rows = all_vec_rows

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
            "session_cwd": r["session_cwd"] if "session_cwd" in r.keys() else None,
            "bm25": bm25_score,
            "vec": 0.0, "fts_hit": True,
        }

    # Vec scores: distance is cosine distance ∈ [0, 2]; normalize to [0, 1]
    # as (2 - d) / 2 — orthogonal (dist=1) → 0.5, opposite (dist=2) → 0.
    if vec_available:
        vec_dists = [r["distance"] for r in vec_rows]
        for i, r in enumerate(vec_rows):
            eid = r["id"]
            vec_score = max(0.0, (2.0 - vec_dists[i]) / 2.0)
            if eid in candidates:
                candidates[eid]["vec"] = vec_score
            else:
                candidates[eid] = {
                    "id": eid, "session_id": r["session_id"],
                    "timestamp": r["timestamp"], "role": r["role"],
                    "content": r["content"], "channel": r["channel"],
                    "compressed": r["compressed"],
                    "session_cwd": r["session_cwd"] if "session_cwd" in r.keys() else None,
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

    # Strong-hit merge: substring-scan all active memes; mark strong tier
    # ((name)/(body)) for needle matches. Bypasses vec floor.
    q_lower = q.lower()
    for r, tier in _memes_strong_hits(conn, q_lower):
        mid = r["id"]
        if mid in memes_by_id:
            memes_by_id[mid]["strong"] = tier
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
            "bm25": 0.0, "vec": 0.0, "fts_hit": False,
            "pinned": int(r["pinned"] or 0),
            "type": r["type"],
            "use_count": int(r["use_count"] or 0),
            "strong": tier,
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
    # Strong-hit merge: substring-scan all milestones; mark strong tier
    # ((name)/(body)) for needle matches. Bypasses vec floor.
    for r, tier in _milestone_strong_hits(conn, q_lower):
        mid = r["id"]
        if mid in ms_by_id:
            ms_by_id[mid]["strong"] = tier
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
            "bm25": 0.0, "vec": 0.0, "fts_hit": False,
            "pinned": int(r["pinned"] or 0),
            "scope": r["scope"],
            "strong": tier,
        }
    milestone_cands = list(ms_by_id.values())

    # Diary: vec-only lane (no kw scan for long-form prose). Build candidates
    # gated by _VEC_ONLY_FLOOR; scored with w_diary_vec, no bm25/recency.
    # When a time window is active, collect the Melbourne-local dates it spans
    # and filter diary rows to those dates only.
    _diary_dates: set[str] | None = None
    if since and until:
        from .timeutil import utc_iso_to_local_date as _u2d
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        try:
            _s = _dt.fromisoformat(since.replace("Z", "+00:00"))
            _e = _dt.fromisoformat(until.replace("Z", "+00:00"))
            _diary_dates = set()
            _cur = _s
            while _cur <= _e:
                _diary_dates.add(_u2d(_cur.strftime("%Y-%m-%dT%H:%M:%SZ")))
                _cur += _td(days=1)
        except Exception:
            _diary_dates = None

    diary_cands: list[dict] = []
    if _diary_dates is not None:
        # Date window present — direct SELECT, bypass vec floor.
        _ph = ",".join("?" * len(_diary_dates))
        for row in conn.execute(
            f"SELECT rowid, date, content FROM diary "
            f"WHERE date IN ({_ph}) AND COALESCE(content,'') NOT IN ('','—')",
            list(_diary_dates),
        ).fetchall():
            ts = row["date"] + "T00:00:00Z" if row["date"] else ""
            diary_cands.append({
                "kind": "diary", "id": int(row["rowid"]),
                "session_id": None, "timestamp": ts,
                "role": "diary", "content": row["content"],
                "channel": None, "compressed": 0,
                "bm25": 0.0, "vec": 1.0, "fts_hit": False,
                "date": row["date"],
            })
    else:
        for card in diary_vec_cards:
            vs = card["vec_score"]
            if vs < _VEC_ONLY_FLOOR:
                continue
            if not card["content"] or card["content"] == "—":
                continue
            ts = card["date"] + "T00:00:00Z" if card["date"] else ""
            diary_cands.append({
                "kind": "diary", "id": card["id"],
                "session_id": None, "timestamp": ts,
                "role": "diary", "content": card["content"],
                "channel": None, "compressed": 0,
                "bm25": 0.0, "vec": vs, "fts_hit": False,
                "date": card["date"],
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

    # ── entities: FTS5 + vec lane merged into a single candidate pool ──────
    # Symmetric with memes / milestone — FTS catches keyword hits against the
    # FULL row body (name+fact+aliases), vec catches paraphrase. No more
    # reverse-substring on alias triggers.
    ent_by_id = {c["id"]: c for c in _entity_candidates(conn, q, limit)}
    for card in entities_vec_cards:
        if card["id"] in ent_by_id:
            ent_by_id[card["id"]]["vec"] = card["vec_score"]
        elif card["vec_score"] >= _VEC_ONLY_FLOOR:
            fact = card["fact"]
            if not fact:
                continue
            name = card["name"]
            ekind = card["kind"] or ""
            content = f"{name} ({ekind}): {fact}" if ekind else f"{name}: {fact}"
            ent_by_id[card["id"]] = {
                "kind": "entity", "id": card["id"],
                "session_id": None,
                "timestamp": card["created_at"],
                "role": "entity", "content": content,
                "channel": None, "compressed": 0,
                "bm25": 0.0, "vec": card["vec_score"], "fts_hit": False,
                "mention_count": int(card["mention_count"] or 0),
                "entity_kind": ekind,
            }
    # Strong-hit merge: substring-scan all live entities; mark strong tier
    # ((name)/(body)) for needle matches. Bypasses vec floor.
    for r, tier in _entity_strong_hits(conn, q_lower):
        eid = r["id"]
        if eid in ent_by_id:
            ent_by_id[eid]["strong"] = tier
            continue
        fact = r["fact"] or ""
        if not fact.strip():
            continue
        name = r["name"] or ""
        ekind = r["kind"] or ""
        content = f"{name} ({ekind}): {fact}" if ekind else f"{name}: {fact}"
        ent_by_id[eid] = {
            "kind": "entity", "id": eid,
            "session_id": None,
            "timestamp": r["created_at"] or "",
            "role": "entity", "content": content,
            "channel": None, "compressed": 0,
            "bm25": 0.0, "vec": 0.0, "fts_hit": False,
            "mention_count": int(r["mention_count"] or 0),
            "entity_kind": ekind,
            "strong": tier,
        }
    entity_cands = list(ent_by_id.values())

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
                    "AND importance<=2",
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

        # cwd-bucket bias: nudge same-context events up, cross-context down.
        # Only fires when BOTH the current cwd and the event's session cwd
        # classify into a known bucket — sessions without recorded cwd stay
        # neutral so the backfill gap (pre-cwd events) doesn't get silently
        # demoted. Rules + magnitudes come from [recall.buckets] config.
        if cur_bucket:
            ev_bucket = _cwd_bucket(c.get("session_cwd"), bucket_rules)
            if ev_bucket:
                if ev_bucket == cur_bucket:
                    raw += same_boost
                else:
                    raw -= diff_penalty

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
    # vec dominates; bm25 adds keyword signal). Vec pre-gate: rows with
    # vec < _ANCHOR_VEC_FLOOR are dropped unless strong-hit bypasses it.
    for mc in milestone_cands:
        vec_val = mc.get("vec", 0.0)
        strong = mc.get("strong", False)
        if not strong and vec_val < _ANCHOR_VEC_FLOOR:
            continue
        raw = w_bm25 * mc["bm25"] + w_milestones_vec * vec_val
        raw += _ANCHOR_BIAS  # see _ANCHOR_BIAS
        if strong:
            raw = max(raw, _STRONG_NAME_FLOOR if strong == "name"
                      else _STRONG_BODY_FLOOR)
        scored.append((raw, {**mc, "score": raw}))

    # ── memes scoring (mirror milestone) ─────────────────────────────────────
    for vc in memes_cands:
        vec_val = vc.get("vec", 0.0)
        strong = vc.get("strong", False)
        if not strong and vec_val < _ANCHOR_VEC_FLOOR:
            continue
        raw = w_bm25 * vc["bm25"] + w_memes_vec * vec_val
        raw += _ANCHOR_BIAS  # see _ANCHOR_BIAS
        if strong:
            raw = max(raw, _STRONG_NAME_FLOOR if strong == "name"
                      else _STRONG_BODY_FLOOR)
        scored.append((raw, {**vc, "score": raw}))

    # ── diary scoring (vec only — evergreen long-form prose) ─────────────────
    if "diary" not in exclude_kinds:
        for dc in diary_cands:
            raw = w_diary_vec * dc.get("vec", 0.0)
            scored.append((raw, {**dc, "score": raw}))

    # ── tasks scoring (vec only — evergreen study + project surface) ────────
    if "task" not in exclude_kinds:
        for tc in tasks_cands:
            raw = w_tasks_vec * tc.get("vec", 0.0)
            scored.append((raw, {**tc, "score": raw}))

    # ── entity scoring (FTS + vec + mention boost, vec pre-gate) ───────────
    # vec dominates; bm25 adds keyword signal. FTS-only (vec=0) dropped same
    # as milestone/memes — CJK trigram noise. mention_count boost kept.
    # strong-hit bypasses vec floor.
    for ec in entity_cands:
        vec_val = ec.get("vec", 0.0)
        strong = ec.get("strong", False)
        if not strong and vec_val < _ANCHOR_VEC_FLOOR:
            continue
        raw = (
            w_bm25 * ec["bm25"] + w_entities_vec * vec_val
            + min(0.05, 0.02 * math.log1p(ec.get("mention_count", 0)))
        )
        raw += _ANCHOR_BIAS  # see _ANCHOR_BIAS
        if strong:
            raw = max(raw, _STRONG_NAME_FLOOR if strong == "name"
                      else _STRONG_BODY_FLOOR)
        scored.append((raw, {**ec, "score": raw}))

    # ── unified min_score gate ──────────────────────────────────────────────
    # All lanes (events / anchors / diary / tasks) must clear the same floor.
    # Without this, anchor lanes (milestone/memes/diary/tasks/entity) bypass
    # the gate entirely and surface unrelated rows on every query.
    scored = [(s, r) for s, r in scored if s >= min_score]
    scored.sort(key=lambda x: x[0], reverse=True)

    # ── pick top-N by raw score (no per-kind reservation) ────────────────────
    # All lanes compete on raw score; min_score gate already excluded noise.
    # Event adjacency dedup: hook attaches ±1 same-session context per event
    # hit, so neighbouring event ids (diff ≤ 1, same session) would surface
    # twice. Keep the highest-scored of each adjacent run.
    picks: list = []
    chosen_event_ids: dict[str, list[int]] = {}
    for s, r in scored:
        kind = r.get("kind") or "event"
        if kind not in ("entity", "milestone", "memes", "diary", "task"):
            sid = r.get("session_id")
            rid = r.get("id")
            if sid and rid:
                ids = chosen_event_ids.setdefault(sid, [])
                if any(abs(int(rid) - x) <= 1 for x in ids):
                    continue
                ids.append(int(rid))
        picks.append((s, r))
        if len(picks) >= limit:
            break

    # ── row passthrough ───────────────────────────────────────────────────────
    # No per-item content cap — caller (hook / MCP) owns final shaping.
    # `budget_chars` (when set) is a defensive cumulative backstop only.
    out: list[dict] = []
    used = 0
    for _, row in picks[:limit]:
        content = re.sub(r"^\[time:[^\]]+\]\s*", "", row["content"] or "")
        content = re.sub(r'\s*<(?:image|file)\s+path="[^"]*?"[^>]*>\s*', " ", content).strip()
        if budget_chars is not None and used + len(content) > budget_chars:
            break
        out.append({**row, "content": content})
        used += len(content)

    return out


# ── config-driven entrypoint (used by hook + MCP daemon) ─────────────────────

def recall_with_config(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int | None = None,
    budget_chars: int | None = None,
    current_cwd: str | None = None,
    exclude_kinds: tuple[str, ...] = ("diary", "task"),
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Run recall_fusion with weights + thresholds from [recall] config.

    Single shared path so hook (UserPromptSubmit) and MCP daemon return the
    same shape for the same query. Caller may override limit/budget per call.
    `current_cwd` enables per-event same-bucket boost / cross-bucket penalty
    (CC-Lab=project, ~/my-dashboard=daily, Study=study).
    `exclude_kinds`: kinds to suppress. Hook default = ("diary", "task");
    MCP callers pass () to include all kinds.
    `since`/`until`: UTC ISO strings for time-lane filtering.
    """
    from . import config as _config
    rcfg = _config.load().get("recall", {})
    _weight_keys = {
        "w_vec", "w_bm25", "w_recency", "w_affect",
        "w_memes_vec", "w_entities_vec", "w_milestones_vec",
        "w_diary_vec", "w_tasks_vec", "min_score",
    }
    # Strip WX time-anchor prefix before query reaches FTS + vec.
    # Format: "[time: <...> | gap: <...>] <actual query>"
    # Strip once at entry; downstream sees the clean query only.
    q = re.sub(r"^\[time:[^\]]+\]\s*", "", query.strip())
    # budget_chars is hook-side post-shaping (per-kind rules). Fusion stays
    # passthrough — callers that want a hard char cap pass it explicitly.
    return recall_fusion(
        conn, q,
        limit=int(limit if limit is not None else rcfg.get("limit", 6)),
        budget_chars=budget_chars,
        current_cwd=current_cwd,
        exclude_kinds=exclude_kinds,
        since=since,
        until=until,
        **{k: float(rcfg[k]) for k in _weight_keys if k in rcfg},
    )


def fetch_window_digests(
    conn: sqlite3.Connection,
    since_utc: str,
    until_utc: str,
    cap: int = 6,
) -> list[dict]:
    """Return session_digests rows whose ts falls in [since_utc, until_utc).

    Falls back to matching the date column against Melbourne-local dates in
    the window when ts is missing/malformed. Newest first.
    Each row: {"kind": "digest", "id": sid, "date": ..., "content": text[:150]}.
    """
    from .timeutil import utc_iso_to_local_date as _u2d
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    # Primary: ts-based filter
    rows = conn.execute(
        "SELECT sid, date, text, ts FROM session_digests "
        "WHERE ts >= ? AND ts < ? "
        "ORDER BY ts DESC LIMIT ?",
        (since_utc, until_utc, cap),
    ).fetchall()

    if not rows:
        # Fallback: match date column against Melbourne-local dates in window
        try:
            _s = _dt.fromisoformat(since_utc.replace("Z", "+00:00"))
            _e = _dt.fromisoformat(until_utc.replace("Z", "+00:00"))
            dates: list[str] = []
            _cur = _s
            while _cur <= _e:
                dates.append(_u2d(_cur.strftime("%Y-%m-%dT%H:%M:%SZ")))
                _cur += _td(days=1)
        except Exception:
            dates = []
        if dates:
            placeholders = ",".join("?" * len(dates))
            rows = conn.execute(
                f"SELECT sid, date, text, ts FROM session_digests "
                f"WHERE date IN ({placeholders}) "
                f"ORDER BY ts DESC LIMIT ?",
                (*dates, cap),
            ).fetchall()

    out: list[dict] = []
    for r in rows:
        out.append({
            "kind": "digest",
            "id": r["sid"],
            "date": r["date"],
            "content": (r["text"] or "")[:150],
            "score": 0.0,
        })
    return out


# ── event context window helper ──────────────────────────────────────────────

def fetch_event_context(
    conn: sqlite3.Connection,
    session_id: str,
    event_id: int,
    n: int = 1,
) -> list[dict]:
    """Return up to 2n adjacent events from the same session.

    Order: prev_n ... prev_1, next_1 ... next_n (oldest-first within each
    side; target event itself excluded). Used by the recall hook so an event
    snippet ships with surrounding turns — gives the model context to judge
    whether the hit is the latest take or a since-corrected earlier one.
    """
    if not session_id or n <= 0 or event_id <= 0:
        return []
    prev = conn.execute(
        "SELECT id, role, content, timestamp FROM events "
        "WHERE session_id = ? AND id < ? "
        "ORDER BY id DESC LIMIT ?",
        (session_id, event_id, n),
    ).fetchall()
    nxt = conn.execute(
        "SELECT id, role, content, timestamp FROM events "
        "WHERE session_id = ? AND id > ? "
        "ORDER BY id ASC LIMIT ?",
        (session_id, event_id, n),
    ).fetchall()
    out: list[dict] = []
    for r in reversed(prev):
        out.append({
            "id": r["id"], "role": r["role"],
            "content": r["content"] or "",
            "timestamp": r["timestamp"] or "",
            "rel": "prev",
        })
    for r in nxt:
        out.append({
            "id": r["id"], "role": r["role"],
            "content": r["content"] or "",
            "timestamp": r["timestamp"] or "",
            "rel": "next",
        })
    return out
