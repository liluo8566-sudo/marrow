"""SQLite store: connection factory (sqlite-vec loaded), schema, idempotent init.

Schema granularity matches SCHEMA.md: SCHEMA-named key columns + id/timestamps.
Exact indexes and which columns get embedded stay build-time, widened per phase.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import sqlite_vec

from . import config

SCHEMA_VERSION = 17

# Phase 1 first-class tables + Phase 2 affect/entities (DECISIONS Phase 2).
# The retired emotions/people/preferences/dir placeholders stay absent.
_TABLES = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  channel TEXT,
  compressed INTEGER NOT NULL DEFAULT 0,
  source_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
-- Deleted events: source_hash of rows Lumi purged. archive_events skips any
-- hash listed here so a SessionEnd/catchup re-archive can't resurrect them.
CREATE TABLE IF NOT EXISTS event_tombstones (
  source_hash TEXT PRIMARY KEY,
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  due TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  next_step TEXT,
  last_session_summary TEXT,
  context_pointers TEXT,
  outcome_log TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS milestones (
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL,
  date TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  theme TEXT,
  pinned INTEGER NOT NULL DEFAULT 0,
  source_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS memes (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT,
  context TEXT,
  use_count INTEGER NOT NULL DEFAULT 0,
  last_seen TEXT,
  pinned INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  source_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS stickers (
  id INTEGER PRIMARY KEY,
  meme_id INTEGER REFERENCES memes(id) ON DELETE SET NULL,
  key TEXT NOT NULL,
  asset_path TEXT NOT NULL,
  mime_type TEXT,
  use_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS pit (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'idea',
  related_files TEXT,
  source_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS diary (
  date TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  mood TEXT,
  session_ids TEXT,
  tl_line TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS goose_bites (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  session_id TEXT,
  bites TEXT NOT NULL,
  best INTEGER NOT NULL DEFAULT 0,
  source_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY,
  severity TEXT NOT NULL,
  type TEXT NOT NULL,
  fingerprint TEXT,
  message TEXT NOT NULL,
  source TEXT,
  hit_count INTEGER NOT NULL DEFAULT 1,
  resolved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  target_table TEXT NOT NULL,
  target_id TEXT,
  action TEXT NOT NULL,
  summary TEXT,
  occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS affect (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  ep INTEGER NOT NULL,
  event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
  valence REAL NOT NULL,
  arousal REAL NOT NULL,
  importance INTEGER NOT NULL,
  label TEXT,
  description TEXT,
  entities TEXT,
  mention_count INTEGER NOT NULL DEFAULT 0,
  source TEXT,
  superseded_by INTEGER REFERENCES affect(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  fact TEXT,
  aliases TEXT,
  mention_count INTEGER NOT NULL DEFAULT 0,
  source TEXT,
  superseded_by INTEGER REFERENCES entities(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS events_vec_meta (
  rowid INTEGER PRIMARY KEY,
  embedder_id TEXT NOT NULL,
  dim INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS memes_vec_meta (
  rowid INTEGER PRIMARY KEY,
  embedder_id TEXT NOT NULL,
  dim INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS entities_vec_meta (
  rowid INTEGER PRIMARY KEY,
  embedder_id TEXT NOT NULL,
  dim INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS milestones_vec_meta (
  rowid INTEGER PRIMARY KEY,
  embedder_id TEXT NOT NULL,
  dim INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS diary_vec_meta (
  rowid INTEGER PRIMARY KEY,
  embedder_id TEXT NOT NULL,
  dim INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks_vec_meta (
  rowid INTEGER PRIMARY KEY,
  embedder_id TEXT NOT NULL,
  dim INTEGER NOT NULL
);
-- B1 (2026-06-02): sessions table — one row per (sid, model). Bridge
-- swap_provider upserts on every model swap; /resume <sid> reads model back
-- so a cross-client resume preserves the selected model. channel = wx | cli |
-- (slack…); title is optional human label set by bridge UI.
CREATE TABLE IF NOT EXISTS sessions (
  sid TEXT PRIMARY KEY,
  model TEXT,
  channel TEXT,
  cwd TEXT,
  last_active TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  title TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_last_active
  ON sessions(last_active DESC);
"""

