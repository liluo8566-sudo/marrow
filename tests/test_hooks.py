"""Integration tests for marrow/hooks.py — thin CC hook entrypoints.

Hooks read paths from config; tests point config at a tmp db via
monkeypatch and drive main() with stdin JSON like CC does.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

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


def test_session_start_emits_additional_context(env, monkeypatch, capsys):
    _stdin(monkeypatch, {"session_id": "s1"})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert isinstance(ctx, str)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_session_end_does_not_write_db_pages(env, monkeypatch, tmp_path):
    """SessionEnd MUST NOT touch db-pages — those are owned by daily.py.
    Re-rendering milestone.md every session was the root cause of the
    `Milestone candidate` regrow-after-delete bug (pinned=0 leak into the
    subpage). Dashboard top is still rewritten; db-pages folder is left
    untouched until the next 07:00 daily routine."""
    db, _, _ = env
    conn = storage.connect(db)
    conn.execute("INSERT INTO milestones(scope,date,title,pinned) "
                 "VALUES('me','2026-01-17','Stellan birthday',1)")
    conn.commit()
    conn.close()
    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps(
        {"type": "user", "sessionId": "s1",
         "timestamp": "2026-05-17T01:00:00Z",
         "message": {"role": "user", "content": "ping"}}))
    _stdin(monkeypatch, {"session_id": "s1", "transcript_path": str(jl)})
    assert hooks.main(["session_end"]) == 0
    from pathlib import Path
    sub = Path(tmp_path / "db-pages" / "milestone.md")
    assert not sub.exists(), "session_end must not write milestone.md"


def test_session_end_no_transcript_is_safe(env, monkeypatch):
    _stdin(monkeypatch, {"session_id": "s1"})
    assert hooks.main(["session_end"]) == 0


def test_unknown_event_usage_error(env, monkeypatch):
    _stdin(monkeypatch, {})
    assert hooks.main(["bogus"]) == 2


# ── affect backdrop tests ─────────────────────────────────────────────────────

def _insert_affect(conn, date: str, ep: int, valence: float, arousal: float,
                   importance: int = 5, label: str | None = None,
                   source: str | None = None, description: str | None = None):
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label, "
        "description, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (date, ep, valence, arousal, importance, label, description, source),
    )
    conn.commit()


def test_affect_backdrop_empty_renders_placeholder(env, monkeypatch, capsys):
    """No data => Timeline block renders with _none_ placeholder."""
    _stdin(monkeypatch, {})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Timeline" in ctx
    assert "_none_" in ctx


def test_affect_backdrop_anchors_after_6am_rollover(env, monkeypatch, capsys):
    """Past 6AM: recent session digest appears in Timeline 24h film-strip."""
    db, _, _ = env
    conn = storage.connect(db)
    ts_recent = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO session_digests (sid, date, ts, text, kind, life_lines)"
        " VALUES ('sid-test', ?, ?, 'body', 'casual', '昨晚聊了很多')",
        (ts_recent[:10], ts_recent),
    )
    conn.commit()
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "## Timeline" in ctx
    assert "昨晚聊了很多" in ctx


def test_session_start_zone_caps_keep_output_bounded(env, monkeypatch, capsys):
    """Zone-level caps keep SessionStart output under hook stdout limit."""
    db, _, _ = env
    conn = storage.connect(db)
    today = datetime.now(timezone.utc).date()
    for i in range(50):
        conn.execute("INSERT INTO tasks(category,title,status) VALUES('work',?,?)",
                     (f"Task {i} " + "x" * 100, "active"))
    for i in range(20):
        conn.execute("INSERT INTO alerts(severity,type,message) VALUES('warn','test',?)",
                     ("Alert " + "y" * 200,))
    for i in range(10):
        _insert_affect(conn, today.isoformat(), i + 1, 0.5, 0.5,
                       label="Z" * 50)
    conn.commit()
    conn.close()

    _stdin(monkeypatch, {})
    hooks.main(["session_start"])
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= 10000


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


# ── lifecycle marker tests ────────────────────────────────────────────────────

def test_session_start_writes_lifecycle_marker(env, monkeypatch, capsys):
    """session_start with a session_id -> audit_log has lifecycle:start row."""
    db, _, _ = env
    _stdin(monkeypatch, {"session_id": "test-lc-start"})
    rc = hooks.main(["session_start"])
    assert rc == 0
    # Consume stdout to avoid pytest capsys noise.
    capsys.readouterr()
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE action='session_lifecycle:start' AND target_id='test-lc-start'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "lifecycle:start row not written"
    summary = row["summary"]
    assert "ppid=" in summary
    assert "source=cc" in summary
    assert "started_at=" in summary


def test_session_end_writes_lifecycle_end_marker(env, monkeypatch, tmp_path):
    """session_end -> audit_log has lifecycle:end row."""
    db, _, _ = env
    jl = tmp_path / "s.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "lc-end-sid",
        "timestamp": "2026-05-25T10:00:00Z",
        "message": {"role": "user", "content": "hello"},
    }))
    _stdin(monkeypatch, {"session_id": "lc-end-sid", "transcript_path": str(jl)})
    rc = hooks.main(["session_end"])
    assert rc == 0
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT 1 FROM audit_log"
            " WHERE action='session_lifecycle:end' AND target_id='lc-end-sid' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "lifecycle:end row not written"


def test_session_end_headless_writes_lifecycle_end_and_ended_at(
    env, monkeypatch, tmp_path
):
    """Headless SessionEnd exits early, but still leaves a terminal marker."""
    db, _, _ = env
    jl = tmp_path / "headless.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "headless-sid",
        "timestamp": "2026-05-25T10:00:00Z",
        "message": {
            "role": "user",
            "content": "Compress this file per the rules. Output ONLY",
        },
    }))
    conn = storage.connect(db)
    with conn:
        conn.execute("INSERT INTO sessions (sid) VALUES ('headless-sid')")
    conn.close()

    _stdin(monkeypatch, {
        "session_id": "headless-sid",
        "transcript_path": str(jl),
    })
    rc = hooks.main(["session_end"])

    assert rc == 0
    conn = storage.connect(db)
    try:
        sess = conn.execute(
            "SELECT ended_at FROM sessions WHERE sid='headless-sid'"
        ).fetchone()
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE action='session_lifecycle:end'"
            " AND target_id='headless-sid'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    finally:
        conn.close()
    assert sess is not None and sess["ended_at"]
    assert row is not None and row["summary"] == "headless=1"
    assert n == 0


def test_session_end_subagent_writes_lifecycle_end_and_ended_at(
    env, monkeypatch, tmp_path
):
    """Task-tool transcripts under /tasks/ skip archive/extract cleanly."""
    db, _, _ = env
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    jl = tasks_dir / "subagent.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "subagent-sid",
        "timestamp": "2026-05-25T10:00:00Z",
        "message": {"role": "user", "content": "normal subagent work"},
    }))
    conn = storage.connect(db)
    with conn:
        conn.execute("INSERT INTO sessions (sid) VALUES ('subagent-sid')")
    conn.close()

    _stdin(monkeypatch, {
        "session_id": "subagent-sid",
        "transcript_path": str(jl),
    })
    rc = hooks.main(["session_end"])

    assert rc == 0
    conn = storage.connect(db)
    try:
        sess = conn.execute(
            "SELECT ended_at FROM sessions WHERE sid='subagent-sid'"
        ).fetchone()
        row = conn.execute(
            "SELECT summary FROM audit_log"
            " WHERE action='session_lifecycle:end'"
            " AND target_id='subagent-sid'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    finally:
        conn.close()
    assert sess is not None and sess["ended_at"]
    assert row is not None and row["summary"] == "subagent=1"
    assert n == 0


def test_session_start_marrow_cortex_full_parity(env, monkeypatch, capsys):
    """B3m (07-08): cortex session_start gets the same lifecycle:start row,
    sessions row (channel=ct via MARROW_CHANNEL set alongside MARROW_CORTEX
    in llm.py) and injected context as any other session."""
    db, _, _ = env
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    _stdin(monkeypatch, {"session_id": "cortex-sid-1"})
    rc = hooks.main(["session_start"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["additionalContext"] != ""
    conn = storage.connect(db)
    try:
        lc = conn.execute(
            "SELECT 1 FROM audit_log"
            " WHERE action='session_lifecycle:start' AND target_id='cortex-sid-1'"
        ).fetchone()
        sess = conn.execute(
            "SELECT channel FROM sessions WHERE sid='cortex-sid-1'"
        ).fetchone()
    finally:
        conn.close()
    assert lc is not None
    assert sess is not None and sess["channel"] == "ct"


def test_session_end_marrow_cortex_full_parity(env, monkeypatch, tmp_path):
    """B3m (07-08): cortex session_end writes lifecycle:end like any other
    session. Events are archived per-turn by the Stop hook, not here."""
    db, _, _ = env
    jl = tmp_path / "cortex.jsonl"
    jl.write_text(json.dumps({
        "type": "user", "sessionId": "cortex-sid-2",
        "timestamp": "2026-07-03T10:00:00Z",
        "message": {"role": "user", "content": "cortex wake prompt"},
    }))
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    _stdin(monkeypatch, {"session_id": "cortex-sid-2", "transcript_path": str(jl)})
    rc = hooks.main(["session_end"])
    assert rc == 0
    conn = storage.connect(db)
    try:
        lc = conn.execute(
            "SELECT 1 FROM audit_log"
            " WHERE action='session_lifecycle:end' AND target_id='cortex-sid-2'"
        ).fetchone()
    finally:
        conn.close()
    assert lc is not None


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


# ── pretool_use backup guard — stateless, two tiers ──────────────────────────
# Silent (tmp/scratchpad/worktrees, same-command backup, git) / Reminder
# (additionalContext, fires EVERY call, no dedup) / Deny (permissionDecision
# "deny": recursive rm / db destruction with no same-command backup;
# downgrades to reminder when backup_guard_intercept=false). Git ops are owned
# by the git-revert ask guard and the force-push deny guard.

from pathlib import Path as _Path

_BG_MSG = "back up code/db OR archive docs"
_BG_DENY_MSG = "bulk deletion with no backup"
_MV_DST = str(_Path.home() / "CC-Lab" / "marrow" / "_bg_test_dst")


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


def test_backup_guard_rm_single_file_whitelisted_no_trigger(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "rm /tmp/foo.txt"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_git_status_no_trigger(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git status"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


# -- Silent: whitelist + same-command backup ----------------------------------

def test_backup_guard_rm_rf_tmp_silent(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf /tmp/foo"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_rm_rf_private_tmp_silent(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf /private/tmp/foo"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_scratchpad_silent(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "rm -rf /Users/x/project/scratchpad/old"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_recursive_rm_with_tar_backup_silent(env, monkeypatch, capsys):
    # Escape hatch: a backup action in the SAME command → fully silent allow,
    # no deny AND no reminder.
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "tar -czf /tmp/bak.tgz ~/projects/x && rm -rf ~/projects/x"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")
    assert _BG_DENY_MSG not in out.get("additionalContext", "")


def test_backup_guard_recursive_rm_with_cp_backup_silent(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "cp -r ~/projects/x /tmp/bak && rm -rf ~/projects/x"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_recursive_rm_backup_after_still_denies(env, monkeypatch, capsys):
    """Codex P2 fix: the escape hatch is segment-ORDERED. A backup keyword
    landing AFTER the destructive segment must not launder it — deny stands."""
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "rm -rf ~/projects/x && tar -czf /tmp/bak.tgz ~/projects/x"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert _BG_DENY_MSG in out["permissionDecisionReason"]


def test_backup_guard_recursive_rm_unrelated_cp_before_allows_order_only(
    env, monkeypatch, capsys
):
    """Position-only check, no backup-target matching (explicitly rejected —
    false-positive explosion vs minimal-interception). A `cp` of an UNRELATED
    path before the destructive segment still satisfies the escape hatch."""
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "cp ~/unrelated /tmp/whatever && rm -rf ~/projects/x"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out


# -- Reminder: fires EVERY call, no dedup -------------------------------------

def test_backup_guard_rm_single_file_reminds_every_call(env, monkeypatch, capsys):
    # Non-recursive rm on a non-whitelisted path → reminder, every call (no
    # once-per-session dedup).
    for _ in range(2):
        rc = _pretool(monkeypatch, "Bash", {"command": "rm ~/projects/note.txt"})
        assert rc == 0
        out = _out(capsys)["hookSpecificOutput"]
        assert "permissionDecision" not in out
        assert _BG_MSG in out["additionalContext"]


def test_backup_guard_bulk_mv_reminds(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": f"mv src/* {_MV_DST}"})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG in out["hookSpecificOutput"]["additionalContext"]


def test_backup_guard_delete_from_no_where_elsewhere_reminds(env, monkeypatch, capsys):
    # DELETE FROM without WHERE that is NOT a sqlite3 .db destruction → reminder.
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'psql -c "DELETE FROM events"'})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG in out["hookSpecificOutput"]["additionalContext"]


def test_backup_guard_event_clear_reminds(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "mcp__marrow__event_clear", {})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG in out["hookSpecificOutput"]["additionalContext"]


def test_backup_guard_mcp_action_delete_reminds(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "mcp__marrow__milestone", {"action": "delete"})
    assert rc == 0
    out = _out(capsys)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _BG_MSG in out["hookSpecificOutput"]["additionalContext"]


# -- Deny: recursive rm / db destruction, stateless ---------------------------

def test_backup_guard_recursive_rm_no_backup_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/projects/x"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert _BG_DENY_MSG in out["permissionDecisionReason"]
    assert "additionalContext" not in out


def test_backup_guard_recursive_rm_relative_no_backup_denies(env, monkeypatch, capsys):
    # Any non-whitelisted path (relative too) with recursive rm → deny.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -r build/output"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


# -- Relative path + cwd resolution (whitelist test only) ---------------------

def test_backup_guard_relative_rm_rf_cwd_in_scratchpad_silent(env, monkeypatch, capsys):
    # Bug fix: `cd <scratchpad> && rm -rf ask-demo` was denied even though cwd
    # resolves inside the whitelisted scratchpad zone.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ask-demo"},
                  cwd="/private/tmp/claude-501/proj/scratchpad")
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_relative_rm_rf_cwd_outside_whitelist_denies(env, monkeypatch, capsys):
    # cwd outside both whitelist AND trash zones → relative rm -rf still denies.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ask-demo"},
                  cwd="/Users/Gabrielle/projects")
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_relative_rm_single_file_cwd_whitelisted_silent(env, monkeypatch, capsys):
    # Non-recursive relative rm with a whitelisted cwd → fully silent (no
    # reminder either — the resolved path IS whitelisted).
    rc = _pretool(monkeypatch, "Bash", {"command": "rm ask-demo.txt"},
                  cwd="/private/tmp/claude-501/proj/scratchpad")
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_relative_rm_rf_missing_cwd_denies(env, monkeypatch, capsys):
    # No cwd provided at all (not just empty) + relative recursive rm →
    # unchanged today's behavior: treated as non-whitelisted, deny.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ask-demo"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_rm_db_file_denies(env, monkeypatch, capsys):
    # rm of a *.db file (even non-recursive) outside the whitelist → deny.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm ~/.config/marrow/marrow.db"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_rm_db_file_with_backup_allows(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "cp ~/x.db /tmp/x.db.backup && rm ~/x.db"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_sqlite_delete_no_where_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'sqlite3 t.db "DELETE FROM events"'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_sqlite_delete_no_where_with_backup_allows(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'cp t.db /tmp/t.db.bak && sqlite3 t.db "DELETE FROM events"'})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_sqlite_delete_backup_after_still_denies(env, monkeypatch, capsys):
    """Same ordering fix applied to db-destruction: cp AFTER the sqlite3
    destructive segment must not launder it."""
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'sqlite3 t.db "DELETE FROM events" && cp t.db /tmp/t.db.bak'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_drop_table_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'sqlite3 t.db "DROP TABLE tasks"'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_backup_guard_settings_json_edit_now_silent(env, monkeypatch, capsys):
    # Write/Edit is no longer guarded — a write requires a prior read, so it is
    # recoverable.
    rc = _pretool(monkeypatch, "Edit",
                  {"file_path": "/Users/x/.claude/settings.json", "old_string": "a",
                   "new_string": "b"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_intercept_off_downgrades_deny_to_reminder(env, monkeypatch, capsys):
    base_cfg = config.load()
    base_cfg.setdefault("hooks", {})["backup_guard_intercept"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/projects/x"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert _BG_MSG in out["additionalContext"]


# -- Config off / fail-open ---------------------------------------------------

def test_backup_guard_disabled_via_config(env, monkeypatch, capsys):
    base_cfg = config.load()
    base_cfg.setdefault("hooks", {})["backup_guard"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)

    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/projects/x"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_fail_open_malformed_input(env, monkeypatch, capsys):
    _stdin(monkeypatch, {"session_id": "s1", "tool_name": "Bash",
                         "tool_input": "not-a-dict"})
    rc = hooks.main(["pretool_use"])
    assert rc == 0


# ── rm → trash auto-rewrite ──────────────────────────────────────────────────
# Bash `rm` whose positional targets ALL fall under a trash_paths prefix is
# rewritten to `/usr/bin/trash <paths>` (recoverable) BEFORE the backup guard.
# Mixed / out-of-zone / wildcard targets fall through to the guard untouched.

_HOME = str(_Path.home())
_ICLOUD = _HOME + "/Library/Mobile Documents/com~apple~CloudDocs/Study/x.pdf"


def test_rm_to_trash_icloud_absolute(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'rm "~/Library/Mobile Documents/com~apple~CloudDocs/Study/x.pdf"'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["updatedInput"]["command"].startswith("/usr/bin/trash ")
    assert _ICLOUD in out["updatedInput"]["command"]
    assert "permissionDecision" not in out
    assert "rm auto-rewritten to trash" in out["additionalContext"]
    assert _BG_MSG not in out["additionalContext"]


def test_rm_to_trash_icloud_cwd_relative(env, monkeypatch, capsys):
    cwd = _HOME + "/Library/Mobile Documents/com~apple~CloudDocs/Study"
    rc = _pretool(monkeypatch, "Bash", {"command": "rm x.pdf"}, cwd=cwd)
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["updatedInput"]["command"].startswith("/usr/bin/trash ")
    assert _ICLOUD in out["updatedInput"]["command"]
    assert "permissionDecision" not in out


def test_rm_to_trash_rf_ny_flags_dropped(env, monkeypatch, capsys):
    # ~/Desktop/NY/ is covered by the wider ~/Desktop/ trash prefix.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/Desktop/NY/db-pages/old"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    cmd = out["updatedInput"]["command"]
    assert cmd.startswith("/usr/bin/trash ")
    assert "-rf" not in cmd and "-r" not in cmd
    assert (_HOME + "/Desktop/NY/db-pages/old") in cmd
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_rm_to_trash_desktop_non_ny_rewritten(env, monkeypatch, capsys):
    # Whole ~/Desktop is iCloud-synced personal-file territory, not just NY.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/Desktop/random-project/old"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    cmd = out["updatedInput"]["command"]
    assert cmd.startswith("/usr/bin/trash ")
    assert (_HOME + "/Desktop/random-project/old") in cmd
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_rm_to_trash_non_trash_repo_not_rewritten_reminds(env, monkeypatch, capsys):
    # Path outside trash_paths (git repo) → NOT rewritten; guard reminder fires.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm ~/projects/note.txt"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "updatedInput" not in out
    assert _BG_MSG in out["additionalContext"]


def test_rm_to_trash_non_trash_recursive_still_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf ~/projects/x"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "updatedInput" not in out
    assert out["permissionDecision"] == "deny"


def test_rm_to_trash_mixed_targets_not_rewritten(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "rm ~/Documents/a.txt ~/projects/b.txt"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "updatedInput" not in out
    assert _BG_MSG in out["additionalContext"]


def test_rm_to_trash_chained_only_rm_segment_rewritten(env, monkeypatch, capsys):
    import shlex
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "cd X && rm ~/Downloads/old.zip && echo done"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    expected = (
        "cd X && /usr/bin/trash "
        + shlex.quote(_HOME + "/Downloads/old.zip")
        + " && echo done"
    )
    assert out["updatedInput"]["command"] == expected
    assert "permissionDecision" not in out


def test_rm_to_trash_spaces_quoted_roundtrip(env, monkeypatch, capsys):
    import shlex
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'rm "~/Library/Mobile Documents/com~apple~CloudDocs/Study/x.pdf"'})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    toks = shlex.split(out["updatedInput"]["command"])
    assert toks[0] == "/usr/bin/trash"
    assert toks[1:] == [_ICLOUD]


def test_rm_to_trash_disabled_via_config(env, monkeypatch, capsys):
    base_cfg = config.load()
    base_cfg.setdefault("hooks", {})["rm_to_trash"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    rc = _pretool(monkeypatch, "Bash", {"command": "rm ~/Downloads/old.zip"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert "updatedInput" not in out


# -- git force-push guard — hard deny -----------------------------------------

def test_git_force_push_force_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git push --force origin main"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "force push" in out["permissionDecisionReason"]


def test_git_force_push_with_lease_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git push --force-with-lease origin main"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_git_force_push_short_flag_denies(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git push -f"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_git_force_push_in_worktree_still_denies(env, monkeypatch, capsys):
    # No worktree exemption for force push.
    _stdin(monkeypatch, {
        "session_id": "s1", "tool_name": "Bash",
        "cwd": "/Users/x/.claude/worktrees/agent-abc/marrow",
        "tool_input": {"command": "git push --force origin br"},
    })
    rc = hooks.main(["pretool_use"])
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_git_force_push_commit_message_no_false_positive(env, monkeypatch, capsys):
    # A commit whose -m message merely mentions force push must NOT be denied.
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'git commit -m "git push --force is dangerous"'})
    assert rc == 0
    out = _hook_out(capsys)
    assert out.get("permissionDecision") != "deny"


def test_git_plain_push_and_commit_silent(env, monkeypatch, capsys):
    for cmd in ("git push origin main", "git commit -m wip", "git merge feature"):
        rc = _pretool(monkeypatch, "Bash", {"command": cmd})
        assert rc == 0
        out = _hook_out(capsys)
        assert out.get("permissionDecision") is None, cmd


def test_git_force_push_disabled_via_config(env, monkeypatch, capsys):
    base_cfg = config.load()
    base_cfg.setdefault("hooks", {})["git_force_push_guard"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    rc = _pretool(monkeypatch, "Bash", {"command": "git push --force origin main"})
    assert rc == 0
    out = _hook_out(capsys)
    assert out.get("permissionDecision") != "deny"


# -- git revert-type authorship guard ("ask", enriched reason) ----------------

# Headline marker of the default git_revert_guard_message template.
_HEADLINE = "About to"  # shipped-default headline; live config may override


@pytest.fixture(autouse=True)
def _no_real_git(monkeypatch):
    """Guard enrichment shells out to read-only git; keep the suite hermetic
    by stubbing the single boundary. Individual tests re-patch with a map."""
    monkeypatch.setattr(hooks, "_git_read", lambda *a, **kw: None)


def test_git_revert_reset_hard_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git reset --hard HEAD~1"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert _HEADLINE in out["permissionDecisionReason"]


def test_git_revert_reset_hard_in_commit_message_no_match(env, monkeypatch, capsys):
    # A commit whose -m message merely contains "reset --hard" must NOT match.
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'git commit -m "reset --hard in message"'})
    assert rc == 0
    out = _hook_out(capsys)
    assert out.get("permissionDecision") != "ask"
    assert out.get("permissionDecision") != "deny"


def test_git_revert_checkout_file_discard_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git checkout -- marrow/hooks.py"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_revert_checkout_treeish_before_dashdash_asks(env, monkeypatch, capsys):
    for cmd in ("git checkout HEAD -- marrow/hooks.py",
                "git checkout deadbeef1 -- marrow/hooks.py"):
        rc = _pretool(monkeypatch, "Bash", {"command": cmd})
        assert rc == 0
        out = _out(capsys)["hookSpecificOutput"]
        assert out["permissionDecision"] == "ask", cmd


def test_git_revert_checkout_branch_switch_no_dashdash_not_held(
    env, monkeypatch, capsys
):
    for cmd in ("git checkout some-branch", "git checkout -b newbranch"):
        rc = _pretool(monkeypatch, "Bash", {"command": cmd})
        assert rc == 0
        out = _hook_out(capsys)
        assert out.get("permissionDecision") != "ask", cmd


def test_git_revert_restore_worktree_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git restore marrow/hooks.py"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_revert_restore_staged_only_is_safe(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git restore --staged marrow/hooks.py"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out


def test_git_revert_clean_f_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git clean -fd"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_branch_cap_d_asks_for_authorship(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git branch -D old-feature"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_worktree_remove_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git worktree remove /tmp/wt"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert _HEADLINE in out["permissionDecisionReason"]


def test_git_worktree_remove_in_worktree_cwd_silent(env, monkeypatch, capsys):
    _stdin(monkeypatch, {
        "session_id": "s1", "tool_name": "Bash",
        "cwd": "/Users/x/.claude/worktrees/agent-abc/marrow",
        "tool_input": {"command": "git worktree remove /tmp/wt"},
    })
    rc = hooks.main(["pretool_use"])
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out


# -- per-segment evaluation ---------------------------------------------------

def test_git_revert_compound_restore_staged_then_unsafe_restore_asks(
    env, monkeypatch, capsys
):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git restore --staged a && git restore b"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_revert_restore_staged_alone_still_passes(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git restore --staged a"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out


def test_git_revert_compound_status_then_reset_hard_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git status && git reset --hard"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


def test_git_revert_normal_git_commands_pass(env, monkeypatch, capsys):
    for cmd in ("git status", "git log --oneline", "git diff HEAD",
                "git commit -m wip", "git push origin main"):
        rc = _pretool(monkeypatch, "Bash", {"command": cmd})
        assert rc == 0
        out = _hook_out(capsys)
        assert out.get("permissionDecision") != "ask", cmd


def test_git_revert_branch_cap_d_worktree_cwd_silent(env, monkeypatch, capsys):
    # branch -D whose cwd is a worktree = agent teardown → ask skipped
    # (worktree exemption). Git no longer routes through the backup deny gate,
    # so with nothing else destructive it is silent.
    _stdin(monkeypatch, {
        "session_id": "s1", "tool_name": "Bash",
        "cwd": "/Users/x/.claude/worktrees/agent-abc/marrow",
        "tool_input": {"command": "git branch -D agent-abc"},
    })
    rc = hooks.main(["pretool_use"])
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out


def test_git_revert_worktree_substring_compound_bypass_still_denies(
    env, monkeypatch, capsys
):
    # A compound command whose git-revert segment substring-matches the
    # worktree exemption must NOT let an unrelated recursive rm on a
    # non-whitelisted path ride through — the "" exempt result only skips the
    # ASK, never the backup deny.
    cmd = ("git checkout -- /Users/x/.claude/worktrees/agent-abc/f "
           "&& rm -rf ~/projects/y")
    rc = _pretool(monkeypatch, "Bash", {"command": cmd})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out.get("permissionDecision") == "deny"
    assert "permissionDecisionReason" in out


def test_git_revert_relative_worktree_path_in_cmd_silent(env, monkeypatch, capsys):
    # Relative worktree path in the command (no leading slash) must still hit
    # the worktree/agent-cleanup exemption — cwd itself is not a worktree.
    cmd = (
        'git merge --no-ff some-branch -m "x" '
        "&& git worktree remove .claude/worktrees/agent-foo "
        "&& git branch -d some-branch"
    )
    rc = _pretool(monkeypatch, "Bash", {"command": cmd})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out


def test_git_revert_disabled_via_config(env, monkeypatch, capsys):
    base_cfg = config.load()
    base_cfg.setdefault("hooks", {})["git_revert_guard"] = False
    monkeypatch.setattr(config, "load", lambda: base_cfg)
    rc = _pretool(monkeypatch, "Bash", {"command": "git reset --hard HEAD~1"})
    assert rc == 0
    out = _hook_out(capsys)
    assert out.get("permissionDecision") != "ask"


# -- revert-guard reason enrichment (Action / File / LOC / By) -----------------

def _fake_git(monkeypatch, table):
    """Route `_git_read(cwd, args)` by the args prefix. Unmatched → None."""
    def _read(cwd, args, timeout=3):
        key = " ".join(args)
        for prefix, out in table.items():
            if key.startswith(prefix):
                return out
        return None
    monkeypatch.setattr(hooks, "_git_read", _read)


def _reason(monkeypatch, cmd, cwd="/repo", sid="s1"):
    inp = {"session_id": sid, "tool_name": "Bash", "cwd": cwd,
           "tool_input": {"command": cmd}}
    return hooks._git_revert_guard(inp)


def test_reason_restore_file_and_loc(env, monkeypatch):
    _fake_git(monkeypatch, {
        "diff --numstat -- tests/test_wx_watch.py":
            "12\t35\ttests/test_wx_watch.py\n",
    })
    monkeypatch.setattr(hooks, "_max_mtime", lambda *a: None)
    out = _reason(monkeypatch, "git restore tests/test_wx_watch.py")
    assert out.splitlines()[0].startswith("⚠️ About to")
    assert "discard uncommitted changes" in out
    assert "Action: git restore" in out.splitlines()[1]
    assert "File: tests/test_wx_watch.py" in out
    assert "LOC:  +12 −35" in out


def test_reason_checkout_treeish_drops_dashdash_from_action(env, monkeypatch):
    _fake_git(monkeypatch, {"diff --numstat -- a.py": "1\t2\ta.py\n"})
    monkeypatch.setattr(hooks, "_max_mtime", lambda *a: None)
    out = _reason(monkeypatch, "git checkout HEAD~1 -- a.py")
    action = out.splitlines()[1]
    assert action == "Action: git checkout HEAD~1"
    assert "File: a.py" in out
    assert "LOC:  +1 −2" in out


def test_reason_reset_hard_counts_commits(env, monkeypatch):
    _fake_git(monkeypatch, {
        "diff --numstat HEAD": "5\t7\tm.py\n",
        "log --oneline HEAD~3..HEAD": "aaa x\nbbb y\nccc z\n",
    })
    monkeypatch.setattr(hooks, "_max_mtime", lambda *a: None)
    out = _reason(monkeypatch, "git reset --hard HEAD~3")
    assert "roll the working tree all the way back" in out
    assert "Action: git reset --hard HEAD~3" in out
    assert "LOC:  +5 −7 (3 commits)" in out


def test_reason_revert_uses_show_numstat(env, monkeypatch):
    _fake_git(monkeypatch, {"show --numstat --format= abc123": "3\t0\tf.py\n"})
    out = _reason(monkeypatch, "git revert --no-edit abc123")
    assert "add an inverse commit" in out
    assert "File: f.py" in out and "LOC:  +3 −0" in out


def test_reason_branch_d_uses_default_branch_range(env, monkeypatch):
    _fake_git(monkeypatch, {
        "symbolic-ref --short refs/remotes/origin/HEAD": "origin/main\n",
        "log --numstat --format= origin/main..feat": "9\t1\tx.py\n2\t0\ty.py\n",
    })
    out = _reason(monkeypatch, "git branch -D feat")
    assert "force-delete a branch" in out
    assert "Action: git branch -D feat" in out
    assert "File: x.py, y.py" in out and "LOC:  +11 −1" in out


def test_reason_stash_drop_uses_stash_show(env, monkeypatch):
    _fake_git(monkeypatch, {"stash show --numstat": "4\t4\ts.py\n"})
    out = _reason(monkeypatch, "git stash drop")
    assert "drop stashed changes" in out and "LOC:  +4 −4" in out


def test_reason_worktree_remove_counts_dirty(env, monkeypatch):
    _fake_git(monkeypatch, {"status --porcelain": " M a\n?? b\n"})
    out = _reason(monkeypatch, "git worktree remove /tmp/wt")
    assert "remove a worktree directory" in out
    assert "File: /tmp/wt (2 uncommitted)" in out
    assert "LOC:" not in out


def test_reason_clean_lists_would_remove(env, monkeypatch):
    _fake_git(monkeypatch, {
        "clean -nd": "Would remove a.txt\nWould remove b/\nWould remove c\n"
                     "Would remove d\n",
    })
    monkeypatch.setattr(hooks, "_max_mtime", lambda *a: None)
    out = _reason(monkeypatch, "git clean -fd")
    assert "delete untracked files" in out
    assert "File: a.txt, b/, c (+1)" in out


def test_reason_degrades_to_action_when_git_fails(env, monkeypatch):
    # autouse _no_real_git already returns None for every git read
    out = _reason(monkeypatch, "git reset --hard")
    assert out.splitlines() == ["⚠️ About to roll the working tree all the way back — confirm?",
                                "Action: git reset --hard"]


def test_reason_unclassifiable_uses_generic_label(env, monkeypatch):
    # Pattern matches but the git text is a quoted argument of another program
    # → no classification. Line 1 must still read whole, never "又要了".
    out = _reason(monkeypatch, 'python probe.py run "git reset --hard HEAD"')
    assert out == "⚠️ About to mess with your git state — confirm?"
    assert "又要了" not in out


def test_reason_never_empty_when_enrichment_raises(env, monkeypatch):
    monkeypatch.setattr(hooks, "_git_revert_reason",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    out = _reason(monkeypatch, "git reset --hard")
    # "" would read as worktree-exempt in the caller — must fall back instead
    assert out and out.strip()


def test_guard_still_fail_open_on_config_error(env, monkeypatch):
    monkeypatch.setattr(config, "load",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _reason(monkeypatch, "git reset --hard") is None


def test_worktree_exemption_skips_enrichment(env, monkeypatch):
    called = []
    monkeypatch.setattr(hooks, "_git_revert_reason",
                        lambda *a, **kw: called.append(1) or "x")
    out = _reason(monkeypatch, "git reset --hard",
                  cwd="/Users/x/.claude/worktrees/agent-abc/marrow")
    assert out == "" and called == []


# -- By: ownership ------------------------------------------------------------

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


def test_owner_current_session(env, monkeypatch):
    db, _, _ = env
    now = datetime.now(timezone.utc)
    _seed_session(db, "s1", "cli", "/repo", _iso(now - timedelta(hours=1)),
                  _iso(now))
    ts = (now - timedelta(minutes=5)).timestamp()
    assert hooks._git_revert_owner("s1", "/repo", ts).startswith("Current Session · ")


def test_owner_other_session_named(env, monkeypatch):
    db, _, _ = env
    now = datetime.now(timezone.utc)
    _seed_session(db, "s1", "cli", "/repo", _iso(now - timedelta(minutes=1)),
                  _iso(now))
    _seed_session(db, "9102aaaa-bbbb", "cli", "/repo/sub",
                  _iso(now - timedelta(hours=5)), _iso(now - timedelta(minutes=10)),
                  _iso(now - timedelta(minutes=10)))
    ts = (now - timedelta(hours=1)).timestamp()
    got = hooks._git_revert_owner("s1", "/repo", ts)
    assert got.startswith("⚠️ Other Session cli·9102 · ")


def test_owner_overlapping(env, monkeypatch):
    db, _, _ = env
    now = datetime.now(timezone.utc)
    _seed_session(db, "s1", "cli", "/repo", _iso(now - timedelta(hours=5)),
                  _iso(now))
    _seed_session(db, "4a86aaaa-bbbb", "ct", "/repo", _iso(now - timedelta(hours=5)),
                  _iso(now))
    ts = (now - timedelta(hours=1)).timestamp()
    assert hooks._git_revert_owner("s1", "/repo", ts) == (
        "⚠️ Overlapping with ct·4a86 · unclear")


def test_owner_unrelated_cwd_is_not_overlap(env, monkeypatch):
    db, _, _ = env
    now = datetime.now(timezone.utc)
    _seed_session(db, "s1", "cli", "/repo", _iso(now - timedelta(hours=5)),
                  _iso(now))
    _seed_session(db, "4a86aaaa-bbbb", "ct", "/elsewhere",
                  _iso(now - timedelta(hours=5)), _iso(now))
    ts = (now - timedelta(hours=1)).timestamp()
    assert hooks._git_revert_owner("s1", "/repo", ts).startswith("Current Session")


def test_owner_omitted_when_unknown(env, monkeypatch):
    assert hooks._git_revert_owner("", "/repo", 1.0) is None       # no sid
    assert hooks._git_revert_owner("s1", "/repo", None) is None    # no timestamp
    assert hooks._git_revert_owner("ghost", "/repo", 1.0) is None  # no row


def test_reason_includes_by_line(env, monkeypatch):
    db, _, _ = env
    now = datetime.now(timezone.utc)
    _seed_session(db, "s1", "cli", "/repo", _iso(now - timedelta(hours=1)),
                  _iso(now))
    _fake_git(monkeypatch, {"diff --numstat -- a.py": "1\t1\ta.py\n"})
    monkeypatch.setattr(hooks, "_max_mtime",
                        lambda *a: (now - timedelta(minutes=2)).timestamp())
    out = _reason(monkeypatch, "git restore a.py")
    assert out.splitlines()[-1].startswith("By:   Current Session · ")


# -- housekeep commit subject stamp -------------------------------------------

def _cats(**kw):
    base = {"deleted": [], "renamed": [], "added": [], "modified": []}
    base.update(kw)
    return base


def test_housekeep_subject_carries_session_tag(env):
    msg = hooks._build_housekeep_commit_msg(_cats(modified=["a.py"]), 6, "cli·ab3a")
    assert msg.splitlines()[0] == "auto: session-start housekeep (6 files) [cli·ab3a]"
    assert msg.splitlines()[2] == "modified: a.py"


def test_housekeep_subject_unchanged_without_tag(env):
    msg = hooks._build_housekeep_commit_msg(_cats(modified=["a.py"]), 6, None)
    assert msg.splitlines()[0] == "auto: session-start housekeep (6 files)"


def test_housekeep_subject_kind_override(env):
    msg = hooks._build_housekeep_commit_msg(
        _cats(modified=["a.md"]), 2, "cli·ab3a", "docs housekeep")
    assert msg.splitlines()[0] == "auto: docs housekeep (2 files) [cli·ab3a]"


# -- housekeep docs/stale split -----------------------------------------------

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


def _mk_repo(tmp_path):
    repo = tmp_path / "hk"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _porcelain(repo):
    return [ln for ln in _git(repo, "status", "--porcelain").stdout.splitlines()
            if ln.strip()]


def test_split_housekeep_dirty_buckets(env, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_housekeep_stale_hours", lambda: 2.0)
    monkeypatch.setattr(hooks, "_housekeep_docs_exts",
                        lambda: {".md", ".toml", ".json", ".txt"})
    repo = _mk_repo(tmp_path)
    (repo / "notes.md").write_text("fresh doc\n")
    (repo / "fresh.py").write_text("x = 1\n")
    (repo / "old.py").write_text("y = 2\n")
    old_ts = time.time() - 3 * 3600
    os.utime(repo / "old.py", (old_ts, old_ts))
    (repo / "seed.txt").unlink()

    docs, stale, fresh = hooks._split_housekeep_dirty(str(repo), _porcelain(repo))
    assert sorted(ln[3:].strip() for ln in docs) == ["notes.md", "seed.txt"]
    assert [ln[3:].strip() for ln in stale] == ["old.py"]
    assert [ln[3:].strip() for ln in fresh] == ["fresh.py"]


def test_split_treats_missing_mtime_as_stale(env, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_housekeep_stale_hours", lambda: 2.0)
    monkeypatch.setattr(hooks, "_housekeep_docs_exts", lambda: {".md"})
    repo = _mk_repo(tmp_path)
    (repo / "gone.py").write_text("z = 3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add gone")
    (repo / "gone.py").unlink()
    docs, stale, fresh = hooks._split_housekeep_dirty(str(repo), _porcelain(repo))
    assert (docs, [ln[3:].strip() for ln in stale], fresh) == ([], ["gone.py"], [])


def test_split_untracked_dir_judged_by_newest_file_inside(
        env, tmp_path, monkeypatch):
    """`?? dir/` — an old directory holding a just-written file stays fresh."""
    monkeypatch.setattr(hooks, "_housekeep_stale_hours", lambda: 2.0)
    monkeypatch.setattr(hooks, "_housekeep_docs_exts", lambda: {".md"})
    repo = _mk_repo(tmp_path)
    d = repo / "wip"
    (d / "deep").mkdir(parents=True)
    (d / "deep" / "new.py").write_text("x = 1\n")
    old_ts = time.time() - 9 * 3600
    for p in (d, d / "deep"):
        os.utime(p, (old_ts, old_ts))

    docs, stale, fresh = hooks._split_housekeep_dirty(str(repo), _porcelain(repo))
    assert (docs, stale) == ([], [])
    assert [ln[3:].strip() for ln in fresh] == ["wip/"]

    os.utime(d / "deep" / "new.py", (old_ts, old_ts))
    docs, stale, fresh = hooks._split_housekeep_dirty(str(repo), _porcelain(repo))
    assert [ln[3:].strip() for ln in stale] == ["wip/"] and fresh == []


def test_commit_housekeep_groups_two_commits_and_leaves_fresh(
        env, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_housekeep_stale_hours", lambda: 2.0)
    monkeypatch.setattr(hooks, "_housekeep_docs_exts", lambda: {".md"})
    repo = _mk_repo(tmp_path)
    (repo / "notes.md").write_text("doc\n")
    (repo / "fresh.py").write_text("x = 1\n")
    (repo / "old.py").write_text("y = 2\n")
    old_ts = time.time() - 5 * 3600
    os.utime(repo / "old.py", (old_ts, old_ts))

    out = hooks._commit_housekeep_groups(str(repo), _porcelain(repo),
                                         "cli·ab3a", "cwd")

    subjects = _git(repo, "log", "--format=%s", "-3").stdout.splitlines()
    assert subjects[1] == "auto: docs housekeep (1 files) [cli·ab3a]"
    assert subjects[0] == "auto: stale leftovers (1 files) [cli·ab3a]"
    # fresh.py untouched: still the only dirty entry
    assert [ln[3:].strip() for ln in _porcelain(repo)] == ["fresh.py"]
    assert any("skipped 1 fresh" in ln for ln in out)
    assert any(ln.startswith("cwd docs: committed 1 files") for ln in out)
    assert any(ln.startswith("cwd stale: committed 1 files") for ln in out)


def test_commit_housekeep_groups_docs_only_single_commit(
        env, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_housekeep_stale_hours", lambda: 2.0)
    monkeypatch.setattr(hooks, "_housekeep_docs_exts", lambda: {".md"})
    repo = _mk_repo(tmp_path)
    (repo / "a.md").write_text("a\n")
    (repo / "b.md").write_text("b\n")
    hooks._commit_housekeep_groups(str(repo), _porcelain(repo), None, "cwd")
    subjects = _git(repo, "log", "--format=%s", "-2").stdout.splitlines()
    assert subjects[0] == "auto: docs housekeep (2 files)"
    assert subjects[1] == "seed"
    assert _porcelain(repo) == []


def test_porcelain_paths_rename_and_quoting():
    assert hooks._porcelain_paths("R  old.py -> new.py") == ["old.py", "new.py"]
    assert hooks._porcelain_paths(' M "caf\\303\\251.md"') == ["café.md"]


def test_session_tag_resolution(env):
    db, _, _ = env
    now = datetime.now(timezone.utc)
    _seed_session(db, "ab3ac0de-1111", "cli", "/repo", _iso(now), _iso(now))
    _seed_session(db, "nochan-2222", "", "/repo", _iso(now), _iso(now))
    conn = storage.init_db(db)
    assert hooks._session_tag("ab3ac0de-1111", conn) == "cli·ab3a"
    assert hooks._session_tag("nochan-2222", conn) is None   # blank channel
    assert hooks._session_tag("missing", conn) is None       # no row
    assert hooks._session_tag(None, conn) is None
    conn.close()


# -- T8: no-`--` checkout classification + loss gate --------------------------

def _git_repo_state(monkeypatch, *, tracked=(), dirty=()):
    """Model the read-only git boundary as a tiny repo: `tracked` = paths in
    the index (disk presence irrelevant), `dirty` = paths carrying
    uncommitted work. Everything else answers None (unknown)."""
    def _read(cwd, args, timeout=3):
        if args[:1] == ["ls-files"]:
            want = args[args.index("--") + 1:] if "--" in args else list(tracked)
            return "".join(f"{p}\n" for p in tracked if p in want)
        if args[:2] == ["status", "--porcelain"]:
            want = args[args.index("--") + 1:] if "--" in args else list(dirty)
            return "".join(f" M {p}\n" for p in dirty if p in want)
        if args[:1] == ["diff"]:
            return ""
        return None
    monkeypatch.setattr(hooks, "_git_read", _read)


def test_t8_checkout_no_dashdash_modified_file_asks(env, monkeypatch, capsys):
    _git_repo_state(monkeypatch, tracked=["a.py"], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash", {"command": "git checkout a.py"},
                  cwd="/repo")
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert "File: a.py" in out["permissionDecisionReason"]


def test_t8_checkout_no_dashdash_clean_file_silent(env, monkeypatch, capsys):
    _git_repo_state(monkeypatch, tracked=["a.py"], dirty=[])
    rc = _pretool(monkeypatch, "Bash", {"command": "git checkout a.py"},
                  cwd="/repo")
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)


def test_t8_checkout_staged_only_change_asks(env, monkeypatch, capsys):
    # `git add a.py` then `git checkout HEAD -- a.py`: nothing unstaged, but
    # the staged work is still destroyed — porcelain reports it, so we ask.
    _git_repo_state(monkeypatch, tracked=["a.py"], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git checkout HEAD -- a.py"}, cwd="/repo")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_t8_checkout_rmd_tracked_file_asks(env, monkeypatch, capsys):
    # File deleted from disk but still in the index — no disk-presence bypass.
    _git_repo_state(monkeypatch, tracked=["gone.py"], dirty=["gone.py"])
    rc = _pretool(monkeypatch, "Bash", {"command": "git checkout gone.py"},
                  cwd="/repo")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_t8_checkout_dash_C_form_asks(env, monkeypatch, capsys):
    _git_repo_state(monkeypatch, tracked=["a.py"], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git -C /repo checkout a.py"}, cwd="/elsewhere")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_t8_checkout_global_flag_form_asks(env, monkeypatch, capsys):
    _git_repo_state(monkeypatch, tracked=["a.py"], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git --work-tree=/repo checkout a.py"},
                  cwd="/repo")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_t8_checkout_ambiguous_tracked_and_ref_asks(env, monkeypatch, capsys):
    # `main` is both a branch and a tracked path — ambiguous, so ask.
    _git_repo_state(monkeypatch, tracked=["main"], dirty=["main"])
    rc = _pretool(monkeypatch, "Bash", {"command": "git checkout main"},
                  cwd="/repo")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_t8_checkout_ref_only_and_new_branch_and_bare_silent(
    env, monkeypatch, capsys
):
    _git_repo_state(monkeypatch, tracked=["a.py"], dirty=["a.py"])
    for cmd in ("git checkout main", "git checkout -b feat",
                "git checkout -B feat", "git checkout --orphan feat",
                "git checkout"):
        rc = _pretool(monkeypatch, "Bash", {"command": cmd}, cwd="/repo")
        assert rc == 0
        assert "permissionDecision" not in _hook_out(capsys), cmd


def test_t8_checkout_dashdash_form_regression_asks(env, monkeypatch, capsys):
    _git_repo_state(monkeypatch, tracked=["a.py"], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash", {"command": "git checkout -- a.py"},
                  cwd="/repo")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_t8_checkout_dashdash_form_clean_is_silent(env, monkeypatch, capsys):
    # Decided: the legacy `--` form loses its clean-file popup on purpose.
    _git_repo_state(monkeypatch, tracked=["a.py"], dirty=[])
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git checkout HEAD -- a.py"}, cwd="/repo")
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)


def test_t8_restore_clean_target_is_silent(env, monkeypatch, capsys):
    _git_repo_state(monkeypatch, tracked=["a.py"], dirty=[])
    rc = _pretool(monkeypatch, "Bash", {"command": "git restore a.py"},
                  cwd="/repo")
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)


def test_t8_checkout_compound_caught_per_segment(env, monkeypatch, capsys):
    _git_repo_state(monkeypatch, tracked=["a.py"], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "cd /repo && git checkout a.py"}, cwd="/repo")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_t8_checkout_word_in_commit_message_no_match(env, monkeypatch, capsys):
    _git_repo_state(monkeypatch, tracked=["a.py"], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash",
                  {"command": 'git commit -m "git checkout a.py"'},
                  cwd="/repo")
    assert rc == 0
    assert _hook_out(capsys).get("permissionDecision") != "ask"


def test_t8_checkout_untracked_operand_silent(env, monkeypatch, capsys):
    # Not in the index at all → git would error anyway; nothing to lose.
    _git_repo_state(monkeypatch, tracked=[], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash", {"command": "git checkout a.py"},
                  cwd="/repo")
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)


# -- relative `-C` / `--work-tree` resolve against the tool cwd ---------------

def _git_repo_at(monkeypatch, repo, *, tracked=(), dirty=()):
    """Answer git queries ONLY for *repo*; any other cwd answers None, so a
    mis-resolved relative dir cannot accidentally look clean OR dirty."""
    seen = []

    def _read(cwd, args, timeout=3):
        seen.append(cwd)
        if cwd != repo:
            return None
        if args[:1] == ["ls-files"]:
            want = args[args.index("--") + 1:] if "--" in args else list(tracked)
            return "".join(f"{p}\n" for p in tracked if p in want)
        if args[:2] == ["status", "--porcelain"]:
            want = args[args.index("--") + 1:] if "--" in args else list(dirty)
            return "".join(f" M {p}\n" for p in dirty if p in want)
        if args[:1] == ["diff"]:
            return ""
        return None
    monkeypatch.setattr(hooks, "_git_read", _read)
    return seen


def test_relative_dash_C_resolves_against_tool_cwd(env, monkeypatch, capsys):
    seen = _git_repo_at(monkeypatch, "/repo/sub", tracked=["a.py"], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash", {"command": "git -C sub checkout a.py"},
                  cwd="/repo")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert seen and set(seen) == {"/repo/sub"}


def test_relative_work_tree_resolves_against_tool_cwd(env, monkeypatch, capsys):
    seen = _git_repo_at(monkeypatch, "/repo/sub", tracked=["a.py"], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git --work-tree=sub checkout a.py"}, cwd="/repo")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert seen and set(seen) == {"/repo/sub"}


def test_absolute_dash_C_unchanged(env, monkeypatch, capsys):
    seen = _git_repo_at(monkeypatch, "/other", tracked=["a.py"], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git -C /other checkout a.py"}, cwd="/repo")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert set(seen) == {"/other"}


def test_relative_dash_C_pointing_nowhere_fails_safe(env, monkeypatch, capsys):
    # Nothing answers (git would error on a non-repo too). Documented fall:
    # no-`--` form → no tracked operand → branch-switch shape → silent (the
    # command itself destroys nothing); `--` form names its targets outright,
    # so unknown status still holds → ask. Neither path raises.
    _git_repo_at(monkeypatch, "/repo/real", tracked=["a.py"], dirty=["a.py"])
    rc = _pretool(monkeypatch, "Bash", {"command": "git -C nope checkout a.py"},
                  cwd="/repo")
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)

    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git -C nope checkout -- a.py"}, cwd="/repo")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_git_repo_dir_resolution_unit(env):
    assert hooks._git_repo_dir("", "/repo") == ""
    assert hooks._git_repo_dir("/abs", "/repo") == "/abs"
    assert hooks._git_repo_dir("sub", "/repo") == "/repo/sub"
    assert hooks._git_repo_dir("../sib", "/repo/a") == "/repo/sib"
    assert hooks._git_repo_dir("sub", "") == "sub"          # no cwd -> as given
    assert hooks._git_repo_dir("~/x", "/repo").startswith("/")  # ~ expanded
