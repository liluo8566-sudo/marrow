import sqlite3

from marrow import storage


def _legacy_v29_conn(tmp_path):
    path = tmp_path / "legacy-v29.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
    conn.execute("PRAGMA user_version=29")
    return conn


def test_v30_migration_creates_goals_table(tmp_path):
    conn = _legacy_v29_conn(tmp_path)
    try:
        storage._migrate_to_v30(conn)
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "goals" in names
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 30
    finally:
        conn.close()


def test_v30_migration_idempotent(tmp_path):
    conn = _legacy_v29_conn(tmp_path)
    try:
        storage._migrate_to_v30(conn)
        storage._migrate_to_v30(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 30
    finally:
        conn.close()


def test_goals_upsert_by_key(tmp_path):
    conn = _legacy_v29_conn(tmp_path)
    try:
        storage._migrate_to_v30(conn)
        conn.execute(
            "INSERT INTO goals (key, value, unit) VALUES ('sleep', '7', 'h')")
        conn.execute(
            "INSERT INTO goals (key, value, unit, updated_at)"
            " VALUES ('sleep', '8', 'h', '2026-07-03T00:00:00Z')"
            " ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, unit=excluded.unit,"
            " updated_at=excluded.updated_at")
        rows = conn.execute("SELECT key, value FROM goals").fetchall()
        assert [(r["key"], r["value"]) for r in rows] == [("sleep", "8")]
    finally:
        conn.close()
