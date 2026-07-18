"""timeline_page: thin dashboard writer (fork-local, replaces retired P2 chain)."""
from __future__ import annotations

import sqlite3

import pytest

from marrow import config, storage, timeline_page
from marrow.tl_writer import tl_add


@pytest.fixture()
def conn():
    c = storage.init_db(":memory:")
    yield c
    c.close()


def _cfg_with_dashboard(path: str):
    base = config.load()  # snapshot BEFORE monkeypatching config.load
    base.setdefault("paths", {})["dashboard"] = path
    return base


def test_update_writes_timeline_md(conn, tmp_path, monkeypatch):
    tl_add(conn, timerange="10:00-11:00", user_word="乐",
           assistant_word="稳", body="缝合上游大包", importance=3)
    out = tmp_path / "dash" / "dashboard.md"
    cfg = _cfg_with_dashboard(str(out))
    monkeypatch.setattr(timeline_page.config, "load", lambda: cfg)
    timeline_page.update(conn)
    md = out.read_text()
    assert md.startswith("# Dashboard")
    assert "缝合上游大包" in md
    assert "## Timeline" in md


def test_update_noop_without_dashboard_path(conn, tmp_path, monkeypatch):
    cfg = config.load()
    cfg.setdefault("paths", {})["dashboard"] = ""
    monkeypatch.setattr(timeline_page.config, "load", lambda: cfg)
    # Must not raise nor create anything.
    timeline_page.update(conn)
    assert list(tmp_path.iterdir()) == []


def test_update_overwrites_previous_render(conn, tmp_path, monkeypatch):
    out = tmp_path / "dashboard.md"
    out.write_text("stale old render")
    tl_add(conn, timerange="21:00-22:00", user_word="困",
           assistant_word="哄", body="夜里装轻量面板", importance=2)
    cfg = _cfg_with_dashboard(str(out))
    monkeypatch.setattr(timeline_page.config, "load", lambda: cfg)
    timeline_page.update(conn)
    md = out.read_text()
    assert "stale old render" not in md
    assert "夜里装轻量面板" in md
