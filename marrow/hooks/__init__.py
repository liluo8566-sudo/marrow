"""Thin CC hook entrypoints. `python -m marrow.hooks <event>`.

Code-only, no LLM. Parallel-safe with the legacy ny-memm hooks —
marrow registers ALONGSIDE them, never replaces. Logic lives in the marrow
package; this only does hook I/O (stdin JSON in, stdout JSON for
SessionStart additionalContext, side effects for SessionEnd).

  session_start      -> inject alerts + timeline backdrop; drain alerts fallback
  session_end        -> write terminal lifecycle:end marker
  user_prompt_submit -> recall fusion injection

Events are archived per-turn by the Stop hook; SessionEnd no longer cleans or
archives the transcript. PreToolUse is the global prompt-guard.py (scope already
covers ~/CC-Lab/marrow/), not duplicated here.

Submodules (pure split of the former hooks.py, no logic change):
  _shared       stdin JSON reader
  state         per-session state files (recall seen, cursors, recall logs)
  housekeep     git auto-commit block + ~/.claude.json snapshot
  lifecycle     session_start / session_end / stop
  recall_inject user_prompt_submit + hit rendering
  bash_guard    backup guard tiers + rm -> trash rewrite
  git_guard     force-push / revert-type guards + write ledger
  pretool       pretool_use / agent_guard
  inject        turn_inject + kickout / usage-threshold context
"""
from __future__ import annotations

import sys

from .. import config, repo, storage, transcript  # noqa: F401 — patch surface
from . import (  # noqa: F401
    _shared,
    bash_guard,
    git_guard,
    housekeep,
    inject,
    lifecycle,
    pretool,
    recall_inject,
    state,
)
from ._shared import _read_input  # noqa: F401
from .bash_guard import (  # noqa: F401
    _backup_guard_deny,
    _backup_guard_line,
    _isolation_hit,
    _isolation_prefixes,
    _rm_to_trash_rewrite,
)
from .git_guard import (  # noqa: F401
    _git_force_push_guard,
    _git_repo_dir,
    _git_revert_guard,
    _git_revert_matches,
    _git_revert_owner,
    _session_write_set,
)
from .housekeep import (  # noqa: F401
    _build_housekeep_commit_msg,
    _build_housekeep_report_line,
    _categorize_porcelain,
    _claude_json_snapshot_block,
    _commit_housekeep_groups,
    _git_housekeep_block,
    _porcelain_paths,
    _session_tag,
    _split_housekeep_dirty,
)
from .inject import (  # noqa: F401
    _in_time_window,
    _kickout_context,
    _usage_threshold_context,
    _window_tokens_from_transcript,
    turn_inject,
)
from .lifecycle import (  # noqa: F401
    _is_worktree_session,
    _primary_worktree,
    session_end,
    session_start,
    stop,
)
from .pretool import agent_guard, pretool_use  # noqa: F401
from .recall_inject import (  # noqa: F401
    _append_recall_log,
    _apply_rel_cutoff,
    _recall_head,
    _render_hit_block,
    user_prompt_submit,
)
from .state import (  # noqa: F401
    _RECALL_TZ,
    _load_outbound_cursor,
    _load_recall_seen,
    _load_sticker_nudge,
    _outbound_notes,
    _prune_recall_logs,
    _save_outbound_cursor,
    _save_recall_seen,
    _save_sticker_nudge,
)

_EVENTS = {
    "session_start": session_start,
    "session_end": session_end,
    "stop": stop,
    "user_prompt_submit": user_prompt_submit,
    "turn_inject": turn_inject,
    "pretool_use": pretool_use,
    "agent_guard": agent_guard,
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in _EVENTS:
        print(f"usage: python -m marrow.hooks {{{'|'.join(_EVENTS)}}}",
              file=sys.stderr)
        return 2
    try:
        return _EVENTS[args[0]]()
    except Exception as e:  # hook must never break the session
        try:
            repo.add_alert("warn", "hook", f"hook_dispatch_failed:{args[0]}",
                           message=str(e), source="hooks.py",
                           db=config.db_path())
        except Exception:
            pass
        return 0
