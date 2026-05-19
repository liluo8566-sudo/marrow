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

SCHEMA_VERSION = 1

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
CREATE TABLE IF NOT EXISTS threads (
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
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS vocab (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT,
  context TEXT,
  use_count INTEGER NOT NULL DEFAULT 0,
  last_seen TEXT,
  source_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS stickers (
  id INTEGER PRIMARY KEY,
  vocab_id INTEGER REFERENCES vocab(id) ON DELETE SET NULL,
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
  message TEXT NOT NULL,
  source TEXT,
  resolved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
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
"""

# superseded_by IS NULL = the current row. Recall/backdrop read the live view.
_VIEWS = """
CREATE VIEW IF NOT EXISTS affect_live AS
  SELECT * FROM affect WHERE superseded_by IS NULL;
CREATE VIEW IF NOT EXISTS entities_live AS
  SELECT * FROM entities WHERE superseded_by IS NULL;
"""

# FTS5 over the bulk recall surface (events.content), kept in sync by triggers.
_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  content, content='events', content_rowid='id'
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


def _vec_table(dim: int) -> str:
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS events_vec "
        f"USING vec0(embedding float[{dim}])"
    )


def _ondisk_vec_dim(conn: sqlite3.Connection) -> int | None:
    r = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='events_vec'"
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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: str | None = None) -> sqlite3.Connection:
    cfg = config.load()
    conn = connect(path)
    dim = int(cfg.get("embedding", {}).get("dim", 1024))
    with conn:
        conn.executescript(_TABLES)
        conn.executescript(_FTS)
        conn.executescript(_VIEWS)
        # Vector dim change (e.g. 384 placeholder -> 1024 bge-m3): vec0 has
        # no ALTER. Empty -> drop+rebuild (lossless). Non-empty -> leave the
        # old table untouched; never silently discard embeddings.
        cur_dim = _ondisk_vec_dim(conn)
        if cur_dim is not None and cur_dim != dim:
            n = conn.execute(
                "SELECT count(*) FROM events_vec").fetchone()[0]
            if n == 0:
                conn.execute("DROP TABLE events_vec")
        conn.execute(_vec_table(dim))
        # Schema-evolution backfill: a column added after a db already
        # exists is not applied by CREATE IF NOT EXISTS. Idempotent —
        # duplicate-column ALTER is swallowed; add a row per new column.
        for tbl, col, decl in (("goose_bites", "source_hash", "TEXT"),):
            try:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn
