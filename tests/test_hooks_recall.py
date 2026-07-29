"""user_prompt_submit — recall fusion injection."""
from __future__ import annotations

import json


from marrow import config, hooks, storage
from _hooks_shared import (  # noqa: F401 — fixtures resolved by name
    _hook_out,
    _iso,
    _no_real_git,
    _out,
    _pretool,
    _seed_session,
    _stdin,
    env,
)

# ── user_prompt_submit tests (wired to recall.recall_fusion) ─────────────────

def test_user_prompt_submit_explicit_disable(env, monkeypatch, capsys):
    """Explicit recall.vector = false => no-op, no output."""
    base_cfg = config.load()
    base_cfg.setdefault("recall", {})["vector"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    _stdin(monkeypatch, {"prompt": "hello", "session_id": "s1"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_user_prompt_submit_exclude_cwds_match_noop(env, monkeypatch, capsys):
    """[recall].exclude_cwds (C3 groundwork, HANDOVER queue item 2): session
    cwd starting with a listed prefix skips recall injection entirely."""
    base_cfg = config.load()
    base_cfg.setdefault("recall", {})["vector"] = True
    base_cfg["recall"]["exclude_cwds"] = ["/Users/Gabrielle/private-project"]
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    _stdin(monkeypatch, {"prompt": "hello", "session_id": "s1",
                         "cwd": "/Users/Gabrielle/private-project/sub"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_user_prompt_submit_exclude_cwds_no_match_proceeds(env, monkeypatch, capsys):
    """A cwd not matching any exclude_cwds prefix is unaffected (falls
    through to the normal gate/config checks below, not a forced hit)."""
    base_cfg = config.load()
    base_cfg.setdefault("recall", {})["vector"] = False  # isolate this gate
    base_cfg["recall"]["exclude_cwds"] = ["/Users/Gabrielle/private-project"]
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    _stdin(monkeypatch, {"prompt": "hello", "session_id": "s1",
                         "cwd": "/Users/Gabrielle/CC-Lab/marrow"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    # vector=false still no-ops downstream, but for a DIFFERENT reason —
    # confirms the exclude_cwds branch didn't consume it (no output either way).
    assert capsys.readouterr().out == ""


def _force_vector_on(monkeypatch, min_score: float = 0.30):
    base_cfg = config.load()
    base_cfg.setdefault("recall", {})["vector"] = True
    # Lower min_score so FTS-only event hits (vec=0, bm25+recency ~0.35-0.39)
    # clear the gate in tests that have no embedder loaded.
    base_cfg["recall"]["min_score"] = min_score
    monkeypatch.setattr(config, "load", lambda: base_cfg)


def test_user_prompt_submit_no_hits_noop(env, monkeypatch, capsys):
    """vector=true + no matching events => no additionalContext written."""
    _force_vector_on(monkeypatch)
    _stdin(monkeypatch, {"prompt": "hello", "session_id": "s1"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_user_prompt_submit_empty_prompt_noop(env, monkeypatch, capsys):
    """Empty prompt with vector=true => graceful no-op."""
    _force_vector_on(monkeypatch)
    _stdin(monkeypatch, {"prompt": "", "session_id": "s1"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_user_prompt_submit_emits_recall_block(env, monkeypatch, capsys):
    """vector=true + matching FTS event => ## Recall block in additionalContext."""
    db, _, _ = env
    conn = storage.connect(db)
    conn.execute(
        "INSERT INTO events(session_id,timestamp,role,content) "
        "VALUES('s9','2026-05-20T10:00:00Z','user','build phase 1 plan')")
    conn.commit()
    conn.close()
    _force_vector_on(monkeypatch)
    _stdin(monkeypatch, {"prompt": "phase 1 plan", "session_id": "s1"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out, "expected stdout JSON with additionalContext"
    data = json.loads(out)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "## Recall" in ctx
    assert "phase 1 plan" in ctx
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_user_prompt_submit_marrow_cortex_full_parity(env, monkeypatch, capsys):
    """B3m (07-08): cortex user_prompt_submit gets title/model backfill +
    touch like any other session (full memory parity, no recall short-circuit)."""
    db, _, _ = env
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    conn = storage.connect(db)
    conn.execute("INSERT INTO sessions (sid) VALUES ('cortex-sid-3')")
    conn.commit()
    conn.close()
    _stdin(monkeypatch, {"session_id": "cortex-sid-3", "prompt": "what should I do now?"})
    rc = hooks.main(["user_prompt_submit"])
    assert rc == 0
    conn = storage.connect(db)
    try:
        sess = conn.execute(
            "SELECT last_active FROM sessions WHERE sid='cortex-sid-3'"
        ).fetchone()
    finally:
        conn.close()
    assert sess is not None and sess["last_active"]
