"""v36 (id-reuse -> AUTOINCREMENT rebuild) + v37 (md_index path-key merge)."""
import os
import re
import sqlite3

import pytest

from marrow import storage
from marrow.md_index import MdIndex, _canon

_AUTOINC = ("events", "entities", "memes", "milestones", "stickers")

# Minimal NOT-NULL payload per table so a raw INSERT succeeds.
_INSERT = {
    "events": ("INSERT INTO events(session_id,timestamp,role,content)"
               " VALUES('s','2026-01-01T00:00:00Z','user',?)"),
    "entities": ("INSERT INTO entities(kind,name) VALUES('person',?)"),
    "memes": ("INSERT INTO memes(type,key) VALUES('meme',?)"),
    "milestones": ("INSERT INTO milestones(scope,date,title)"
                   " VALUES('me','2026-01-01',?)"),
    "stickers": ("INSERT INTO stickers(path) VALUES(?)"),
}


def _deps(conn, table):
    return sorted(
        (r["type"], r["name"], r["sql"])
        for r in conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=?"
            " AND type IN ('trigger','index')", (table,)
        ).fetchall()
    )


def _downgrade_to_plain_pk(conn, table):
    """Test-only inverse of the migration: strip AUTOINCREMENT, keep rows+deps.

    Mirrors the production rebuild (FK-safe DROP via legacy_alter_table) so a
    v36 run afterwards exercises the real code path on a realistic table.
    """
    orig = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
    new_sql = re.sub(
        rf'CREATE TABLE (IF NOT EXISTS )?"?{re.escape(table)}"?',
        f'CREATE TABLE "{table}_old"', orig, count=1)
    new_sql = new_sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                              "INTEGER PRIMARY KEY", 1)
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    cl = ", ".join(f'"{c}"' for c in cols)
    deps = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=?"
        " AND type IN ('trigger','index') AND sql IS NOT NULL AND name!=?",
        (table, table)).fetchall()]
    conn.execute(new_sql)
    conn.execute(f"INSERT INTO {table}_old ({cl}) SELECT {cl} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f'ALTER TABLE "{table}_old" RENAME TO "{table}"')
    for d in deps:
        conn.execute(d)


def _make_prev36_db(tmp_path):
    """Fresh DB then rolled back to a plain-PK, user_version=35 state."""
    path = str(tmp_path / "prev36.db")
    conn = storage.init_db(path)
    # Seed rows + FK links before downgrade.
    with conn:
        conn.execute(_INSERT["events"], ("hello world",))
        eid = conn.execute("SELECT id FROM events").fetchone()[0]
        conn.execute("INSERT INTO affect(date,ep,event_id,valence,arousal,"
                     "importance) VALUES('2026-01-01',0,?,0.1,0.1,2)", (eid,))
        conn.execute(_INSERT["entities"], ("Ada",))
        conn.execute(_INSERT["entities"], ("Bob",))
        rows = [r[0] for r in conn.execute("SELECT id FROM entities ORDER BY id")]
        conn.execute("UPDATE entities SET superseded_by=? WHERE id=?",
                     (rows[0], rows[1]))
        for t in ("memes", "milestones", "stickers"):
            conn.execute(_INSERT[t], ("x",))
    before_deps = {t: _deps(conn, t) for t in _AUTOINC}
    conn.execute("PRAGMA foreign_keys=OFF")
    with conn:
        conn.execute("PRAGMA legacy_alter_table=ON")
        for t in _AUTOINC:
            _downgrade_to_plain_pk(conn, t)
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA user_version=35")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn, before_deps, eid, (rows[1], rows[0])


# ── fresh install ──────────────────────────────────────────────────────────


def test_fresh_install_all_targets_autoincrement(tmp_path):
    conn = storage.init_db(str(tmp_path / "fresh.db"))
    try:
        for t in _AUTOINC:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (t,)
            ).fetchone()[0]
            assert "AUTOINCREMENT" in sql.upper(), t
        assert (conn.execute("PRAGMA user_version").fetchone()[0]
                == storage.SCHEMA_VERSION)
    finally:
        conn.close()


