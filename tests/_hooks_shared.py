"""Shared fixtures/helpers for the tests/test_hooks_*.py family
(split out of the former tests/test_hooks.py)."""
from __future__ import annotations

import io
import json

import pytest

from marrow import config, hooks, storage

@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    dash = str(tmp_path / "dashboard.md")
    sub_folder = str(tmp_path / "db-pages")
    sub_state = str(tmp_path / "db_state")
    conn = storage.init_db(db)
    conn.execute("INSERT INTO tasks(category,title,status) "
                 "VALUES('study','GAMSAT plan','active')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "db_pages_path", lambda: sub_folder)
    monkeypatch.setattr(config, "db_pages_state_path", lambda: sub_state)
    # Legacy aliases kept synced so any caller still hitting the old name
    # (uncommitted other-window edits in daily.py) sees the same tmp paths.
    monkeypatch.setattr(config, "sub_pages_path", lambda: sub_folder)
    monkeypatch.setattr(config, "sub_pages_state_path", lambda: sub_state)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db, dash, tmp_path


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _pretool(monkeypatch, tool_name, tool_input, sid="s1", cwd=None):
    payload = {"session_id": sid, "tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        payload["cwd"] = cwd
    _stdin(monkeypatch, payload)
    return hooks.main(["pretool_use"])


def _out(capsys):
    return json.loads(capsys.readouterr().out)


def _hook_out(capsys):
    """hookSpecificOutput dict; empty stdout (fully silent) -> {}."""
    raw = capsys.readouterr().out.strip()
    if not raw:
        return {}
    return json.loads(raw).get("hookSpecificOutput", {})


@pytest.fixture(autouse=True)
def _no_real_git(monkeypatch):
    """Guard enrichment shells out to read-only git; keep the suite hermetic
    by stubbing the single boundary. Individual tests re-patch with a map."""
    monkeypatch.setattr(hooks.git_guard, "_git_read", lambda *a, **kw: None)


def _seed_session(db, sid, channel, cwd, created, last_active, ended=None):
    import sqlite3 as _s
    conn = _s.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO sessions(sid, model, channel, cwd, created_at,"
        " last_active, ended_at) VALUES(?,?,?,?,?,?,?)",
        (sid, "opus", channel, cwd, created, last_active, ended))
    conn.commit()
    conn.close()


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