# superseded_by IS NULL = the current row. Recall/backdrop read the live view.
_VIEWS = """
CREATE VIEW IF NOT EXISTS affect_live AS
  SELECT * FROM affect WHERE superseded_by IS NULL;
CREATE VIEW IF NOT EXISTS entities_live AS
  SELECT * FROM entities WHERE superseded_by IS NULL;
"""

# FTS5 over the bulk recall surface (events.content), kept in sync by triggers.
# tokenize=trigram is the only built-in tokenizer that splits CJK; the default
# unicode61 emits zero CJK tokens, so CN event_hint match silently returns []
# (Phase 1 bug — see review-phase-2.md). Trigram requires ≥3-char phrase
# queries for CN, acceptable for event_hint short phrases.
_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  content, content='events', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, content) VALUES('delete', old.id, old.content);
  INSERT INTO events_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

# FTS5 over the anchor tables (memes / milestones / entities). Standalone
# (no content=); body = whole-row TRIM(col || ' ' || col || ...) so query
# matches against the FULL row content, not just title/key. Trigram tokenizer
# = same CJK behaviour as events_fts (≥3-char phrase for CN). Triggers keep
# rows in sync; first-time backfill happens in init_db once the table exists.
_FTS_EXT = """
CREATE VIRTUAL TABLE IF NOT EXISTS memes_fts USING fts5(
  body, tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS memes_ai AFTER INSERT ON memes BEGIN
  INSERT INTO memes_fts(rowid, body) VALUES (new.id,
    TRIM(COALESCE(new.key,'') || ' ' || COALESCE(new.value,'') || ' ' || COALESCE(new.context,''))
  );
END;
CREATE TRIGGER IF NOT EXISTS memes_ad AFTER DELETE ON memes BEGIN
  DELETE FROM memes_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS memes_au AFTER UPDATE ON memes BEGIN
  DELETE FROM memes_fts WHERE rowid = old.id;
  INSERT INTO memes_fts(rowid, body) VALUES (new.id,
    TRIM(COALESCE(new.key,'') || ' ' || COALESCE(new.value,'') || ' ' || COALESCE(new.context,''))
  );
END;

CREATE VIRTUAL TABLE IF NOT EXISTS milestones_fts USING fts5(
  body, tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS milestones_ai AFTER INSERT ON milestones BEGIN
  INSERT INTO milestones_fts(rowid, body) VALUES (new.id,
    TRIM(COALESCE(new.title,'') || ' ' || COALESCE(new.description,''))
  );
END;
CREATE TRIGGER IF NOT EXISTS milestones_ad AFTER DELETE ON milestones BEGIN
  DELETE FROM milestones_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS milestones_au AFTER UPDATE ON milestones BEGIN
  DELETE FROM milestones_fts WHERE rowid = old.id;
  INSERT INTO milestones_fts(rowid, body) VALUES (new.id,
    TRIM(COALESCE(new.title,'') || ' ' || COALESCE(new.description,''))
  );
END;

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
  body, tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
  INSERT INTO entities_fts(rowid, body) VALUES (new.id,
    TRIM(COALESCE(new.name,'') || ' ' || COALESCE(new.fact,'') || ' ' || COALESCE(new.aliases,''))
  );
END;
CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
  DELETE FROM entities_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
  DELETE FROM entities_fts WHERE rowid = old.id;
  INSERT INTO entities_fts(rowid, body) VALUES (new.id,
    TRIM(COALESCE(new.name,'') || ' ' || COALESCE(new.fact,'') || ' ' || COALESCE(new.aliases,''))
  );
END;

"""


_FTS_EXT_BACKFILL: dict[str, str] = {
    "memes_fts": (
        "INSERT INTO memes_fts(rowid, body) "
        "SELECT id, TRIM(COALESCE(key,'') || ' ' || COALESCE(value,'') "
        "             || ' ' || COALESCE(context,'')) FROM memes"
    ),
    "milestones_fts": (
        "INSERT INTO milestones_fts(rowid, body) "
        "SELECT id, TRIM(COALESCE(title,'') || ' ' || COALESCE(description,'')) "
        "FROM milestones"
    ),
    "entities_fts": (
        "INSERT INTO entities_fts(rowid, body) "
        "SELECT id, TRIM(COALESCE(name,'') || ' ' || COALESCE(fact,'') "
        "             || ' ' || COALESCE(aliases,'')) FROM entities"
    ),
}


