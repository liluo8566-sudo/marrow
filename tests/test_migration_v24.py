import sqlite3

from marrow import storage


def _legacy_v23_conn(tmp_path):
    path = tmp_path / "legacy-v23.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
CREATE TABLE session_digests (
  sid TEXT PRIMARY KEY,
  date TEXT NOT NULL,
  text TEXT NOT NULL,
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  kind TEXT,
  tl_line TEXT,
  life_lines TEXT,
  tl_hidden INTEGER NOT NULL DEFAULT 0
);
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
INSERT INTO session_digests
  (sid, date, text, ts, kind, tl_line, life_lines, tl_hidden)
VALUES
  ('sid-1', '2026-06-01', 'body', '2026-06-01T00:00:00Z',
   'casual', 'old marrow line', 'legacy life line', 0);
PRAGMA user_version=23;
    """)
    return conn


def _names(conn):
    return {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def test_v24_migration_creates_session_watermarks(tmp_path):
    conn = _legacy_v23_conn(tmp_path)
    try:
        storage._migrate_to_v24(conn)
        assert "session_watermarks" in _names(conn)
    finally:
        conn.close()


def test_v24_migration_preserves_digests_with_segment_zero(tmp_path):
    conn = _legacy_v23_conn(tmp_path)
    try:
        storage._migrate_to_v24(conn)
        row = conn.execute(
            "SELECT sid, segment_seq, date, text, kind, tl_line, life_lines"
            " FROM session_digests WHERE sid='sid-1'"
        ).fetchone()
        assert dict(row) == {
            "sid": "sid-1",
            "segment_seq": 0,
            "date": "2026-06-01",
            "text": "body",
            "kind": "casual",
            "tl_line": "old marrow line",
            "life_lines": "legacy life line",
        }
    finally:
        conn.close()


def test_v24_session_digests_composite_pk_allows_segments(tmp_path):
    conn = _legacy_v23_conn(tmp_path)
    try:
        storage._migrate_to_v24(conn)
        conn.execute(
            "INSERT INTO session_digests"
            " (sid, segment_seq, date, text, kind, tl_line)"
            " VALUES ('sid-1', 1, '2026-06-01', 'body 2', 'task', 'new line')"
        )
        rows = conn.execute(
            "SELECT segment_seq, text FROM session_digests"
            " WHERE sid='sid-1' ORDER BY segment_seq"
        ).fetchall()
        assert [(r["segment_seq"], r["text"]) for r in rows] == [
            (0, "body"),
            (1, "body 2"),
        ]
    finally:
        conn.close()


def test_v24_session_digests_fts_triggers_still_work(tmp_path):
    conn = _legacy_v23_conn(tmp_path)
    try:
        storage._migrate_to_v24(conn)
        assert conn.execute(
            "SELECT rowid FROM session_digests_fts"
            " WHERE session_digests_fts MATCH 'marrow'"
        ).fetchone() is not None
        conn.execute(
            "INSERT INTO session_digests"
            " (sid, segment_seq, date, text, kind, tl_line, life_lines)"
            " VALUES ('sid-1', 1, '2026-06-01', 'body 2', 'casual',"
            " 'fresh anchor', 'silver thread')"
        )
        assert conn.execute(
            "SELECT rowid FROM session_digests_fts"
            " WHERE session_digests_fts MATCH 'silver'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_v24_migration_idempotent(tmp_path):
    conn = _legacy_v23_conn(tmp_path)
    try:
        storage._migrate_to_v24(conn)
        storage._migrate_to_v24(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 24
        assert conn.execute(
            "SELECT COUNT(*) FROM session_digests WHERE sid='sid-1'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
