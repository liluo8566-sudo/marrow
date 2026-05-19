import sqlite3

import pytest

from marrow import storage

PHASE1_TABLES = {
    "events", "threads", "milestones", "vocab", "stickers",
    "pit", "diary", "goose_bites", "alerts", "audit_log",
}
PHASE2_ABSENT = {"emotions", "people", "preferences", "dir"}


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
