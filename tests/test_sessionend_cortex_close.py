"""session_end cortex-close branch (Fix E): when the cortex window really ends
(non-'clear' reason) while a wake is open, session_end fires a proxy lie_down so
the wake ends at once instead of waiting for the 20-min fallback. /clear (which
also fires SessionEnd but leaves the window alive) and non-cortex sessions must
never trigger it.
"""
from __future__ import annotations

import io
import json

import pytest

from marrow import config, hooks, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # Keep session_end from doing real transcript / archive work.
    monkeypatch.setattr(hooks.transcript, "is_headless", lambda *a, **k: True)
    return db, tmp_path


def _stdin(monkeypatch, tpath, reason=None):
    payload = {
        "session_id": "sid-x",
        "cwd": str(tpath.parent),
        "transcript_path": str(tpath),
    }
    if reason is not None:
        payload["reason"] = reason
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _run(monkeypatch, tpath, reason, cortex):
    _stdin(monkeypatch, tpath, reason)
    calls = []
    monkeypatch.setattr(hooks.cortex_bridge, "is_cortex_session", lambda: cortex)
    monkeypatch.setattr(hooks.cortex_bridge, "cortex_window_closed",
                        lambda tp: calls.append(tp))
    hooks.session_end()
    return calls


def test_reason_clear_no_proxy(env, monkeypatch, tmp_path):
    tpath = tmp_path / "fake.jsonl"
    tpath.write_text("")
    assert _run(monkeypatch, tpath, "clear", cortex=True) == []


def test_reason_exit_fires_proxy(env, monkeypatch, tmp_path):
    tpath = tmp_path / "fake.jsonl"
    tpath.write_text("")
    calls = _run(monkeypatch, tpath, "exit", cortex=True)
    assert calls == [str(tpath)]


def test_reason_missing_fires_proxy(env, monkeypatch, tmp_path):
    tpath = tmp_path / "fake.jsonl"
    tpath.write_text("")
    # Missing reason -> treat as window ending.
    calls = _run(monkeypatch, tpath, None, cortex=True)
    assert calls == [str(tpath)]


def test_non_cortex_session_untouched(env, monkeypatch, tmp_path):
    tpath = tmp_path / "fake.jsonl"
    tpath.write_text("")
    assert _run(monkeypatch, tpath, "exit", cortex=False) == []
