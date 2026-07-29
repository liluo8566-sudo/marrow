"""PreToolUse backup guard tiers + rm -> trash auto-rewrite."""
from __future__ import annotations



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


def test_backup_guard_scratchpad_dir_itself_silent(env, monkeypatch, capsys):
    # Regression: the whitelist used a "/scratchpad/" substring test, so the
    # directory ITSELF (no trailing slash) was denied.
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "rm -rf /Users/x/project/scratchpad"})
    assert rc == 0
    out = _hook_out(capsys)
    assert "permissionDecision" not in out
    assert _BG_MSG not in out.get("additionalContext", "")


def test_backup_guard_worktree_dir_itself_silent(env, monkeypatch, capsys):
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "rm -rf /Users/x/proj/.claude/worktrees"})
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)


def test_backup_guard_isolation_prefix_path_silent(env, monkeypatch, capsys):
    # /tmp/claude-* and its /private twin are isolation zones (the /private one
    # is not covered by the plain /tmp prefix rule).
    rc = _pretool(monkeypatch, "Bash",
                  {"command": "rm -rf /private/tmp/claude-501/proj/scratch"})
    assert rc == 0
    assert "permissionDecision" not in _hook_out(capsys)


def test_backup_guard_non_whitelisted_lookalike_still_denies(env, monkeypatch, capsys):
    # `scratchpads` (plural) is not the whitelisted `scratchpad` dir.
    rc = _pretool(monkeypatch, "Bash", {"command": "rm -rf /Users/x/scratchpads"})
    assert rc == 0
    assert _out(capsys)["hookSpecificOutput"]["permissionDecision"] == "deny"


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
