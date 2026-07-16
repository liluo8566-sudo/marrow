"""SQLite store: connection factory (sqlite-vec loaded), schema, idempotent init.

Schema granularity matches SCHEMA.md: SCHEMA-named key columns + id/timestamps.
Exact indexes and which columns get embedded stay build-time, widened per phase.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

from . import config

SCHEMA_VERSION = 40

# Tables whose id must never be reused (freed-id-reuse disease family): a plain
# INTEGER PRIMARY KEY hands a deleted id back to the next INSERT, and side-tables
# keyed by that id (events_vec_meta, md_index tombstones, ...) then poison the
# newborn row. AUTOINCREMENT keeps ids strictly increasing forever. _TABLES
# seeds new installs correct; _migrate_to_v36 rebuilds existing DBs.
_AUTOINC_TABLES = ("events", "entities", "memes", "milestones", "stickers")

# Phase 1 first-class tables + Phase 2 affect/entities (DECISIONS Phase 2).
# The retired emotions/people/preferences/dir placeholders stay absent.
_TABLES = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  channel TEXT,
  compressed INTEGER NOT NULL DEFAULT 0,
  source_hash TEXT,
  ts_start TEXT,
  ts_end TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT
);
-- Deleted events: source_hash of rows the user purged. archive_events skips any
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
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT,
  use_count INTEGER NOT NULL DEFAULT 0,
  last_seen TEXT,
  pinned INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  source_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS stickers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  sha256 TEXT,
  phash TEXT,
  desc TEXT,
  source TEXT NOT NULL DEFAULT 'finder',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  last_used TEXT,
  updated_at TEXT
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
  tone TEXT,
  overview TEXT,
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
CREATE TRIGGER IF NOT EXISTS alerts_sync_resolved
AFTER UPDATE OF resolved_at ON alerts
WHEN NEW.resolved_at IS NOT NULL AND NEW.resolved = 0
BEGIN
  UPDATE alerts SET resolved = 1 WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS alerts_sync_resolved_insert
AFTER INSERT ON alerts
WHEN NEW.resolved_at IS NOT NULL AND NEW.resolved = 0
BEGIN
  UPDATE alerts SET resolved = 1 WHERE id = NEW.id;
END;
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
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  fact TEXT,
  aliases TEXT,
  mention_count INTEGER NOT NULL DEFAULT 0,
  source TEXT,
  superseded_by INTEGER REFERENCES entities(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT
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
CREATE TABLE IF NOT EXISTS stickers_vec_meta (
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
  created_at TEXT,
  last_active TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  ended_at TEXT,
  title TEXT NOT NULL DEFAULT '',
  effort TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_last_active
  ON sessions(last_active DESC);
CREATE TABLE IF NOT EXISTS session_watermarks (
  sid TEXT NOT NULL,
  segment_seq INTEGER NOT NULL,
  last_event_id INTEGER NOT NULL,
  last_turn_idx INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (sid, segment_seq)
);
-- v40: cross-channel message drop (msg MCP tool → outbox → channel adapters).
-- One row per note. target = tg | wx | cli | ct | session:<full-sid>. Delivery
-- is at-most-once, claimed via a single UPDATE...WHERE status='pending'.
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  from_sid TEXT,
  from_channel TEXT,
  target TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  sent_at TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  watch_reply INTEGER NOT NULL DEFAULT 0,
  watch_timeout_min INTEGER,
  watch_state TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status_target ON outbox(status, target);
CREATE INDEX IF NOT EXISTS idx_outbox_watch_state_sent
  ON outbox(watch_state, sent_at);
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
    TRIM(COALESCE(new.key,'') || ' ' || COALESCE(new.value,''))
  );
END;
CREATE TRIGGER IF NOT EXISTS memes_ad AFTER DELETE ON memes BEGIN
  DELETE FROM memes_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS memes_au AFTER UPDATE ON memes BEGIN
  DELETE FROM memes_fts WHERE rowid = old.id;
  INSERT INTO memes_fts(rowid, body) VALUES (new.id,
    TRIM(COALESCE(new.key,'') || ' ' || COALESCE(new.value,''))
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
        "SELECT id, TRIM(COALESCE(key,'') || ' ' || COALESCE(value,'')) FROM memes"
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
    "stickers_vec",
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
        # Cascade vec/meta cleanup on event deletion — separate trigger
        # so it's created after vec tables exist (events_ad only handles FTS).
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS events_ad_vec
            AFTER DELETE ON events BEGIN
                DELETE FROM events_vec WHERE rowid = old.id;
                DELETE FROM events_vec_meta WHERE rowid = old.id;
            END
        """)
        # Schema-evolution backfill: a column added after a db already
        # exists is not applied by CREATE IF NOT EXISTS. Idempotent —
        # duplicate-column ALTER is swallowed; add a row per new column.
        # SQLite ALTER cannot use non-constant defaults, so the column is
        # added nullable then backfilled from created_at on the same pass.
        for tbl, col, decl in (
            ("milestones", "updated_at", "TEXT"),
            ("sessions", "cwd", "TEXT"),
            ("events", "updated_at", "TEXT"),
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
        _migrate_to_v18(conn)
        _migrate_to_v19(conn)
        _migrate_to_v20(conn)
        _migrate_to_v21(conn)
        _migrate_to_v22(conn)
        _migrate_to_v23(conn)
        _migrate_to_v24(conn)
        _migrate_to_v25(conn)
        _migrate_to_v26(conn)
        _migrate_to_v27(conn)
        _migrate_to_v28(conn)
        _migrate_to_v29(conn)
        _migrate_to_v30(conn)
        _migrate_to_v31(conn)
        _migrate_to_v32(conn)
        _migrate_to_v33(conn)
        _migrate_to_v34(conn)
        _migrate_to_v35(conn)
        _migrate_to_v36(conn)
        _migrate_to_v37(conn)
        _migrate_to_v38(conn)
        _migrate_to_v39(conn)
        _migrate_to_v40(conn)
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
        "  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),"
        "  updated_at TEXT"
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


def _migrate_to_v18(conn: sqlite3.Connection) -> None:
    """v18: tl_hidden flag on session_digests + diary — lets user permanently
    hide a timeline line from future renders without deleting the row.
    Idempotent — duplicate ALTER swallowed; user_version short-circuits.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 18:
        return
    for tbl, col, decl in (
        ("session_digests", "tl_hidden", "INTEGER NOT NULL DEFAULT 0"),
        ("diary", "tl_hidden", "INTEGER NOT NULL DEFAULT 0"),
    ):
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass


def _migrate_to_v19(conn: sqlite3.Connection) -> None:
    """v19: stickers table C2 schema — drop meme-era columns, add path/sha256/
    phash/desc/source/last_used. Table is empty in prod so DROP+recreate is safe.
    Idempotent via user_version + column check.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 19:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(stickers)")}
    if "meme_id" in cols:
        conn.execute("DROP TABLE stickers")


def _migrate_to_v20(conn: sqlite3.Connection) -> None:
    """v20: sessions.effort for cross-channel /switch inheritance.
    Idempotent via user_version + column check.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 20:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "effort" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN effort TEXT DEFAULT ''")
    conn.execute("PRAGMA user_version=20")


def _migrate_to_v21(conn: sqlite3.Connection) -> None:
    """v21: sessions.created_at for real session duration in /info.
    Idempotent via user_version + column check.
    Backfills existing rows with last_active as best approximation.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 21:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "created_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN created_at TEXT")
    conn.execute(
        "UPDATE sessions SET created_at = last_active WHERE created_at IS NULL"
    )
    conn.execute("PRAGMA user_version=21")