def _vec_table(dim: int, name: str = "events_vec") -> str:
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} "
        f"USING vec0(embedding float[{dim}])"
    )


# Cross-table vec lanes (Phase 2 cross-table recall, 2026-05-25). Each row in
# the named main table can have a 1024d row in <name>_vec; tracked by
# <name>_vec_meta. Same shape as events_vec so the embed write path stays one
# helper.
_VEC_LANES = (
    "memes_vec", "entities_vec", "milestones_vec", "diary_vec", "tasks_vec",
)


def _ondisk_vec_dim(conn: sqlite3.Connection, name: str = "events_vec") -> int | None:
    r = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name=?", (name,)
    ).fetchone()
    if not r:
        return None
    m = re.search(r"float\[(\d+)\]", r[0])
    return int(m.group(1)) if m else None


def connect(path: str | None = None) -> sqlite3.Connection:
    cfg = config.load()
    db = path or cfg["paths"]["db"]
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: str | None = None) -> sqlite3.Connection:
    cfg = config.load()
    conn = connect(path)
    dim = int(cfg.get("embedding", {}).get("dim", 1024))
    with conn:
        # Pre-_TABLES renames: legacy populated tables (`threads`, `vocab`)
        # must be renamed BEFORE CREATE TABLE IF NOT EXISTS runs, or both
        # the legacy and the new empty table end up coexisting.
        _pre_v2_rename(conn)
        _pre_v5_rename(conn)
        conn.executescript(_TABLES)
        # FTS5 tokenizer migration: Phase 1 shipped unicode61 (CJK tokenless).
        # Drop + rebuild with trigram when stale; rebuild only on migration.
        r = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='events_fts'"
        ).fetchone()
        need_fts_rebuild = bool(r and r[0] and "trigram" not in r[0])
        if need_fts_rebuild:
            conn.execute("DROP TABLE events_fts")
        conn.executescript(_FTS)
        if need_fts_rebuild:
            conn.execute(
                "INSERT INTO events_fts(events_fts) VALUES('rebuild')")
        # Anchor-table FTS5 (memes / milestones / entities). Created here so
        # the triggers attach before any subsequent writer fires. First-time
        # backfill: if the fts row count = 0 but the base table is non-empty,
        # bulk insert. Idempotent — re-run on existing populated FTS is a no-op.
        # Migrate any pre-fix triggers that used the external-content `'delete'`
        # command (illegal on standalone FTS5) before recreating.
        for _tbl in ("memes", "milestones", "entities"):
            _r = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (f"{_tbl}_ad",)
            ).fetchone()
            if _r and _r[0] and "VALUES('delete'" in _r[0]:
                conn.execute(f"DROP TRIGGER IF EXISTS {_tbl}_ai")
                conn.execute(f"DROP TRIGGER IF EXISTS {_tbl}_ad")
                conn.execute(f"DROP TRIGGER IF EXISTS {_tbl}_au")
        conn.executescript(_FTS_EXT)
        for fts_name, sql in _FTS_EXT_BACKFILL.items():
            base = fts_name.removesuffix("_fts")
            fts_n = conn.execute(
                f"SELECT count(*) FROM {fts_name}").fetchone()[0]
            base_n = conn.execute(
                f"SELECT count(*) FROM {base}").fetchone()[0]
            if fts_n == 0 and base_n > 0:
                conn.execute(sql)
        conn.executescript(_VIEWS)
        # Vector dim change (e.g. 384 placeholder -> 1024 bge-m3): vec0 has
        # no ALTER. Empty -> drop+rebuild (lossless). Non-empty -> leave the
        # old table untouched + alert; never silently discard embeddings.
        cur_dim = _ondisk_vec_dim(conn)
        if cur_dim is not None and cur_dim != dim:
            n = conn.execute(
                "SELECT count(*) FROM events_vec").fetchone()[0]
            if n == 0:
                conn.execute("DROP TABLE events_vec")
            else:
                _fp = "embedding_dim_mismatch:events_vec"
                _msg = (f"events_vec dim={cur_dim} != config {dim}; "
                        f"{n} rows preserved, manual re-embed required")
                _existing = conn.execute(
                    "SELECT id FROM alerts"
                    " WHERE type='embedding_dim_mismatch'"
                    " AND fingerprint=? AND resolved=0 LIMIT 1", (_fp,)
                ).fetchone()
                if _existing:
                    conn.execute(
                        "UPDATE alerts SET hit_count=hit_count+1,"
                        " message=?, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                        " WHERE id=?", (_msg, _existing["id"]))
                else:
                    conn.execute(
                        "INSERT INTO alerts (severity, type, fingerprint, message, source)"
                        " VALUES ('warn','embedding_dim_mismatch',?,?,'storage.py:init_db')",
                        (_fp, _msg))
        conn.execute(_vec_table(dim))
        # Cross-table vec lanes (memes/entities/milestones). Same dim as
        # events_vec; mismatch handling mirrors above (empty -> drop+rebuild,
        # non-empty -> leave + alert).
        for lane in _VEC_LANES:
            cur_lane_dim = _ondisk_vec_dim(conn, lane)
            if cur_lane_dim is not None and cur_lane_dim != dim:
                n = conn.execute(f"SELECT count(*) FROM {lane}").fetchone()[0]
                if n == 0:
                    conn.execute(f"DROP TABLE {lane}")
                else:
                    _fp = f"embedding_dim_mismatch:{lane}"
                    _msg = (f"{lane} dim={cur_lane_dim} != config {dim}; "
                            f"{n} rows preserved, manual re-embed required")
                    _existing = conn.execute(
                        "SELECT id FROM alerts"
                        " WHERE type='embedding_dim_mismatch'"
                        " AND fingerprint=? AND resolved=0 LIMIT 1", (_fp,)
                    ).fetchone()
                    if _existing:
                        conn.execute(
                            "UPDATE alerts SET hit_count=hit_count+1,"
                            " message=?, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                            " WHERE id=?", (_msg, _existing["id"]))
                    else:
                        conn.execute(
                            "INSERT INTO alerts (severity, type, fingerprint, message, source)"
                            " VALUES ('warn','embedding_dim_mismatch',?,?,'storage.py:init_db')",
                            (_fp, _msg))
            conn.execute(_vec_table(dim, lane))
        # Schema-evolution backfill: a column added after a db already
        # exists is not applied by CREATE IF NOT EXISTS. Idempotent —
        # duplicate-column ALTER is swallowed; add a row per new column.
        # SQLite ALTER cannot use non-constant defaults, so the column is
        # added nullable then backfilled from created_at on the same pass.
        for tbl, col, decl in (
            ("goose_bites", "source_hash", "TEXT"),
            ("milestones", "updated_at", "TEXT"),
            ("sessions", "cwd", "TEXT"),
        ):
            try:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "UPDATE milestones SET updated_at = created_at "
            "WHERE updated_at IS NULL"
        )
        _migrate_to_v2(conn)
        _migrate_to_v3(conn)
        _migrate_to_v4(conn)
        _migrate_to_v5(conn)
        _migrate_to_v6(conn)
        _migrate_to_v7(conn)
        _migrate_to_v8(conn)
        _migrate_to_v9(conn)
        _migrate_to_v10(conn)
        _migrate_to_v11(conn)
        _migrate_to_v12(conn)
        _migrate_to_v13(conn)
        _migrate_to_v14(conn)
        _migrate_to_v15(conn)
        _migrate_to_v16(conn)
        _migrate_to_v17(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn


def _pre_v2_rename(conn: sqlite3.Connection) -> None:
    """Pre-_TABLES rename: legacy `threads` -> `tasks`. Idempotent.

    Must run BEFORE `_TABLES` so CREATE TABLE IF NOT EXISTS tasks does not
    create a sibling empty table next to the populated legacy one.
    """
    has_threads = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
    ).fetchone()
    has_tasks = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    if has_threads and not has_tasks:
        conn.execute("ALTER TABLE threads RENAME TO tasks")


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """v2 schema: affect Unresolved/reconcile cols + session_digests table.
    Idempotent — duplicate ALTER swallowed; user_version short-circuits.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 2:
        return
    for col, decl in (
        ("unresolved", "INTEGER DEFAULT 0"),
        ("reconcile_ref", "INTEGER REFERENCES affect(id)"),
        ("resolved_at", "TEXT"),
        ("reconcile_prev_text", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE affect ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    # session_digests: one row per sessionend_async DIGEST result.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_digests ("
        "  sid TEXT PRIMARY KEY,"
        "  date TEXT NOT NULL,"
        "  text TEXT NOT NULL,"
        "  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_digests_date"
        " ON session_digests(date)"
    )


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v3 schema: memes.pinned (LLM-written, aging exemption) +
    memes.status (code-written by aging job — 'active' | 'dormant').
    Idempotent — duplicate ALTER swallowed; user_version short-circuits.
    Pre-v5 the table was named `vocab`; v5 renames it. This migration may
    therefore run against either name; resolve at call time.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 3:
        return
    tbl = "memes" if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memes'"
    ).fetchone() else "vocab"
    for col, decl in (
        ("pinned", "INTEGER NOT NULL DEFAULT 0"),
        ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ):
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """v4 schema: affect.description — short event anchor phrase per ep,
    surface field for Today/Week render. Existing rows backfill NULL.
    Idempotent — duplicate ALTER swallowed; user_version short-circuits.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 4:
        return
    try:
        conn.execute("ALTER TABLE affect ADD COLUMN description TEXT")
    except sqlite3.OperationalError:
        pass


def _pre_v5_rename(conn: sqlite3.Connection) -> None:
    """Pre-_TABLES rename: legacy `vocab` -> `memes`. Idempotent.

    Must run BEFORE `_TABLES` so CREATE TABLE IF NOT EXISTS memes does not
    create a sibling empty table next to the populated legacy one.
    Also handles stickers.vocab_id -> stickers.meme_id column rename
    (SQLite >=3.25 supports RENAME COLUMN).
    """
    has_memes = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memes'"
    ).fetchone()
    has_vocab = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vocab'"
    ).fetchone()
    if has_vocab and not has_memes:
        conn.execute("ALTER TABLE vocab RENAME TO memes")
    has_stickers = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stickers'"
    ).fetchone()
    if has_stickers:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(stickers)")}
        if "vocab_id" in cols and "meme_id" not in cols:
            conn.execute(
                "ALTER TABLE stickers RENAME COLUMN vocab_id TO meme_id"
            )


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    """v5 schema bump only. The actual rename lives in _pre_v5_rename so it
    runs BEFORE CREATE TABLE IF NOT EXISTS memes. Idempotent.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 5:
        return


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    """v6: entities.aliases TEXT — JSON list of CN/EN alias strings so
    reverse-match in entity_recall hits cross-language queries
    (Colours <-> 颜色 / colour; 南南 <-> Allen). Idempotent.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 6:
        return
    try:
        conn.execute("ALTER TABLE entities ADD COLUMN aliases TEXT")
    except sqlite3.OperationalError:
        pass


