import sqlite3

import pytest

from marrow import storage

PHASE1_TABLES = {
    "events", "tasks", "milestones", "vocab", "stickers",
    "pit", "diary", "goose_bites", "alerts", "audit_log",
}
PHASE2_ABSENT = {"emotions", "people", "preferences", "dir", "threads"}


@pytest.fixture()
def db(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    yield conn
    conn.close()


def _names(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_phase1_tables_present(db):
    assert PHASE1_TABLES <= _names(db)


def test_phase2_tables_absent(db):
    assert not (PHASE2_ABSENT & _names(db))


def test_user_version(db):
    assert db.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION


def test_fts_synced_on_insert_update_delete(db):
    db.execute("INSERT INTO events(session_id,timestamp,role,content) "
               "VALUES('s','2026-05-17T00:00:00Z','user','hello marrow world')")
    db.commit()
    q = "SELECT rowid FROM events_fts WHERE events_fts MATCH 'marrow'"
    assert db.execute(q).fetchone() is not None
    rid = db.execute("SELECT id FROM events").fetchone()[0]
    db.execute("UPDATE events SET content='changed text' WHERE id=?", (rid,))
    db.commit()
    assert db.execute(q).fetchone() is None
    assert db.execute(
        "SELECT 1 FROM events_fts WHERE events_fts MATCH 'changed'"
    ).fetchone() is not None
    db.execute("DELETE FROM events WHERE id=?", (rid,))
    db.commit()
    assert db.execute(
        "SELECT 1 FROM events_fts WHERE events_fts MATCH 'changed'"
    ).fetchone() is None


def test_vec0_table_usable(db):
    cols = db.execute("PRAGMA table_info(events_vec)").fetchall()
    assert cols, "events_vec virtual table missing"


def test_init_idempotent(tmp_path):
    p = str(tmp_path / "i.db")
    storage.init_db(p).close()
    conn = storage.init_db(p)
    conn.execute("INSERT INTO events(session_id,timestamp,role,content) "
                 "VALUES('s','2026-05-17T00:00:00Z','user','x')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    conn.close()


def test_foreign_key_set_null_on_vocab_delete(db):
    db.execute("INSERT INTO vocab(type,key) VALUES('cipher','P')")
    vid = db.execute("SELECT id FROM vocab").fetchone()[0]
    db.execute("INSERT INTO stickers(vocab_id,key,asset_path) VALUES(?,?,?)",
               (vid, "P", "/tmp/x.png"))
    db.execute("DELETE FROM vocab WHERE id=?", (vid,))
    db.commit()
    assert db.execute("SELECT vocab_id FROM stickers").fetchone()[0] is None


# --- Phase 2 schema freeze (Step 0) ---

AFFECT_COLS = {
    "id", "date", "ep", "event_id", "valence", "arousal", "importance",
    "label", "entities", "mention_count", "source", "superseded_by",
    "created_at",
}
ENTITY_COLS = {
    "id", "kind", "name", "fact", "mention_count", "source",
    "superseded_by", "created_at",
}


def _cols(conn, table):
    return {r["name"] for r in
            conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_affect_schema(db):
    assert "affect" in _names(db)
    assert AFFECT_COLS <= _cols(db, "affect")


def test_entities_schema(db):
    assert "entities" in _names(db)
    assert ENTITY_COLS <= _cols(db, "entities")


def test_affect_live_excludes_superseded(db):
    db.execute("INSERT INTO affect(date,ep,valence,arousal,importance) "
               "VALUES('2026-05-19',1,0.5,0.3,3)")
    old = db.execute("SELECT id FROM affect").fetchone()[0]
    db.execute("INSERT INTO affect(date,ep,valence,arousal,importance) "
               "VALUES('2026-05-19',1,0.8,0.6,5)")
    new = db.execute("SELECT id FROM affect WHERE id<>?", (old,)).fetchone()[0]
    db.execute("UPDATE affect SET superseded_by=? WHERE id=?", (new, old))
    db.commit()
    live = [r["id"] for r in db.execute("SELECT id FROM affect_live")]
    assert live == [new]


def test_entities_live_excludes_superseded(db):
    db.execute("INSERT INTO entities(kind,name,fact) "
               "VALUES('person','Allen','old')")
    old = db.execute("SELECT id FROM entities").fetchone()[0]
    db.execute("INSERT INTO entities(kind,name,fact) "
               "VALUES('person','Allen','new')")
    new = db.execute(
        "SELECT id FROM entities WHERE id<>?", (old,)).fetchone()[0]
    db.execute("UPDATE entities SET superseded_by=? WHERE id=?", (new, old))
    db.commit()
    live = [r["id"] for r in db.execute("SELECT id FROM entities_live")]
    assert live == [new]


def test_affect_event_id_set_null_on_event_delete(db):
    db.execute("INSERT INTO events(session_id,timestamp,role,content) "
               "VALUES('s','2026-05-19T00:00:00Z','user','x')")
    eid = db.execute("SELECT id FROM events").fetchone()[0]
    db.execute("INSERT INTO affect(date,ep,event_id,valence,arousal,"
               "importance) VALUES('2026-05-19',1,?,0.5,0.3,3)", (eid,))
    db.execute("DELETE FROM events WHERE id=?", (eid,))
    db.commit()
    assert db.execute("SELECT event_id FROM affect").fetchone()[0] is None


def test_events_vec_meta_present(db):
    assert "events_vec_meta" in _names(db)
    assert {"rowid", "embedder_id", "dim"} <= _cols(db, "events_vec_meta")


def test_default_config_is_bge_m3_1024():
    """DECISIONS factory contract: ships bge-m3 @ 1024d."""
    import tomllib
    from pathlib import Path

    import marrow
    d = Path(marrow.__file__).with_name("config.default.toml")
    emb = tomllib.loads(d.read_text())["embedding"]
    assert emb["dim"] == 1024 and emb["id"] == "bge-m3"


_VEC_384 = ("CREATE VIRTUAL TABLE IF NOT EXISTS events_vec "
            "USING vec0(embedding float[384])")


def _force_dim(monkeypatch, st, d):
    """init_db reads config.load(); pin embedding.dim for the test."""
    real = st.config.load

    def fake():
        c = real()
        c.setdefault("embedding", {})["dim"] = d
        return c

    monkeypatch.setattr(st.config, "load", fake)


def test_milestones_has_updated_at_column(db):
    """Round 2 Unit 1: milestones.updated_at backs persistent md edits."""
    assert "updated_at" in _cols(db, "milestones")


def test_milestones_updated_at_defaults_on_insert(db):
    db.execute(
        "INSERT INTO milestones(scope,date,title) VALUES('me','2026-05-22','x')"
    )
    db.commit()
    row = db.execute(
        "SELECT created_at, updated_at FROM milestones"
    ).fetchone()
    assert row["updated_at"] is not None
    # Default is the same strftime expression as created_at -> equal here.
    assert row["updated_at"] == row["created_at"]


def test_milestones_updated_at_backfill_on_old_db(tmp_path):
    """Existing rows on an old db schema must get updated_at = created_at."""
    p = str(tmp_path / "legacy.db")
    # Build a legacy milestones table (no updated_at) and insert one row,
    # then re-open with init_db to trigger the ALTER + backfill path.
    legacy = sqlite3.connect(p)
    legacy.execute(
        "CREATE TABLE milestones ("
        "  id INTEGER PRIMARY KEY,"
        "  scope TEXT NOT NULL,"
        "  date TEXT NOT NULL,"
        "  title TEXT NOT NULL,"
        "  description TEXT,"
        "  theme TEXT,"
        "  pinned INTEGER NOT NULL DEFAULT 0,"
        "  source_hash TEXT,"
        "  created_at TEXT NOT NULL DEFAULT "
        "(strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        ")"
    )
    legacy.execute(
        "INSERT INTO milestones(scope,date,title,created_at) "
        "VALUES('me','2026-01-01','old','2026-01-01T00:00:00Z')"
    )
    legacy.commit()
    legacy.close()

    conn = storage.init_db(p)
    try:
        cols = {r["name"] for r in
                conn.execute("PRAGMA table_info(milestones)").fetchall()}
        assert "updated_at" in cols
        row = conn.execute(
            "SELECT created_at, updated_at FROM milestones"
        ).fetchone()
        assert row["updated_at"] == row["created_at"]
    finally:
        conn.close()


def test_events_vec_dim_follows_config(tmp_path, monkeypatch):
    import marrow.storage as st
    _force_dim(monkeypatch, st, 1024)
    conn = st.init_db(str(tmp_path / "c.db"))
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='events_vec'"
    ).fetchone()[0]
    conn.close()
    assert "float[1024]" in sql


def test_vec_dim_migration_rebuilds_when_empty(tmp_path, monkeypatch):
    import marrow.storage as st
    p = str(tmp_path / "m.db")
    monkeypatch.setattr(st, "_vec_table", lambda d: _VEC_384)
    _force_dim(monkeypatch, st, 384)
    st.init_db(p).close()
    monkeypatch.undo()
    _force_dim(monkeypatch, st, 1024)
    conn = st.init_db(p)  # on-disk 384, target 1024, empty -> rebuild
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='events_vec'"
    ).fetchone()[0]
    conn.close()
    assert "float[1024]" in sql


# --- v2 migration (Step 0): tasks rename + session_digests + affect cols ---

def test_v2_threads_renamed_to_tasks(db):
    """Fresh DB at user_version=2: tasks present, threads absent."""
    names = _names(db)
    assert "tasks" in names
    assert "threads" not in names


def test_v2_session_digests_table(db):
    assert "session_digests" in _names(db)
    assert {"sid", "date", "text", "ts"} <= _cols(db, "session_digests")


def test_v2_affect_unresolved_cols(db):
    cols = _cols(db, "affect")
    assert {"unresolved", "reconcile_ref", "resolved_at",
            "reconcile_prev_text"} <= cols


def test_v4_affect_description_col(db):
    """v4: affect.description — short anchor phrase per ep."""
    assert "description" in _cols(db, "affect")
    assert db.execute("PRAGMA user_version").fetchone()[0] >= 4


def test_v2_legacy_threads_rename_preserves_rows(tmp_path):
    """Legacy DB (user_version<2) with `threads` rows → rename to `tasks`."""
    p = str(tmp_path / "legacy.db")
    legacy = sqlite3.connect(p)
    legacy.executescript(
        "CREATE TABLE threads (id INTEGER PRIMARY KEY, category TEXT, "
        "title TEXT, status TEXT, due TEXT, next_step TEXT,"
        " last_session_summary TEXT, context_pointers TEXT,"
        " outcome_log TEXT, created_at TEXT, updated_at TEXT);"
        "INSERT INTO threads (category, title, status)"
        " VALUES ('study', 'legacy-row', 'active');"
        "PRAGMA user_version=1;"
    )
    legacy.commit()
    legacy.close()
    conn = storage.init_db(p)
    try:
        names = _names(conn)
        assert "tasks" in names
        assert "threads" not in names
        row = conn.execute(
            "SELECT title, status FROM tasks WHERE title='legacy-row'"
        ).fetchone()
        assert row is not None
        assert row["status"] == "active"
    finally:
        conn.close()


def test_vec_dim_migration_preserves_when_nonempty(tmp_path, monkeypatch):
    import marrow.storage as st
    p = str(tmp_path / "n.db")
    monkeypatch.setattr(st, "_vec_table", lambda d: _VEC_384)
    _force_dim(monkeypatch, st, 384)
    conn = st.init_db(p)
    conn.execute(
        "INSERT INTO events_vec(rowid,embedding) VALUES(1,?)",
        (b"\x00" * (384 * 4),))
    conn.commit()
    conn.close()
    monkeypatch.undo()
    _force_dim(monkeypatch, st, 1024)
    conn = st.init_db(p)  # dim conflict + non-empty -> must NOT drop data
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='events_vec'"
    ).fetchone()[0]
    n = conn.execute("SELECT count(*) FROM events_vec").fetchone()[0]
    conn.close()
    assert "float[384]" in sql and n == 1