def _migrate_to_v22(conn: sqlite3.Connection) -> None:
    """v22: stickers.updated_at for sync-loop detection of desc edits.
    Idempotent via user_version + column check.
    Backfills existing rows with created_at as best approximation.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 22:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(stickers)")}
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE stickers ADD COLUMN updated_at TEXT")
    conn.execute(
        "UPDATE stickers SET updated_at = created_at WHERE updated_at IS NULL"
    )
    conn.execute("PRAGMA user_version=22")


def _migrate_to_v23(conn: sqlite3.Connection) -> None:
    """v23: sessions.ended_at — mark session lifecycle end for housekeep."""
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 23:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "ended_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN ended_at TEXT")
    conn.execute("UPDATE sessions SET ended_at = last_active WHERE ended_at IS NULL")
    conn.execute("PRAGMA user_version=23")


def _migrate_to_v24(conn: sqlite3.Connection) -> None:
    """v24: mid-session watermarks and segmented session digests."""
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 24:
        return
    conn.executescript("""
CREATE TABLE IF NOT EXISTS session_watermarks (
  sid TEXT NOT NULL,
  segment_seq INTEGER NOT NULL,
  last_event_id INTEGER NOT NULL,
  last_turn_idx INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (sid, segment_seq)
);
    """)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(session_digests)")}
    if "segment_seq" not in cols:
        conn.executescript("""