def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    """v7: cross-table vec lanes (memes_vec / entities_vec / milestones_vec).
    Tables are created unconditionally in init_db (via _VEC_LANES loop), so
    this bump is a version sentinel only. Backfill happens on the next
    embed_pending() call.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 7:
        return


def _migrate_to_v8(conn: sqlite3.Connection) -> None:
    """v8: diary vec lane (diary_vec / diary_vec_meta). Table + meta are
    created unconditionally in init_db; this bump is a version sentinel.
    Backfill happens on the next embed_pending() call.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 8:
        return


def _migrate_to_v9(conn: sqlite3.Connection) -> None:
    """v9: tasks vec lane (tasks_vec / tasks_vec_meta) covering study +
    projects (both render from the `tasks` table filtered by category).
    Table + meta created unconditionally in init_db; this bump is a version
    sentinel. Backfill happens on the next embed_pending() call.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 9:
        return


def _migrate_to_v10(conn: sqlite3.Connection) -> None:
    """v10: md_index table (Phase 3 md=SoT). Per-block content hash per file,
    keyed (path, block_id). tombstone_at non-null = user deleted the block;
    blocks the auto-writer from resurrecting same id with same hash. Watcher
    + inserter share this table. Idempotent.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 10:
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS md_index ("
        "  path TEXT NOT NULL,"
        "  block_id TEXT NOT NULL,"
        "  content_hash TEXT NOT NULL,"
        "  last_seen_at TEXT NOT NULL,"
        "  tombstone_at TEXT,"
        "  PRIMARY KEY (path, block_id)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_md_index_path ON md_index(path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_md_index_tombstone"
        " ON md_index(tombstone_at) WHERE tombstone_at IS NOT NULL"
    )


