"""v38 — sqlite_sequence gap correction from md_index tombstones/live blocks.

Root cause: v36 seeded sqlite_sequence to max(id) at rebuild time, but rows
created-then-deleted BEFORE v36 left their id free while md_index still
holds a row (live or tombstoned) for it — a post-v36 INSERT can reuse that
higher id and the inserter silently ghosts the row. v38 bumps seq to the
highest numeric block_id ever observed on the table's subpage.
"""
from __future__ import annotations

from marrow import storage


def _seq(conn, table):
    row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)
    ).fetchone()
    return row[0] if row else None


def test_v38_bumps_seq_to_tombstoned_md_gap(tmp_path):
    conn = storage.init_db(str(tmp_path / "gap.db"))
    try:
        # Simulate the pre-v36 gap: milestones has 1 live row (seq=1) but
        # md_index (milestone.md) references ids up to 3 (2 tombstoned).
        with conn:
            conn.execute(
                "INSERT INTO milestones(scope,date,title) VALUES('me','2026-01-01','x')"
            )
            conn.execute(
                "INSERT INTO md_index(path,block_id,content_hash,last_seen_at,"
                "tombstone_at) VALUES(?,?,?,?,?)",
                ("/vault/db-pages/milestone.md", "2", "h", "2026-01-01T00:00:00Z",
                 "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO md_index(path,block_id,content_hash,last_seen_at,"
                "tombstone_at) VALUES(?,?,?,?,?)",
                ("/vault/db-pages/milestone.md", "3", "h", "2026-01-01T00:00:00Z",
                 "2026-01-01T00:00:00Z"),
            )
            conn.execute("PRAGMA user_version=37")
        assert _seq(conn, "milestones") == 1
        storage._migrate_to_v38(conn)
        assert _seq(conn, "milestones") == 3
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 38
    finally:
        conn.close()


def test_v38_noop_when_no_gap(tmp_path):
    conn = storage.init_db(str(tmp_path / "nogap.db"))
    try:
        with conn:
            conn.execute(
                "INSERT INTO memes(type,key) VALUES('meme','x')"
            )
            conn.execute(
                "INSERT INTO md_index(path,block_id,content_hash,last_seen_at,"
                "tombstone_at) VALUES(?,?,?,?,?)",
                ("/vault/db-pages/memes.md", "1", "h", "2026-01-01T00:00:00Z", None),
            )
            conn.execute("PRAGMA user_version=37")
        before = _seq(conn, "memes")
        storage._migrate_to_v38(conn)
        assert _seq(conn, "memes") == before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 38
    finally:
        conn.close()


def test_v38_idempotent(tmp_path):
    conn = storage.init_db(str(tmp_path / "idem.db"))
    try:
        with conn:
            conn.execute(
                "INSERT INTO stickers(path) VALUES('a')"
            )
            conn.execute(
                "INSERT INTO md_index(path,block_id,content_hash,last_seen_at,"
                "tombstone_at) VALUES(?,?,?,?,?)",
                ("/vault/db-pages/stickers.md", "9", "h", "2026-01-01T00:00:00Z",
                 "2026-01-01T00:00:00Z"),
            )
            conn.execute("PRAGMA user_version=37")
        storage._migrate_to_v38(conn)
        assert _seq(conn, "stickers") == 9
        conn.execute("PRAGMA user_version=37")  # force re-entry
        storage._migrate_to_v38(conn)
        assert _seq(conn, "stickers") == 9
    finally:
        conn.close()


def test_v38_ignores_non_numeric_block_ids(tmp_path):
    """diary/wallet-style block ids (dates, paths) never inflate seq."""
    conn = storage.init_db(str(tmp_path / "nonnum.db"))
    try:
        with conn:
            conn.execute(
                "INSERT INTO entities(kind,name) VALUES('person','Ada')"
            )
            conn.execute(
                "INSERT INTO md_index(path,block_id,content_hash,last_seen_at,"
                "tombstone_at) VALUES(?,?,?,?,?)",
                ("/vault/db-pages/profile.md", "2026-01-01", "h",
                 "2026-01-01T00:00:00Z", None),
            )
            conn.execute("PRAGMA user_version=37")
        before = _seq(conn, "entities")
        storage._migrate_to_v38(conn)
        assert _seq(conn, "entities") == before
    finally:
        conn.close()
