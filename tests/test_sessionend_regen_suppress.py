from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from marrow import config, hooks, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db, tmp_path


def _stdin(monkeypatch, sid: str, tpath):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({
            "session_id": sid,
            "cwd": str(tpath.parent),
            "transcript_path": str(tpath),
        })),
    )


def test_session_end_regen_suppress_consumes_flag(env, monkeypatch, tmp_path):
    _db, data_dir = env
    sid = "sid-regen"
    tpath = tmp_path / "fake.jsonl"
    tpath.write_text("")
    suppress = data_dir / f".regen_suppress_{sid}"
    suppress.touch()
    _stdin(monkeypatch, sid, tpath)

    with patch.object(hooks.transcript, "is_headless", return_value=False), \
         patch.object(hooks.transcript, "clean") as mclean, \
         patch.object(hooks.repo, "archive_events") as march, \
         patch.object(hooks, "_is_worktree_session") as mworktree:
        rc = hooks.session_end()

    assert rc == 0
    assert not suppress.exists()
    mclean.assert_not_called()
    march.assert_not_called()
    mworktree.assert_not_called()


def test_session_end_without_regen_suppress_proceeds(env, monkeypatch, tmp_path):
    sid = "sid-normal"
    tpath = tmp_path / "fake.jsonl"
    tpath.write_text("")
    _stdin(monkeypatch, sid, tpath)
    monkeypatch.delenv("MARROW_BRIDGE", raising=False)
    fake_rows = [{
        "session_id": sid,
        "role": "user",
        "content": "hi",
        "timestamp": "2026-06-02T00:00:00Z",
        "source_hash": "h-normal",
    }]

    with patch.object(hooks.transcript, "is_headless", return_value=False), \
         patch.object(hooks.transcript, "clean", return_value=fake_rows) as mclean, \
         patch.object(hooks.repo, "archive_events") as march, \
         patch.object(hooks, "_is_worktree_session", return_value=False), \
         patch.object(hooks, "popen_detach_lazy"):
        rc = hooks.session_end()

    assert rc == 0
    mclean.assert_called_once_with(
        str(tpath), skip_headless_check=False, channel="cli"
    )
    march.assert_called_once()


def test_session_end_wrong_regen_suppress_sid_does_not_suppress(
    env, monkeypatch, tmp_path,
):
    _db, data_dir = env
    sid = "sid-real"
    tpath = tmp_path / "fake.jsonl"
    tpath.write_text("")
    wrong = data_dir / ".regen_suppress_sid-other"
    wrong.touch()
    _stdin(monkeypatch, sid, tpath)
    monkeypatch.delenv("MARROW_BRIDGE", raising=False)
    fake_rows = [{
        "session_id": sid,
        "role": "user",
        "content": "hi",
        "timestamp": "2026-06-02T00:00:00Z",
        "source_hash": "h-wrong",
    }]

    with patch.object(hooks.transcript, "is_headless", return_value=False), \
         patch.object(hooks.transcript, "clean", return_value=fake_rows) as mclean, \
         patch.object(hooks.repo, "archive_events") as march, \
         patch.object(hooks, "_is_worktree_session", return_value=False), \
         patch.object(hooks, "popen_detach_lazy"):
        rc = hooks.session_end()

    assert rc == 0
    assert wrong.exists()
    mclean.assert_called_once_with(
        str(tpath), skip_headless_check=False, channel="cli"
    )
    march.assert_called_once()