def _migrate_to_v11(conn: sqlite3.Connection) -> None:
    """v11: memes_reject_log — persistent (key, type, reason) counter so a
    repeat-rejected candidate fast-skips the gate (no sonnet tokens burnt re-
    extracting the same dup next round). Only persistent reasons land here
    (dup_milestone / dup_entity / cosine_dup); freq_gate rejects are
    time-relative and never logged.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 11:
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memes_reject_log ("
        "  key TEXT NOT NULL,"
        "  type TEXT NOT NULL,"
        "  reason TEXT NOT NULL,"
        "  count INTEGER NOT NULL DEFAULT 1,"
        "  last_rejected_at TEXT NOT NULL,"
        "  PRIMARY KEY (key, type, reason)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memes_reject_log_key_type"
        " ON memes_reject_log(key, type)"
    )


def _migrate_to_v12(conn: sqlite3.Connection) -> None:
    """v12: atlas table — manually editable heading-tree subpage that replaces
    dir_tree.md. One row per directory; depth controls auto-stub expansion;
    stale marks dirs no longer found on fs (NEVER deleted, preserves manual
    fields). Idempotent.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 12:
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS atlas ("
        "  path TEXT PRIMARY KEY,"
        "  note TEXT,"
        "  write_hint TEXT,"
        "  naming_hint TEXT,"
        "  depth INTEGER NOT NULL DEFAULT 0,"
        "  stale INTEGER NOT NULL DEFAULT 0,"
        "  updated_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_atlas_stale ON atlas(stale)"
    )


