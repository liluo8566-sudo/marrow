"""Tests for the per-turn Stop hook (marrow.hooks.stop / A1 ingest).

Contract: after each completed assistant turn the hook archives the newly
completed user+assistant pair (idempotent by source_hash), logs a ct_activity
row, and keeps a per-sid cursor for cheap tail reads. On rewind / bridge
rewrite / stale offset the parentUuid walk fails and the hook falls back to a
full-file live-chain rebuild purely to ingest the current pair + reset cursor.
Ghost rows ingested before a rewind stay in the DB (no retraction in v1).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from marrow import config, hooks, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    storage.init_db(db).close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db, tmp_path


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _u(uuid, parent, content, role="user"):
    t = "user" if role == "user" else "assistant"
    msg = ({"role": "user", "content": content} if role == "user"
           else {"role": "assistant", "model": "claude-opus-4-7",
                 "content": [{"type": "text", "text": content}]})
    return {"type": t, "sessionId": "s1", "timestamp": "2026-07-03T01:00:00Z",
            "uuid": uuid, "parentUuid": parent, "isSidechain": False,
            "message": msg}


def _write(path: Path, records) -> None:
    path.write_text("\n".join(json.dumps(o) for o in records), encoding="utf-8")


def _append(path: Path, records) -> None:
    with path.open("a", encoding="utf-8") as f:
        for o in records:
            f.write("\n" + json.dumps(o))


def _events(db):
    conn = storage.connect(db)
    try:
        return [r["content"] for r in conn.execute(
            "SELECT content FROM events ORDER BY id").fetchall()]
    finally:
        conn.close()


def _activity_count(db):
    conn = storage.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) c FROM ct_activity").fetchone()["c"]
    finally:
        conn.close()


# ── first ingest + pair extraction ───────────────────────────────────────────

def test_first_stop_ingests_pair_and_activity(env, monkeypatch, tmp_path):
    db, _ = env
    jl = tmp_path / "s.jsonl"
    _write(jl, [_u("u1", None, "hello"),
                _u("a1", "u1", "hi there", role="assistant")])
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl),
                         "cwd": str(tmp_path)})
    assert hooks.main(["stop"]) == 0
    assert _events(db) == ["hello", "hi there"]
    assert _activity_count(db) == 1
    # cursor written
    cur = json.loads((tmp_path / "state" / "ct_cursor" / "s1.json").read_text())
    assert cur["last_uuid"] == "a1" and cur["offset"] > 0


def test_drops_noise_keeps_text(env, monkeypatch, tmp_path):
    db, _ = env
    jl = tmp_path / "s.jsonl"
    _write(jl, [
        _u("u1", None, "real q"),
        {"type": "assistant", "sessionId": "s1", "timestamp": "t",
         "uuid": "a1", "parentUuid": "u1", "isSidechain": False,
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "thinking", "thinking": "hmm"},
                                 {"type": "tool_use", "name": "Bash", "input": {}},
                                 {"type": "text", "text": "real a"}]}},
    ])
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl),
                         "cwd": str(tmp_path)})
    assert hooks.main(["stop"]) == 0
    assert _events(db) == ["real q", "real a"]


# ── incremental tail read ─────────────────────────────────────────────────────

def test_incremental_second_turn_only_new(env, monkeypatch, tmp_path):
    db, _ = env
    jl = tmp_path / "s.jsonl"
    _write(jl, [_u("u1", None, "hello"),
                _u("a1", "u1", "hi", role="assistant")])
    payload = {"session_id": "s1", "transcript_path": str(jl),
               "cwd": str(tmp_path)}
    _stdin(monkeypatch, payload)
    hooks.main(["stop"])
    # next turn appended
    _append(jl, [_u("u2", "a1", "more?"),
                 _u("a2", "u2", "sure", role="assistant")])
    _stdin(monkeypatch, payload)
    hooks.main(["stop"])
    assert _events(db) == ["hello", "hi", "more?", "sure"]
    assert _activity_count(db) == 2


def test_idempotent_rerun_no_dup(env, monkeypatch, tmp_path):
    db, _ = env
    jl = tmp_path / "s.jsonl"
    _write(jl, [_u("u1", None, "hello"),
                _u("a1", "u1", "hi", role="assistant")])
    payload = {"session_id": "s1", "transcript_path": str(jl),
               "cwd": str(tmp_path)}
    _stdin(monkeypatch, payload)
    hooks.main(["stop"])
    _stdin(monkeypatch, payload)
    hooks.main(["stop"])  # no new bytes
    assert _events(db) == ["hello", "hi"]


# ── rewind: full-file live-chain rebuild, ghosts retained ────────────────────

def test_rewind_full_rebuild_ingests_live_ghost_retained(env, monkeypatch, tmp_path):
    db, _ = env
    jl = tmp_path / "s.jsonl"
    _write(jl, [_u("u1", None, "hi"),
                _u("a1", "u1", "hello", role="assistant"),
                _u("u2", "a1", "GHOST-Q"),
                _u("a2", "u2", "GHOST-A", role="assistant")])
    payload = {"session_id": "s1", "transcript_path": str(jl),
               "cwd": str(tmp_path)}
    _stdin(monkeypatch, payload)
    hooks.main(["stop"])
    assert _events(db) == ["hi", "hello", "GHOST-Q", "GHOST-A"]
    # user /rewind to a1, retypes -> new branch u3/a3 (parent a1); u2/a2 rewound
    _append(jl, [_u("u3", "a1", "kept-q"),
                 _u("a3", "u3", "kept-a", role="assistant")])
    _stdin(monkeypatch, payload)
    hooks.main(["stop"])
    ev = _events(db)
    # live pair ingested
    assert "kept-q" in ev and "kept-a" in ev
    # ghost rows from before the rewind stay (accepted v1 behaviour)
    assert "GHOST-Q" in ev and "GHOST-A" in ev
    assert ev == ["hi", "hello", "GHOST-Q", "GHOST-A", "kept-q", "kept-a"]
    # rerun: no dup
    _stdin(monkeypatch, payload)
    hooks.main(["stop"])
    assert _events(db) == ev


# ── bridge physical truncation / stale offset -> full rebuild ────────────────

def test_stale_offset_truncation_full_rebuild(env, monkeypatch, tmp_path):
    db, tmp = env
    jl = tmp_path / "s.jsonl"
    _write(jl, [_u("u1", None, "hi"),
                _u("a1", "u1", "hello", role="assistant")])
    # simulate a prior larger file: cursor offset beyond current size, uuid gone
    cur_dir = tmp / "state" / "ct_cursor"
    cur_dir.mkdir(parents=True, exist_ok=True)
    (cur_dir / "s1.json").write_text(json.dumps(
        {"last_uuid": "GONE", "offset": 999999}))
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl),
                         "cwd": str(tmp_path)})
    hooks.main(["stop"])
    # offset > size -> full rebuild path -> live rows ingested
    assert _events(db) == ["hi", "hello"]


# ── env + isolation guards ────────────────────────────────────────────────────

def test_marrow_cortex_skips(env, monkeypatch, tmp_path):
    db, tmp = env
    jl = tmp_path / "s.jsonl"
    _write(jl, [_u("u1", None, "hello"),
                _u("a1", "u1", "hi", role="assistant")])
    monkeypatch.setenv("MARROW_CORTEX", "1")
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl),
                         "cwd": str(tmp_path)})
    assert hooks.main(["stop"]) == 0
    assert _events(db) == []
    conn = storage.connect(db)
    try:
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ct_activity'"
        ).fetchone()
    finally:
        conn.close()
    assert has is None  # table never created — nothing written
    assert not (tmp / "state" / "ct_cursor" / "s1.json").exists()


def test_subagent_transcript_skipped(env, monkeypatch, tmp_path):
    db, _ = env
    jl = tmp_path / "tasks" / "sub.jsonl"
    jl.parent.mkdir(parents=True, exist_ok=True)
    _write(jl, [_u("u1", None, "sub work")])
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl),
                         "cwd": str(tmp_path)})
    assert hooks.main(["stop"]) == 0
    assert _events(db) == []


def test_bridge_channel_recorded(env, monkeypatch, tmp_path):
    db, _ = env
    jl = tmp_path / "s.jsonl"
    _write(jl, [_u("u1", None, "hey"),
                _u("a1", "u1", "yo", role="assistant")])
    monkeypatch.setenv("MARROW_BRIDGE", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "wx")
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl),
                         "cwd": str(tmp_path)})
    hooks.main(["stop"])
    conn = storage.connect(db)
    try:
        chans = {r["channel"] for r in conn.execute(
            "SELECT channel FROM events").fetchall()}
        act = conn.execute("SELECT channel FROM ct_activity").fetchone()["channel"]
    finally:
        conn.close()
    assert chans == {"wx"} and act == "wx"


# ── e2e: invoke the hook as a subprocess exactly as CC does ──────────────────

def test_e2e_subprocess_ingest_and_no_dup(tmp_path):
    """Drive `python -m marrow.hooks stop` as a real subprocess against an
    isolated DB (HOME override -> config.toml points at a tmp db copy). Never
    touches the live ~/.config/marrow DB."""
    repo_root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    cfg_dir = home / ".config" / "marrow"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "e2e.db"
    storage.init_db(str(db)).close()
    (cfg_dir / "config.toml").write_text(
        f'[paths]\ndb = "{db}"\n', encoding="utf-8")

    jl = tmp_path / "s.jsonl"
    _write(jl, [_u("u1", None, "e2e hello"),
                _u("a1", "u1", "e2e hi", role="assistant")])
    envelope = json.dumps({"session_id": "s1", "transcript_path": str(jl),
                           "cwd": str(tmp_path)})
    run_env = dict(os.environ)
    run_env["HOME"] = str(home)

    def _run():
        return subprocess.run(
            [sys.executable, "-m", "marrow.hooks", "stop"],
            input=envelope, capture_output=True, text=True,
            cwd=str(repo_root), env=run_env, timeout=60)

    r = _run()
    assert r.returncode == 0, r.stderr
    assert _events(str(db)) == ["e2e hello", "e2e hi"]
    # rerun -> no dup
    r = _run()
    assert r.returncode == 0, r.stderr
    assert _events(str(db)) == ["e2e hello", "e2e hi"]