DROP TRIGGER IF EXISTS session_digests_ai;
DROP TRIGGER IF EXISTS session_digests_ad;
DROP TRIGGER IF EXISTS session_digests_au;
DROP TABLE IF EXISTS session_digests_fts;
ALTER TABLE session_digests RENAME TO session_digests_old;
CREATE TABLE session_digests (
  sid TEXT NOT NULL,
  segment_seq INTEGER NOT NULL DEFAULT 0,
  date TEXT NOT NULL,
  text TEXT NOT NULL,
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT,
  kind TEXT,
  tl_line TEXT,
  life_lines TEXT,
  tl_hidden INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (sid, segment_seq)
);
INSERT INTO session_digests
  (sid, segment_seq, date, text, ts, updated_at, kind, tl_line, life_lines, tl_hidden)
SELECT sid, 0, date, text, ts, NULL, kind, tl_line, life_lines, tl_hidden
FROM session_digests_old;
DROP TABLE session_digests_old;
CREATE INDEX IF NOT EXISTS idx_session_digests_date ON session_digests(date);
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
    else:
        conn.executescript("""
DROP TRIGGER IF EXISTS session_digests_ai;
DROP TRIGGER IF EXISTS session_digests_ad;
DROP TRIGGER IF EXISTS session_digests_au;
DROP TABLE IF EXISTS session_digests_fts;
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
    conn.execute(
        "INSERT INTO session_digests_fts(rowid, body) "
        "SELECT rowid, TRIM(COALESCE(tl_line,'') || ' ' || COALESCE(life_lines,'')) "
        "FROM session_digests "
        "WHERE tl_line IS NOT NULL OR life_lines IS NOT NULL"
    )
    conn.execute("PRAGMA user_version=24")


def _migrate_to_v25(conn: sqlite3.Connection) -> None:
    """v25: diary overview fields + session_digests.updated_at."""
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 25:
        return
    for col in ("tone TEXT", "overview TEXT"):
        try:
            conn.execute(f"ALTER TABLE diary ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE session_digests ADD COLUMN updated_at TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA user_version=25")


def _migrate_to_v26(conn: sqlite3.Connection) -> None:
    """v26: ensure session_digests.updated_at for databases already at v25."""
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 26:
        return
    try:
        conn.execute("ALTER TABLE session_digests ADD COLUMN updated_at TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA user_version=26")


def _migrate_to_v27(conn: sqlite3.Connection) -> None:
    """v27: updated_at for entities, memes, affect."""
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 27:
        return
    for tbl in ("entities", "memes", "affect"):
        try:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN updated_at TEXT")
        except sqlite3.OperationalError:
            pass
    conn.execute("PRAGMA user_version=27")


def _migrate_to_v28(conn: sqlite3.Connection) -> None:
    """v28: events.ts_start / ts_end — explicit timerange for channel='self'
    (tl_add) rows. NULL for all other rows; timestamp stays the sort key.
    Idempotent — duplicate ALTER swallowed; user_version short-circuits.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 28:
        return
    for col in ("ts_start TEXT", "ts_end TEXT"):
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    conn.execute("PRAGMA user_version=28")


def _migrate_to_v29(conn: sqlite3.Connection) -> None:
    """v29: events.imp / events.flag — self-authored (role='tl') recall boost,
    retire, milestone SQL (imp) + cortex management marks (flag, open vocab).
    Retires the channel='self' marker: role='tl', channel backfilled to a real
    platform, affect label folded into content. Idempotent.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 29:
        return
    for col in ("imp INTEGER", "flag TEXT"):
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    rows = conn.execute(
        "SELECT id, content FROM events WHERE channel='self'"
    ).fetchall()
    for r in rows:
        af = conn.execute(
            "SELECT label, importance FROM affect WHERE event_id=?"
            " ORDER BY id DESC LIMIT 1",
            (r["id"],),
        ).fetchone()
        label = (af["label"] if af else "") or ""
        imp = af["importance"] if af else None
        content = r["content"] or ""
        if label and not content.lstrip().startswith("【"):
            content = f"【{label}】{content}"
        conn.execute(
            "UPDATE events SET role='tl', channel='cli', content=?, imp=?"
            " WHERE id=?",
            (content, imp, r["id"]),
        )
    conn.execute("PRAGMA user_version=29")


def _migrate_to_v30(conn: sqlite3.Connection) -> None:
    """v30: goals table (C1/C3, Decided 07-03 eve) — key/value/unit pairs set
    via goal(action=set) MCP, read via goal(action=list). No history, latest
    value only."""
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 30:
        return
    conn.executescript("""
CREATE TABLE IF NOT EXISTS goals (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  unit TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
    """)
    conn.execute("PRAGMA user_version=30")


def _migrate_to_v31(conn: sqlite3.Connection) -> None:
    """v31: ct_rate_limit table (C3, HANDOVER queue item 2) — kv snapshot of
    the latest rate_limit_event stream frame, flattened per field. Writer =
    llm.py stream consumption; reader = cortex bulletin (tolerant, latest
    values only, no history)."""
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 31:
        return
    conn.executescript("""
CREATE TABLE IF NOT EXISTS ct_rate_limit (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
    """)
    conn.execute("PRAGMA user_version=31")


def _migrate_to_v32(conn: sqlite3.Connection) -> None:
    """v32: ct_first_tick table (C4, First tick 07-04) — an executing session
    self-marks a cortex-nagged item as seen/handled so other sessions and later
    wakes stop repeat-nagging. Writer = first(action=tick) MCP tool; reader = cortex
    (latest mark per item, no history)."""
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 32:
        return
    conn.executescript("""
CREATE TABLE IF NOT EXISTS ct_first_tick (
  item TEXT PRIMARY KEY,
  seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  sid TEXT,
  note TEXT
);
    """)
    conn.execute("PRAGMA user_version=32")


def _migrate_to_v33(conn: sqlite3.Connection) -> None:
    """v33: drop memes.context — memes reduced to key/value (matches
    entities: name/fact). Retrieval is whole-row vector search; context
    added no signal. Rebuilds memes_fts triggers/body to match.

    Also clears memes_vec / memes_vec_meta: the embedding text definition
    changed (was key/value/context, now key/value only), so vectors built
    under the old definition are stale. The memes pending query skips rows
    with an existing meta row, so without this clear they would never
    re-embed. embed_pending's memes lane repopulates them on next run.

    Idempotent — column-existence check + user_version short-circuit (both
    the schema change and the vec clear are gated by the same "context in
    cols" branch, which only runs once).
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 33:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memes)").fetchall()}
    if "context" in cols:
        conn.execute("DROP TRIGGER IF EXISTS memes_ai")
        conn.execute("DROP TRIGGER IF EXISTS memes_au")
        conn.execute("ALTER TABLE memes DROP COLUMN context")
        conn.executescript("""
CREATE TRIGGER memes_ai AFTER INSERT ON memes BEGIN
  INSERT INTO memes_fts(rowid, body) VALUES (new.id,
    TRIM(COALESCE(new.key,'') || ' ' || COALESCE(new.value,''))
  );
END;
CREATE TRIGGER memes_au AFTER UPDATE ON memes BEGIN
  DELETE FROM memes_fts WHERE rowid = old.id;
  INSERT INTO memes_fts(rowid, body) VALUES (new.id,
    TRIM(COALESCE(new.key,'') || ' ' || COALESCE(new.value,''))
  );
END;
        """)
        conn.execute("DELETE FROM memes_fts")
        conn.execute(
            "INSERT INTO memes_fts(rowid, body) "
            "SELECT id, TRIM(COALESCE(key,'') || ' ' || COALESCE(value,'')) FROM memes"
        )
        conn.execute("DELETE FROM memes_vec")
        conn.execute("DELETE FROM memes_vec_meta")
    conn.execute("PRAGMA user_version=33")


def _migrate_to_v34(conn: sqlite3.Connection) -> None:
    """v34: ct_first_tick.status — column for the tool-side status write
    (later workstream); schema only here. Default 'done' matches the
    seen/handled semantics existing rows already carry implicitly.
    Idempotent — duplicate ALTER swallowed; user_version short-circuits.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 34:
        return
    try:
        conn.execute(
            "ALTER TABLE ct_first_tick ADD COLUMN status TEXT NOT NULL"
            " DEFAULT 'done'"
        )
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA user_version=34")


def _migrate_to_v35(conn: sqlite3.Connection) -> None:
    """v35: unconditional one-time memes-lane vec cleanup (codex review find,
    07-06). _migrate_to_v33's DELETE FROM memes_vec/memes_vec_meta only ran
    for installs that crossed the v33 boundary live — any DB that had already
    reached v33+ under the pre-fix code (e.g. via a fresh init_db that set
    user_version straight to 33/34, or a DB migrated before this cleanup
    existed) never got the clear and can carry stale/poisoned meme vectors
    forever. This migration re-runs the same DELETEs unconditionally so every
    DB gets swept exactly once regardless of migration history.

    Idempotent — DELETE with no matching rows is a no-op; user_version
    short-circuits on second open. embed_pending's memes lane repopulates any
    cleared rows on its next run (self-heals).
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 35:
        return
    conn.execute("DELETE FROM memes_vec")
    conn.execute("DELETE FROM memes_vec_meta")
    conn.execute("PRAGMA user_version=35")


def _backup_db_file(conn: sqlite3.Connection, tag: str) -> None:
    """Best-effort copy of the live DB file to /tmp before a bulk rebuild.

    init_db runs every migration inside one transaction, so a raised error
    already rolls the whole thing back (SQLite atomicity). This extra copy
    defends against a committed-but-wrong logic bug. In-memory / tempfile
    DBs (path empty) are skipped. Failure never aborts the migration.
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchall()
        main = next((r[2] for r in row if r[1] == "main"), None)
        if not main or main == ":memory:" or not Path(main).exists():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dst = Path("/tmp") / f"marrow-{tag}-{stamp}.db.bak"
        shutil.copy2(main, dst)
    except Exception:
        pass


def _rebuild_autoincrement(conn: sqlite3.Connection, table: str) -> bool:
    """Rebuild `table` so its INTEGER PRIMARY KEY becomes AUTOINCREMENT.

    Preserves ids (so rowid-coupled vec/fts stay valid), recreates every
    dependent trigger + index verbatim, and seeds sqlite_sequence to max(id).
    Returns True if a rebuild happened, False if already AUTOINCREMENT.

    FK caveat: with foreign_keys ON (and we cannot toggle it mid-transaction),
    DROP TABLE fires an implicit DELETE that runs ON DELETE SET NULL on any
    child FK. Callers snapshot + restore those columns around this call.
    """
    orig = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if orig is None:
        return False
    if "AUTOINCREMENT" in orig[0].upper():
        return False
    new_sql = re.sub(
        rf'CREATE TABLE (IF NOT EXISTS )?"?{re.escape(table)}"?',
        f'CREATE TABLE "{table}_new"', orig[0], count=1,
    )
    new_sql = re.sub(
        r"INTEGER PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT",
        new_sql, count=1,
    )
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    collist = ", ".join(f'"{c}"' for c in cols)
    # Dependent triggers + indexes (skip auto-indexes: sql IS NULL). FTS/vec
    # twins live under their own tbl_name, so they are NOT captured here and
    # stay untouched (rowids preserved).
    deps = [
        r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=?"
            " AND type IN ('trigger','index') AND sql IS NOT NULL AND name != ?",
            (table, table),
        ).fetchall()
    ]
    conn.execute(new_sql)
    conn.execute(
        f"INSERT INTO {table}_new ({collist}) SELECT {collist} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f'ALTER TABLE "{table}_new" RENAME TO "{table}"')
    for d in deps:
        conn.execute(d)
    conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
    conn.execute(
        "INSERT INTO sqlite_sequence(name, seq)"
        f" SELECT ?, COALESCE(MAX(id),0) FROM {table}",
        (table,),
    )
    return True


def _migrate_to_v36(conn: sqlite3.Connection) -> None:
    """v36: migrate the id-reuse-prone tables to INTEGER PRIMARY KEY
    AUTOINCREMENT (events / entities / memes / milestones / stickers).

    Root cause of a 3-outbreak disease family: plain INTEGER PK reuses a
    freed id after DELETE, and side-tables keyed by that id (events_vec_meta,
    md_index tombstones) then poison the reused id. AUTOINCREMENT stops reuse
    at the source.

    Rebuild preserves ids + recreates triggers/indexes verbatim; the vec/fts
    twins are rowid-coupled and left in place. The only FKs into this set are
    affect.event_id and entities.superseded_by (both ON DELETE SET NULL);
    since DROP TABLE fires those under foreign_keys ON, snapshot + restore.
    Idempotent — already-AUTOINCREMENT tables short-circuit; user_version gate.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 36:
        return
    # Nothing to do if every target is already AUTOINCREMENT (fresh install).
    pending = [
        t for t in _AUTOINC_TABLES
        if (row := conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()) is not None and "AUTOINCREMENT" not in row[0].upper()
    ]
    if not pending:
        conn.execute("PRAGMA user_version=36")
        return
    _backup_db_file(conn, "v36-autoinc")
    # Snapshot the two child FK columns that DROP TABLE would SET NULL.
    affect_map = conn.execute(
        "SELECT id, event_id FROM affect WHERE event_id IS NOT NULL"
    ).fetchall()
    ent_map = conn.execute(
        "SELECT id, superseded_by FROM entities WHERE superseded_by IS NOT NULL"
    ).fetchall()
    # legacy_alter_table ON: the RENAME must NOT rewrite/validate other objects.
    # Views (entities_live/affect_live) and FKs reference these tables by name;
    # a modern RENAME re-parses them mid-rebuild (table momentarily absent) and
    # errors, or rewrites "entities" -> "entities_new". Legacy mode leaves all
    # references as literal text so they resolve once the rename completes.
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        for t in _AUTOINC_TABLES:
            _rebuild_autoincrement(conn, t)
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
    # Restore FK columns (ids were preserved, so the values are still valid).
    for r in affect_map:
        conn.execute(
            "UPDATE affect SET event_id=? WHERE id=?", (r[1], r[0]))
    for r in ent_map:
        conn.execute(
            "UPDATE entities SET superseded_by=? WHERE id=?", (r[1], r[0]))
    conn.execute("PRAGMA user_version=36")


def _canon_md_path(path: str) -> str:
    """Canonical md_index key = expanded + symlink-resolved absolute path.

    The inserter/daemon key md_index by the config symlink path
    (~/.config/marrow/db-pages/...) while the watcher keys by the resolved
    real path (~/Desktop/NY/db-pages/...); the same block lands twice and a
    tombstone on one lane silently blocks rendering on the other. Resolving
    to one canonical form collapses the split. Best-effort — a broken path
    falls back to itself.
    """
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return path


def _migrate_to_v37(conn: sqlite3.Connection) -> None:
    """v37: collapse the md_index path-key split (symlink vs resolved real
    path) onto one canonical form and merge the duplicate rows it created.

    Pairs with the MdIndex path choke point (marrow/md_index.py) that keeps
    every future write canonical. On collision, the most-recently-observed
    row wins (max last_seen_at) so the merged tombstone reflects latest fs
    truth. Idempotent — canon of an already-canon path is identity; a second
    run finds no collisions. user_version gate runs it once.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 37:
        return
    rows = conn.execute(
        "SELECT path, block_id, content_hash, last_seen_at, tombstone_at"
        " FROM md_index"
    ).fetchall()
    def _rank(r: tuple) -> str:
        # A tombstone is always at least as fresh as its own last_seen_at
        # (md_index.py bumps both together going forward); for pre-fix rows
        # written before that change, still prefer tombstone_at when it is
        # the newer of the two so a newer tombstone beats an older active row.
        return max(r[4] or "", r[3] or "")

    best: dict[tuple[str, str], tuple] = {}
    for r in rows:
        canon = _canon_md_path(r[0])
        key = (canon, r[1])
        prev = best.get(key)
        # Most-recently-observed row wins the merge (latest fs truth).
        if prev is None or _rank(r) >= _rank(prev):
            best[key] = (canon, r[1], r[2], r[3], r[4])
    if rows:
        conn.execute("DELETE FROM md_index")
        conn.executemany(
            "INSERT INTO md_index"
            " (path, block_id, content_hash, last_seen_at, tombstone_at)"
            " VALUES (?, ?, ?, ?, ?)",
            list(best.values()),
        )
    conn.execute("PRAGMA user_version=37")


# AUTOINCREMENT tables whose subpage inserter uses str(id) as block_id (see
# subpage_specs.py). events has no subpage; diary/wallet block_ids aren't
# plain table ids — both excluded.
_AUTOINC_MDPAGE = {
    "entities": "profile.md",
    "memes": "memes.md",
    "milestones": "milestone.md",
    "stickers": "stickers.md",
}


def _migrate_to_v38(conn: sqlite3.Connection) -> None:
    """v38: correct sqlite_sequence for installs that ran v36 with a gap.

    v36 seeded sqlite_sequence to max(id) of each table *at rebuild time*.
    But rows created-then-deleted BEFORE v36 leave their id free while
    md_index still holds a (live or tombstoned) row for it — a post-v36
    INSERT can then reuse that higher id, and write_subpage_inserter's
    "no resurrection" branch silently skips appending the md line (ghost
    row: in DB, never rendered). Bumping seq to the highest block_id ever
    observed for that page closes the gap. No-op when there is no gap.
    Idempotent — user_version gate; re-run finds seq already at the max.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 38:
        return
    for table, page in _AUTOINC_MDPAGE.items():
        cur = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)
        ).fetchone()
        cur_seq = cur[0] if cur else 0
        md_max = conn.execute(
            "SELECT MAX(CAST(block_id AS INTEGER)) FROM md_index"
            " WHERE path LIKE ? AND block_id GLOB '[0-9]*'"
            " AND block_id NOT GLOB '*[^0-9]*'",
            (f"%/{page}",),
        ).fetchone()[0] or 0
        if md_max <= cur_seq:
            continue
        if cur is None:
            conn.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                (table, md_max),
            )
        else:
            conn.execute(
                "UPDATE sqlite_sequence SET seq=? WHERE name=?",
                (md_max, table),
            )
    conn.execute("PRAGMA user_version=38")


