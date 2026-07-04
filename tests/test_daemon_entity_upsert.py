"""entity_upsert MCP tool (C4 dims update) — recall-miss create, hit update.

Embedder is unavailable in tests, so match_entity falls back to the alias/name
overlap gate (cosine step no-ops with a warn). That is enough to exercise the
create / update / reject paths deterministically.
"""
from __future__ import annotations

import pytest

from marrow import config, daemon, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    storage.init_db(db).close()
    monkeypatch.setattr(daemon, "_DB", db)
    monkeypatch.setattr(config, "db_path", lambda: db)
    return db


def _rows(db):
    conn = storage.connect(db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, kind, name, fact, aliases, source FROM entities"
            " ORDER BY id").fetchall()]
    finally:
        conn.close()


def test_create_on_miss(env):
    out = daemon.entity_upsert("person", "王医生", fact="ED consultant")
    assert out["ok"] is True
    assert out["action"] == "create"
    rows = _rows(env)
    assert len(rows) == 1
    assert rows[0]["name"] == "王医生"
    assert rows[0]["fact"] == "ED consultant"
    assert rows[0]["source"] == "session"


def test_update_fact_on_name_hit(env):
    first = daemon.entity_upsert("person", "王医生", fact="ED consultant")
    out = daemon.entity_upsert("person", "王医生", fact="ED director now")
    assert out["action"] == "update"
    assert out["id"] == first["id"]
    rows = _rows(env)
    assert len(rows) == 1
    assert rows[0]["fact"] == "ED director now"


def test_update_merges_aliases_on_alias_hit(env):
    daemon.entity_upsert("person", "王医生", aliases=["Dr Wang"])
    out = daemon.entity_upsert("person", "Dr Wang", aliases=["老王"])
    assert out["action"] == "update"
    rows = _rows(env)
    assert len(rows) == 1
    assert "老王" in rows[0]["aliases"]
    assert rows[0]["name"] == "王医生"  # canonical row untouched


def test_reject_unknown_kind(env):
    out = daemon.entity_upsert("gadget", "iPhone")
    assert out["ok"] is False
    assert "kind" in out["error"]
    assert _rows(env) == []


def test_reject_empty_name(env):
    out = daemon.entity_upsert("person", "   ")
    assert out["ok"] is False
    assert _rows(env) == []


def test_distinct_names_create_separate_rows(env):
    daemon.entity_upsert("place", "Clayton gym")
    daemon.entity_upsert("place", "Monash library")
    assert len(_rows(env)) == 2