def _migrate_to_v15(conn: sqlite3.Connection) -> None:
    """v15: alerts dedup hardening — add fingerprint + hit_count + updated_at.

    The legacy dedup key (severity, type, message, source) failed whenever a
    callsite embedded high-cardinality fields (sid, hash, exception text) into
    `message`, producing one row per call (760 silent_death rows in one hour
    on 2026-06-05). The new key (type, fingerprint) lets callers separate the
    stable dedup identity from human-readable detail; repeats bump hit_count
    instead of inserting a duplicate row. Idempotent ALTER (duplicate-column
    OperationalError is swallowed).
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 15:
        return
    for col, decl in (
        ("fingerprint", "TEXT"),
        ("hit_count", "INTEGER NOT NULL DEFAULT 1"),
        ("updated_at", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    # Backfill fingerprint = message for legacy rows so unresolved-state dedup
    # keeps behaving the same until callsites are migrated.
    conn.execute(
        "UPDATE alerts SET fingerprint = message "
        "WHERE fingerprint IS NULL"
    )
    conn.execute(
        "UPDATE alerts SET updated_at = created_at "
        "WHERE updated_at IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alerts_dedup "
        "ON alerts(type, fingerprint, resolved)"
    )


def _migrate_to_v16(conn: sqlite3.Connection) -> None:
    """v16: events.recall_count + events.last_recalled_at — best-effort stats
    updated on recall hits. recall_count feeds vec eviction exemption (aging)
    and future recall-hit boost. last_recalled_at is UTC ISO string.
    Idempotent — duplicate ALTER is swallowed; user_version short-circuits.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 16:
        return
    for col, decl in (
        ("recall_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_recalled_at", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_recall_count"
        " ON events(recall_count) WHERE recall_count > 0"
    )


def _migrate_to_v17(conn: sqlite3.Connection) -> None:
    """v17: session_digests structured columns (kind/tl_line/life_lines) +
    diary.tl_line + session_digests_fts FTS table.

    kind: 'casual' or 'task' — model's explicit session classification.
    tl_line: 15-30 CN char timeline line (life perspective, plain words).
    life_lines: newline-joined life detail lines (casual only; NULL for task).
    diary.tl_line: 25-40 char day summary written by daily.py diary call.
    Idempotent — duplicate ALTER swallowed; user_version short-circuits.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 17:
        return
    for tbl, col, decl in (
        ("session_digests", "kind", "TEXT"),
        ("session_digests", "tl_line", "TEXT"),
        ("session_digests", "life_lines", "TEXT"),
        ("diary", "tl_line", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    # FTS5 over session_digests tl_line + life_lines. session_digests is
    # created in v2; safe to add FTS here since v17 runs after v2.
    # Standalone (no content=); triggers keep in sync.
    conn.executescript("""
CREATE VIRTUAL TABLE IF NOT EXISTS session_digests_fts USING fts5(
  body, tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS session_digests_ai AFTER INSERT ON session_digests BEGIN
  INSERT INTO session_digests_fts(rowid, body) VALUES (new.rowid,
    TRIM(COALESCE(new.tl_line,'') || ' ' || COALESCE(new.life_lines,''))
  );
END;
CREATE TRIGGER IF NOT EXISTS session_digests_ad AFTER DELETE ON session_digests BEGIN
  DELETE FROM session_digests_fts WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS session_digests_au AFTER UPDATE ON session_digests BEGIN
  DELETE FROM session_digests_fts WHERE rowid = old.rowid;
  INSERT INTO session_digests_fts(rowid, body) VALUES (new.rowid,
    TRIM(COALESCE(new.tl_line,'') || ' ' || COALESCE(new.life_lines,''))
  );
END;
    """)
    # Backfill FTS for existing rows that have tl_line/life_lines populated.
    fts_n = conn.execute(
        "SELECT count(*) FROM session_digests_fts").fetchone()[0]
    if fts_n == 0:
        conn.execute(
            "INSERT INTO session_digests_fts(rowid, body) "
            "SELECT rowid, TRIM(COALESCE(tl_line,'') || ' ' || COALESCE(life_lines,'')) "
            "FROM session_digests "
            "WHERE tl_line IS NOT NULL OR life_lines IS NOT NULL"
        )


def _migrate_to_v14(conn: sqlite3.Connection) -> None:
    """v14: sessions table for cross-client model persistence.

    CREATE TABLE IF NOT EXISTS in _TABLES handles brand-new databases; this
    migration is a no-op on existing dbs because the table is created via
    executescript(_TABLES) every connect (idempotent). Kept for the
    user_version bump so SCHEMA_VERSION stays monotonic.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 14:
        return
    # _TABLES script already created the table. Nothing else to do.


def _migrate_to_v13(conn: sqlite3.Connection) -> None:
    """v13: atlas schema slim — drop note/write_hint/stale; add description.

    SQLite cannot drop columns natively; use table-recreate pattern.
    note + write_hint merged into description. stale replaced by DELETE.
    Idempotent via PRAGMA user_version guard.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 13:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(atlas)").fetchall()}
    if "write_hint" not in cols and "note" not in cols:
        return
    conn.executescript("""
        CREATE TABLE atlas_new (
            path TEXT PRIMARY KEY,
            description TEXT,
            naming_hint TEXT,
            depth INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        INSERT INTO atlas_new(path, description, naming_hint, depth, updated_at)
        SELECT path,
               CASE
                 WHEN write_hint IS NOT NULL AND write_hint != '' AND note IS NOT NULL AND note != ''
                   THEN note || ' | ' || write_hint
                 WHEN write_hint IS NOT NULL AND write_hint != ''
                   THEN write_hint
                 ELSE note
               END,
               naming_hint, depth, updated_at
        FROM atlas;
        DROP INDEX IF EXISTS idx_atlas_stale;
        DROP TABLE atlas;
        ALTER TABLE atlas_new RENAME TO atlas;
    """)
