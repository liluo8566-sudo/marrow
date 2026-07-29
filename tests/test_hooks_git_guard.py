"""PreToolUse git guards: force-push deny, revert-type ask,
write ledger, reason enrichment, ownership, T8 checkout."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone


from marrow import config, hooks
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


def test_git_worktree_remove_forced_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git worktree remove -f /Users/x/wt"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert _HEADLINE in out["permissionDecisionReason"]


def test_git_worktree_remove_plain_silent(env, monkeypatch, capsys):
    # git refuses to remove a dirty worktree itself → nothing to confirm.
    rc = _pretool(monkeypatch, "Bash", {"command": "git worktree remove /Users/x/wt"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out


def test_git_worktree_remove_long_force_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git worktree remove --force /Users/x/wt"})
    assert rc == 0
    out = _out(capsys)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"


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


# -- isolation prefixes (ask tier) + agent branch teardown --------------------

def _hooks_cfg(monkeypatch, **kv):
    cfg = config.load()
    cfg.setdefault("hooks", {}).update(kv)
    monkeypatch.setattr(config, "load", lambda: cfg)
    return cfg


def test_isolation_prefix_cwd_tmp_claude_silent(env, monkeypatch, capsys):
    # cwd inside a /private/tmp/claude-* agent scratch zone → ask skipped.
    rc = _pretool(monkeypatch, "Bash", {"command": "git reset --hard HEAD~1"},
                  cwd="/private/tmp/claude-501/proj/wt")
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)


def test_isolation_prefix_cmd_tmp_claude_silent(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "cd /tmp/claude-501/wt && git reset --hard HEAD~1"},
                  cwd="/Users/x/proj")
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)


def test_isolation_prefix_non_isolated_path_still_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "cd /tmp/other/wt && git reset --hard HEAD~1"},
                  cwd="/Users/x/proj")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_isolation_prefixes_configurable(env, monkeypatch, capsys):
    _hooks_cfg(monkeypatch, isolation_prefixes=["/sandbox/"])
    rc = _pretool(monkeypatch, "Bash", {"command": "git reset --hard HEAD~1"},
                  cwd="/opt/sandbox/run")
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)
    # The built-in default no longer applies once overridden.
    rc = _pretool(monkeypatch, "Bash", {"command": "git reset --hard HEAD~1"},
                  cwd="/private/tmp/claude-501/wt")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_git_branch_cap_d_agent_branch_silent(env, monkeypatch, capsys):
    # Agent worktree teardown from the main repo cwd → no popup.
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git branch -D worktree-agent-a1c3d3b9b8eccb8f5"},
                  cwd="/Users/x/proj")
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)


def test_git_branch_cap_d_mixed_operands_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git branch -D worktree-agent-abc my-feature"},
                  cwd="/Users/x/proj")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_git_branch_cap_d_no_operand_asks(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git branch -D"},
                  cwd="/Users/x/proj")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_git_branch_lowercase_d_untouched(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash", {"command": "git branch -d my-feature"},
                  cwd="/Users/x/proj")
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)


def test_agent_branch_prefixes_configurable(env, monkeypatch, capsys):
    _hooks_cfg(monkeypatch, agent_branch_prefixes=["tmp/"])
    rc = _pretool(monkeypatch, "Bash", {"command": "git branch -D tmp/scratch"},
                  cwd="/Users/x/proj")
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "git branch -D worktree-agent-abc"},
                  cwd="/Users/x/proj")
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


# -- session write ledger (own drafts are silently revertable) ----------------

def _transcript(tmp_path, *file_paths, name="t.jsonl"):
    """Synthetic session transcript with one Edit tool_use per path."""
    p = tmp_path / name
    lines = []
    for fp in file_paths:
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": fp}},
            ]},
        }))
    p.write_text("\n".join(lines) + ("\n" if lines else ""))
    return p


def _pretool_t(monkeypatch, cmd, tpath, sid="s-wl", cwd="/repo"):
    _stdin(monkeypatch, {
        "session_id": sid, "tool_name": "Bash", "cwd": cwd,
        "transcript_path": str(tpath), "tool_input": {"command": cmd},
    })
    return hooks.main(["pretool_use"])


def test_write_ledger_own_file_dirty_silent(env, monkeypatch, capsys, tmp_path):
    # Dirty target (git says modified) but the session wrote it → no popup.
    monkeypatch.setattr(hooks.git_guard, "_git_worktree_dirty", lambda *a: True)
    f = tmp_path / "draft.py"
    f.write_text("x")
    t = _transcript(tmp_path, str(f))
    assert _pretool_t(monkeypatch, f"git restore {f}", t) == 0
    assert "permissionDecision" not in _hook_out(capsys)


def test_write_ledger_unknown_file_dirty_asks(env, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(hooks.git_guard, "_git_worktree_dirty", lambda *a: True)
    f = tmp_path / "draft.py"
    other = tmp_path / "theirs.py"
    f.write_text("x")
    other.write_text("y")
    t = _transcript(tmp_path, str(f))
    assert _pretool_t(monkeypatch, f"git restore {other}", t) == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_write_ledger_mixed_operands_ask(env, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(hooks.git_guard, "_git_worktree_dirty", lambda *a: True)
    mine, theirs = tmp_path / "mine.py", tmp_path / "theirs.py"
    mine.write_text("x")
    theirs.write_text("y")
    t = _transcript(tmp_path, str(mine))
    assert _pretool_t(monkeypatch, f"git restore {mine} {theirs}", t) == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_write_ledger_relative_operand_resolved_against_cwd(
    env, monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(hooks.git_guard, "_git_worktree_dirty", lambda *a: True)
    f = tmp_path / "draft.py"
    f.write_text("x")
    t = _transcript(tmp_path, str(f))
    assert _pretool_t(monkeypatch, "git restore draft.py", t,
                      cwd=str(tmp_path)) == 0
    assert "permissionDecision" not in _hook_out(capsys)


def test_write_ledger_unreadable_transcript_asks(env, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(hooks.git_guard, "_git_worktree_dirty", lambda *a: True)
    f = tmp_path / "draft.py"
    f.write_text("x")
    assert _pretool_t(monkeypatch, f"git restore {f}", tmp_path / "missing.jsonl") == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_write_ledger_is_incremental(env, monkeypatch, tmp_path):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    t = _transcript(tmp_path, str(a))
    first = hooks._session_write_set("s-inc", str(t))
    assert first == {os.path.realpath(str(a))}
    cache = json.loads((config.DATA_DIR / "state" / "write_ledger" /
                        "s-inc.json").read_text())
    assert cache["offset"] == t.stat().st_size
    with t.open("a") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Write",
                 "input": {"file_path": str(b)}},
            ]},
        }) + "\n")
    second = hooks._session_write_set("s-inc", str(t))
    assert second == {os.path.realpath(str(a)), os.path.realpath(str(b))}


def test_write_ledger_lazy_no_io_without_path_operands(
    env, monkeypatch, capsys, tmp_path
):
    # The ledger costs a transcript tail scan + a cache write — it may only run
    # when a checkout/restore segment with path operands needs it.
    calls = []
    monkeypatch.setattr(hooks.git_guard, "_session_write_set",
                        lambda *a: calls.append(a) or set())
    t = _transcript(tmp_path, str(tmp_path / "draft.py"))
    for cmd in ("ls -la", "pytest -q", "git status", "git reset --hard HEAD~1",
                "git branch -D worktree-agent-abc"):
        assert _pretool_t(monkeypatch, cmd, t) == 0
        capsys.readouterr()
    assert calls == []
    assert not (config.DATA_DIR / "state" / "write_ledger").exists()


def test_write_ledger_scanned_once_per_call(env, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(hooks.git_guard, "_git_worktree_dirty", lambda *a: True)
    real = hooks._session_write_set
    calls = []
    monkeypatch.setattr(hooks.git_guard, "_session_write_set",
                        lambda *a: calls.append(a) or real(*a))
    mine, theirs = tmp_path / "mine.py", tmp_path / "theirs.py"
    mine.write_text("x")
    theirs.write_text("y")
    t = _transcript(tmp_path, str(mine))
    # Two checkout/restore segments — the memo resolves the ledger only once.
    assert _pretool_t(monkeypatch, f"git restore {mine} && git restore {theirs}",
                      t) == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert len(calls) == 1


def test_write_ledger_does_not_exempt_reset_hard(env, monkeypatch, capsys, tmp_path):
    # Ops with no path operand are untouched by the ledger.
    f = tmp_path / "draft.py"
    f.write_text("x")
    t = _transcript(tmp_path, str(f))
    assert _pretool_t(monkeypatch, "git reset --hard HEAD~1", t) == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "ask"


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
    monkeypatch.setattr(hooks.git_guard, "_git_read", _read)


def _reason(monkeypatch, cmd, cwd="/repo", sid="s1"):
    inp = {"session_id": sid, "tool_name": "Bash", "cwd": cwd,
           "tool_input": {"command": cmd}}
    return hooks._git_revert_guard(inp)


def test_reason_restore_file_and_loc(env, monkeypatch):
    _fake_git(monkeypatch, {
        "diff --numstat -- tests/test_wx_watch.py":
            "12\t35\ttests/test_wx_watch.py\n",
    })
    monkeypatch.setattr(hooks.git_guard, "_max_mtime", lambda *a: None)
    out = _reason(monkeypatch, "git restore tests/test_wx_watch.py")
    assert out.splitlines()[0].startswith("⚠️ About to")
    assert "discard uncommitted changes" in out
    assert "Action: git restore" in out.splitlines()[1]
    assert "File: tests/test_wx_watch.py" in out
    assert "LOC:  +12 −35" in out


def test_reason_checkout_treeish_drops_dashdash_from_action(env, monkeypatch):
    _fake_git(monkeypatch, {"diff --numstat -- a.py": "1\t2\ta.py\n"})
    monkeypatch.setattr(hooks.git_guard, "_max_mtime", lambda *a: None)
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
    monkeypatch.setattr(hooks.git_guard, "_max_mtime", lambda *a: None)
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
    out = _reason(monkeypatch, "git worktree remove -f /tmp/wt")
    assert "remove a worktree directory" in out
    assert "File: /tmp/wt (2 uncommitted)" in out
    assert "LOC:" not in out


def test_reason_clean_lists_would_remove(env, monkeypatch):
    _fake_git(monkeypatch, {
        "clean -nd": "Would remove a.txt\nWould remove b/\nWould remove c\n"
                     "Would remove d\n",
    })
    monkeypatch.setattr(hooks.git_guard, "_max_mtime", lambda *a: None)
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
    monkeypatch.setattr(hooks.git_guard, "_git_revert_reason",
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
    monkeypatch.setattr(hooks.git_guard, "_git_revert_reason",
                        lambda *a, **kw: called.append(1) or "x")
    out = _reason(monkeypatch, "git reset --hard",
                  cwd="/Users/x/.claude/worktrees/agent-abc/marrow")
    assert out == "" and called == []


# -- By: ownership ------------------------------------------------------------




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
    monkeypatch.setattr(hooks.git_guard, "_max_mtime",
                        lambda *a: (now - timedelta(minutes=2)).timestamp())
    out = _reason(monkeypatch, "git restore a.py")
    assert out.splitlines()[-1].startswith("By:   Current Session · ")



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
    monkeypatch.setattr(hooks.git_guard, "_git_read", _read)


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
    monkeypatch.setattr(hooks.git_guard, "_git_read", _read)
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