@pytest.mark.parametrize("table", _AUTOINC)
def test_no_id_reuse_after_delete(tmp_path, table):
    conn = storage.init_db(str(tmp_path / f"reuse-{table}.db"))
    try:
        with conn:
            conn.execute(_INSERT[table], ("a",))
        first = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
        with conn:
            conn.execute(f"DELETE FROM {table} WHERE id=?", (first,))
            conn.execute(_INSERT[table], ("b",))
        second = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
        assert second > first, f"{table}: id {second} reused freed {first}"
    finally:
        conn.close()


# ── v36 full migration on a realistic pre-v36 DB ────────────────────────────


def test_v36_converts_and_preserves_parity(tmp_path):
    conn, before_deps, eid, (sup_child, sup_parent) = _make_prev36_db(tmp_path)
    try:
        # sanity: downgrade really produced plain PK.
        assert "AUTOINCREMENT" not in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='events'"
        ).fetchone()[0].upper()
        storage._migrate_to_v36(conn)
        for t in _AUTOINC:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]
            assert "AUTOINCREMENT" in sql.upper(), t
            assert _deps(conn, t) == before_deps[t], f"{t} dep parity"
        # FK links survived the DROP/rebuild.
        assert conn.execute(
            "SELECT event_id FROM affect").fetchone()[0] == eid
        assert conn.execute(
            "SELECT superseded_by FROM entities WHERE id=?", (sup_child,)
        ).fetchone()[0] == sup_parent
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 36
    finally:
        conn.close()


def test_v36_idempotent(tmp_path):
    conn, _b, _e, _s = _make_prev36_db(tmp_path)
    try:
        storage._migrate_to_v36(conn)
        events_sql_1 = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='events'").fetchone()[0]
        conn.execute("PRAGMA user_version=35")  # force re-entry
        storage._migrate_to_v36(conn)  # already AUTOINCREMENT -> no rebuild
        events_sql_2 = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='events'").fetchone()[0]
        assert events_sql_1 == events_sql_2
    finally:
        conn.close()


def test_rebuild_seeds_sqlite_sequence(tmp_path):
    conn, _b, _e, _s = _make_prev36_db(tmp_path)
    try:
        storage._migrate_to_v36(conn)
        mx = conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()[0]
        assert seq == mx
    finally:
        conn.close()


# ── v37 md_index path-key merge ─────────────────────────────────────────────


def test_v37_merges_symlinked_path_rows(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    conn = storage.init_db(str(tmp_path / "v37.db"))
    try:
        real_p = str((real / "memes.md").resolve())
        link_p = str(link / "memes.md")  # symlink lane, canon == real_p
        assert _canon(link_p) == real_p
        # Two lanes, same block. link lane tombstoned, real lane live+newer.
        conn.execute("DELETE FROM md_index")
        conn.execute(
            "INSERT INTO md_index(path,block_id,content_hash,last_seen_at,"
            "tombstone_at) VALUES(?,?,?,?,?)",
            (link_p, "5", "h_old", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.execute(
            "INSERT INTO md_index(path,block_id,content_hash,last_seen_at,"
            "tombstone_at) VALUES(?,?,?,?,?)",
            (real_p, "5", "h_new", "2026-02-01T00:00:00Z", None))
        conn.execute("PRAGMA user_version=36")
        storage._migrate_to_v37(conn)
        rows = conn.execute(
            "SELECT path,content_hash,tombstone_at FROM md_index").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == real_p           # canonical real path
        assert rows[0][1] == "h_new"          # newest observation won
        assert rows[0][2] is None
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 37
    finally:
        conn.close()


def test_md_index_canon_choke_point(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    conn = storage.init_db(str(tmp_path / "canon.db"))
    try:
        idx = MdIndex(conn)
        # Write through the symlink lane, tombstone through the real lane.
        idx.record_block(str(link / "memes.md"), "7", "h")
        idx.tombstone(str(real / "memes.md"), "7")
        # Both lanes now resolve to the same single row.
        assert idx.get_hash(str(real / "memes.md"), "7") is None  # tombstoned
        assert idx.is_tombstoned(str(link / "memes.md"), "7")
        assert conn.execute("SELECT COUNT(*) FROM md_index").fetchone()[0] == 1
    finally:
        conn.close()
