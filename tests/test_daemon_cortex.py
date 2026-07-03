"""goal_set/goal_list/wish_add MCP tools (C3 marrow-side plumbing)."""
from __future__ import annotations

import pytest

from marrow import config, daemon, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    storage.init_db(db).close()
    monkeypatch.setattr(daemon, "_DB", db)
    monkeypatch.setattr(config, "db_path", lambda: db)
    return db, tmp_path


def test_goal_set_creates_row(env):
    out = daemon.goal_set("sleep", "8", "h")
    assert out == {"ok": True, "key": "sleep", "value": "8", "unit": "h"}
    rows = daemon.goal_list()
    assert rows == [{"key": "sleep", "value": "8", "unit": "h",
                      "updated_at": rows[0]["updated_at"]}]


def test_goal_set_updates_existing_key(env):
    daemon.goal_set("sleep", "7", "h")
    daemon.goal_set("sleep", "8", "h")
    rows = daemon.goal_list()
    assert len(rows) == 1
    assert rows[0]["value"] == "8"


def test_goal_set_requires_key_and_value(env):
    assert daemon.goal_set("", "8")["ok"] is False
    assert daemon.goal_set("sleep", "")["ok"] is False


def test_goal_list_multiple_sorted(env):
    daemon.goal_set("sleep", "8", "h")
    daemon.goal_set("exercise", "3", "x/week")
    rows = daemon.goal_list()
    assert [r["key"] for r in rows] == ["exercise", "sleep"]


def test_wish_add_creates_file_with_header(env, tmp_path, monkeypatch):
    home = tmp_path / "cortex"
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"home": str(home)}})
    out = daemon.wish_add("新出的那个奶茶")
    assert out["ok"] is True
    path = home / "wishlist.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "# Wishlist" in text
    assert "新出的那个奶茶" in text


def test_wish_add_appends_never_touches_prior_lines(env, tmp_path, monkeypatch):
    home = tmp_path / "cortex"
    home.mkdir(parents=True)
    wishlist = home / "wishlist.md"
    wishlist.write_text("# Wishlist\n\n- 2026-01-01 her own hand-written note\n",
                         encoding="utf-8")
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"home": str(home)}})
    daemon.wish_add("second wish")
    text = wishlist.read_text(encoding="utf-8")
    assert "her own hand-written note" in text
    assert "second wish" in text
    assert text.index("her own hand-written note") < text.index("second wish")


def test_wish_add_requires_text(env, tmp_path, monkeypatch):
    home = tmp_path / "cortex"
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"home": str(home)}})
    assert daemon.wish_add("")["ok"] is False
    assert not home.exists() or not (home / "wishlist.md").exists()


def test_wish_add_uses_explicit_wishlist_path(env, tmp_path, monkeypatch):
    target = tmp_path / "somewhere" / "my-wishes.md"
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {"home": str(tmp_path / "cortex"), "wishlist_path": str(target)},
    })
    daemon.wish_add("custom path wish")
    assert target.exists()
    assert "custom path wish" in target.read_text(encoding="utf-8")
