"""goal/wish MCP tools (C3 marrow-side plumbing) + recall cortex guard."""
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
    out = daemon.goal("set", "sleep", "8", "h")
    assert out == {"ok": True, "key": "sleep", "value": "8", "unit": "h"}
    rows = daemon.goal("list")
    assert rows == [{"key": "sleep", "value": "8", "unit": "h",
                      "updated_at": rows[0]["updated_at"]}]


def test_goal_set_updates_existing_key(env):
    daemon.goal("set", "sleep", "7", "h")
    daemon.goal("set", "sleep", "8", "h")
    rows = daemon.goal("list")
    assert len(rows) == 1
    assert rows[0]["value"] == "8"


def test_goal_set_requires_key_and_value(env):
    assert daemon.goal("set", "", "8")["ok"] is False
    assert daemon.goal("set", "sleep", "")["ok"] is False


def test_goal_list_multiple_sorted(env):
    daemon.goal("set", "sleep", "8", "h")
    daemon.goal("set", "exercise", "3", "x/week")
    rows = daemon.goal("list")
    assert [r["key"] for r in rows] == ["exercise", "sleep"]


def test_goal_delete_removes_key(env):
    daemon.goal("set", "sleep", "8", "h")
    out = daemon.goal("delete", "sleep")
    assert out == {"ok": True, "key": "sleep", "deleted": True}
    assert daemon.goal("list") == []


def test_goal_delete_missing_key_reports_not_deleted(env):
    out = daemon.goal("delete", "nope")
    assert out == {"ok": True, "key": "nope", "deleted": False}


def test_goal_unknown_action(env):
    out = daemon.goal("nope")
    assert out["ok"] is False


def test_wish_creates_file_with_header(env, tmp_path, monkeypatch):
    home = tmp_path / "cortex"
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"home": str(home)}})
    out = daemon.wish("新出的那个奶茶")
    assert out["ok"] is True
    path = home / "wishlist.md"
    assert path.exists()
    assert out["path"] == str(path)
    text = path.read_text(encoding="utf-8")
    assert "# Wishlist" in text
    assert "新出的那个奶茶" in text
    assert out["line"] in text


def test_wish_appends_never_touches_prior_lines(env, tmp_path, monkeypatch):
    home = tmp_path / "cortex"
    home.mkdir(parents=True)
    wishlist = home / "wishlist.md"
    wishlist.write_text("# Wishlist\n\n- 2026-01-01 her own hand-written note\n",
                         encoding="utf-8")
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"home": str(home)}})
    daemon.wish("second wish")
    text = wishlist.read_text(encoding="utf-8")
    assert "her own hand-written note" in text
    assert "second wish" in text
    assert text.index("her own hand-written note") < text.index("second wish")


def test_wish_requires_text(env, tmp_path, monkeypatch):
    home = tmp_path / "cortex"
    monkeypatch.setattr(config, "load", lambda: {"cortex": {"home": str(home)}})
    assert daemon.wish("")["ok"] is False
    assert not home.exists() or not (home / "wishlist.md").exists()


def test_wish_uses_explicit_wishlist_path(env, tmp_path, monkeypatch):
    target = tmp_path / "somewhere" / "my-wishes.md"
    monkeypatch.setattr(config, "load", lambda: {
        "cortex": {"home": str(tmp_path / "cortex"), "wishlist_path": str(target)},
    })
    daemon.wish("custom path wish")
    assert target.exists()
    assert "custom path wish" in target.read_text(encoding="utf-8")


def test_recall_blocked_under_marrow_cortex(env, monkeypatch):
    """C3 guard (HANDOVER queue item 2): cortex's resumed session loads MCP
    tools full-env (no isolation, MAP §6) — the recall tool must hard-block
    same as tl's add/update actions, matching "cortex gets its own bulletin,
    never chat memory" (hooks.py user_prompt_submit already no-ops the
    passive hook path; this covers the active MCP-tool-call path)."""
    monkeypatch.setenv("MARROW_CORTEX", "1")
    with pytest.raises(RuntimeError, match="cortex"):
        daemon.recall("anything")