def _migrate_to_v39(conn: sqlite3.Connection) -> None:
    """v39: events.updated_at — freshness arbitration for timeline reconcile.

    The ALTER runs in the schema-evolution backfill loop above (idempotent);
    this gate only bumps user_version. Existing rows stay NULL on purpose:
    the reconcile gate reads COALESCE(updated_at, created_at), so NULL means
    "never content-edited, base = created_at". Only a content-write path
    (tl_writer.tl_update / reconcile self+manual edit) stamps updated_at.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 39:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN updated_at TEXT")
    conn.execute("PRAGMA user_version=39")


def _migrate_to_v40(conn: sqlite3.Connection) -> None:
    """v40: outbox table — cross-channel message drop (msg MCP tool).

    Table + its two indexes are created via _TABLES / _FTS-style CREATE IF NOT
    EXISTS on every connect (idempotent); this gate only bumps user_version so
    SCHEMA_VERSION stays monotonic. Existing DBs get the table on next init_db.
    """
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= 40:
        return
    conn.execute("PRAGMA user_version=40")


def get_latest_watermark(conn, sid):
    """Return latest watermark row as dict for sid, or None."""
    row = conn.execute(
        "SELECT segment_seq, last_event_id, last_turn_idx, created_at"
        " FROM session_watermarks WHERE sid=? ORDER BY segment_seq DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return dict(row) if row else None


def insert_watermark(conn, sid, segment_seq, last_event_id, last_turn_idx=0):
    """Insert a new watermark row. Caller must ensure segment_seq is correct."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO session_watermarks"
            " (sid, segment_seq, last_event_id, last_turn_idx)"
            " VALUES (?, ?, ?, ?)",
            (sid, segment_seq, last_event_id, last_turn_idx),
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
