"""PreToolUse entrypoints: pretool_use, agent_guard."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from .. import config, cortex_bridge, repo
from ._shared import _read_input
from .bash_guard import (
    _backup_guard_deny,
    _backup_guard_line,
    _rm_to_trash_rewrite,
)
from .git_guard import _git_force_push_guard, _git_revert_guard
from .state import _load_sticker_nudge, _save_sticker_nudge

_PLACEMENT_BASH_OPS = {"mv", "cp", "rename", "mmv", "touch", "mkdir"}


def agent_guard() -> int:
    """PreToolUse:Agent burst protection — deny recursion-prone subagents.

    Blocks any Agent dispatch whose subagent_type is in [agent_guard].deny
    (default: general-purpose — it spawns agents out of control). Exit 2 +
    stderr surfaces the reason to the model. Fail-soft: errors exit 0.
    """
    try:
        inp = _read_input()
        if not isinstance(inp, dict) or inp.get("tool_name") != "Agent":
            return 0
        ti = inp.get("tool_input") or {}
        sub = (ti.get("subagent_type") or "general-purpose").strip()
        deny = config.load().get("agent_guard", {}).get("deny", ["general-purpose"])
        if sub in deny:
            print(
                f"[burst-guard] subagent_type={sub!r} is denied — it spawns "
                "agents out of control. Use Explore/executor/coder or a worktree "
                "agent with an explicit model instead.",
                file=sys.stderr,
            )
            return 2
    except Exception:
        return 0
    return 0


def pretool_use() -> int:
    """PreToolUse hook: emit placement guidance for Write/Bash file ops.

    Write or Bash (mv/cp/rename/mmv/touch/mkdir) -> placement mode.
    Edit or other -> literal mode (just path reminder).
    Fail-soft: any error -> silent exit 0.
    """
    try:
        inp = _read_input()
        tool = inp.get("tool_name", "")
        ti = inp.get("tool_input", {})

        # rm → trash auto-rewrite. Runs BEFORE every guard so classification
        # sees the rewritten (harmless) command; the updatedInput + context are
        # merged into whatever hookSpecificOutput the guards below emit. Rewrite
        # wins for the rewritten segments; decisions are computed on the
        # rewritten command (a remaining un-rewritten rm still denies normally).
        rewrite_updated: dict | None = None
        rewrite_ctx: str | None = None
        try:
            rewrite_updated, rewrite_ctx = _rm_to_trash_rewrite(inp)
        except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
            rewrite_updated, rewrite_ctx = None, None
        ti = inp.get("tool_input", {})  # re-read: rewrite may have replaced command

        def _emit_hso(fields: dict) -> None:
            hso = {"hookEventName": "PreToolUse", **fields}
            if rewrite_updated is not None:
                hso["updatedInput"] = rewrite_updated
            if rewrite_ctx:
                ex = hso.get("additionalContext")
                hso["additionalContext"] = (
                    f"{rewrite_ctx}\n\n{ex}" if ex else rewrite_ctx
                )
            print(json.dumps({"hookSpecificOutput": hso}))

        # Force-push hard deny — runs first, no escape hatch, no worktree
        # exemption. Rewriting remote history can permanently destroy commits.
        force_push: str | None = None
        try:
            force_push = _git_force_push_guard(inp)
        except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
            force_push = None
        if force_push:
            _emit_hso({
                "permissionDecision": "deny",
                "permissionDecisionReason": force_push,
            })
            return 0

        # Git revert-type authorship gate — held via "ask" (user confirms
        # whose diff is being discarded). "" (worktree-exempt) only skips the
        # ASK below — the backup deny gate still runs, because the exemption
        # test is a cheap substring match on the whole command and a compound
        # `git checkout -- .claude/worktrees/x && rm -rf ~/y` must not ride an
        # unrelated rm through unexamined. Genuine worktree cleanup stays
        # unblocked via the backup guard's OWN whitelist, which is the right
        # layer to own that decision. None = not a git-revert op at all
        # (fall through, both gates apply normally).
        git_revert: str | None = None
        try:
            git_revert = _git_revert_guard(inp)
        except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
            git_revert = None
        if git_revert:
            _emit_hso({
                "permissionDecision": "ask",
                "permissionDecisionReason": git_revert,
            })
            return 0

        # Cortex lie_down nudge — non-blocking additionalContext on every cortex
        # lie_down call (rotate arg selects the rotate copy). Emitted on its own
        # (lie_down is not a placement op, so it falls out of the Write/Bash
        # guidance path). Never denies.
        lie_down_nudge: str | None = None
        try:
            if cortex_bridge.enabled():
                lie_down_nudge = cortex_bridge._cortex_lie_down_nudge(inp)
        except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
            lie_down_nudge = None
        if lie_down_nudge:
            _emit_hso({"additionalContext": lie_down_nudge})
            return 0

        # Deny tier — block dangerous ops (recursive delete / db destruction
        # with no same-command backup). Short-circuits placement/atlas guidance;
        # the tool call itself is what needs gating. Only a genuinely matched
        # git-revert op (non-empty ask reason) owns the decision and skips
        # this gate — "" (worktree-exempt) and None (no match) both fall
        # through so a compound command's OTHER destructive segment still
        # gets examined.
        deny_reason: str | None = None
        if not git_revert:
            try:
                deny_reason = _backup_guard_deny(inp)
            except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
                deny_reason = None
        if deny_reason:
            _emit_hso({
                "permissionDecision": "deny",
                "permissionDecisionReason": deny_reason,
            })
            return 0

        if tool == "mcp__marrow__sticker" and str(
            (ti or {}).get("action", "")
        ).strip().lower() == "pick":
            sid = inp.get("session_id") if isinstance(inp, dict) else None
            if sid:
                try:
                    _sn = _load_sticker_nudge(sid)
                    _sn["last_sticker_turn"] = _sn.get("turn_count", 0)
                    _save_sticker_nudge(sid, _sn)
                except Exception:
                    pass

        guard_line: str | None = None
        try:
            guard_line = _backup_guard_line(inp)
        except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
            guard_line = None

        def _emit(text: str) -> None:
            payload = f"{guard_line}\n\n{text}" if guard_line else text
            _emit_hso({"additionalContext": payload})

        # Determine mode
        is_placement = False
        target_path_str: str | None = None

        if tool == "Write":
            is_placement = True
            target_path_str = ti.get("file_path", "")
        elif tool == "Bash":
            import shlex
            cmd = ti.get("command", "")
            try:
                tokens = shlex.split(cmd)
            except ValueError:
                tokens = cmd.split()
            # Trim to first command segment so `mv A B && echo ok` doesn't
            # let `ok` masquerade as the move target.
            _SHELL_SEP = {"&&", "||", ";", "|", "&"}
            for _i, _t in enumerate(tokens):
                if _t in _SHELL_SEP:
                    tokens = tokens[:_i]
                    break
            tokens_no_flags = [t for t in tokens if t and not t.startswith("-")]
            if tokens_no_flags and tokens_no_flags[0] in _PLACEMENT_BASH_OPS:
                is_placement = True
                op = tokens_no_flags[0]
                args_only = tokens_no_flags[1:]
                if op in {"mv", "cp"} and len(args_only) >= 2:
                    target_path_str = args_only[-1]
                elif args_only:
                    target_path_str = args_only[-1]

        if not is_placement:
            # No placement guidance applies (read-only / non-placement op).
            # The backup guard reminder (and any rm->trash rewrite) must
            # still surface — orthogonal to placement/atlas coverage.
            if guard_line is not None:
                _emit_hso({"additionalContext": guard_line})
            elif rewrite_updated is not None:
                _emit_hso({})
            return 0

        # Resolve target path
        if not target_path_str:
            if guard_line is not None:
                _emit_hso({"additionalContext": guard_line})
            elif rewrite_updated is not None:
                _emit_hso({})
            return 0

        target = Path(target_path_str).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        target = target.resolve()

        # Check against AUTHORIZED_ROOTS
        from .. import atlas as _atlas_mod
        from .. import drift_sweep
        from .. import storage, config
        roots = [r.expanduser().resolve() for r in drift_sweep.AUTHORIZED_ROOTS]

        root = _atlas_mod._root_of(str(target), roots)
        if root is None:
            # No atlas guidance for this target, but the backup guard reminder
            # (and any rm→trash rewrite) must still surface — orthogonal to
            # placement/atlas coverage.
            if guard_line is not None:
                _emit_hso({"additionalContext": guard_line})
            elif rewrite_updated is not None:
                _emit_hso({})
            return 0

        # Build ancestor chain: root -> parent of target (inclusive)
        # Ancestors from root down to target's parent
        chain: list[Path] = []
        try:
            rel = target.relative_to(root)
            parts = rel.parts
            # root itself
            chain.append(root)
            # intermediate dirs
            for i in range(1, len(parts)):
                chain.append(root / Path(*parts[:i]))
        except ValueError:
            chain = [root]

        # Fetch atlas rows for chain
        conn = storage.connect(config.db_path())
        try:
            chain_rows: dict[str, dict] = {}
            for p in chain:
                rows = conn.execute(
                    "SELECT path, description, naming_hint, depth FROM atlas WHERE path=?",
                    (str(p),),
                ).fetchall()
                for r in rows:
                    chain_rows[r["path"]] = dict(r)

            _home = Path.home()

            def _tilde(p: str) -> str:
                try:
                    return "~/" + str(Path(p).relative_to(_home))
                except ValueError:
                    return p

            # "Own" naming = raw naming_hint that isn't empty and isn't the
            # P/p inherit marker. Only own rules get a Naming: line so the
            # root rule isn't redundantly echoed at every descendant.
            _P_MARKERS = {"p", "P"}

            def _own_naming(row: dict | None) -> str:
                if not row:
                    return ""
                nh = (row.get("naming_hint") or "").strip()
                if not nh or nh in _P_MARKERS:
                    return ""
                return nh

            def _emit_block(path_str: str, row: dict | None,
                            is_root: bool = False) -> list[str]:
                blk: list[str] = [_tilde(path_str)]
                desc = (row or {}).get("description") if row else None
                desc = (desc or "").strip()
                if desc:
                    blk.append(f"- Description: {desc}")
                own = _own_naming(row)
                if own:
                    blk.append(f"- Naming: {own}")
                elif is_root:
                    # Root must always show resolved naming as the source of truth.
                    blk.append(f"- Naming: {_atlas_mod.resolve_naming(conn, path_str, roots)}")
                # Leaf placeholder: no description, no own rule -> hint at siblings.
                if not desc and not own and not is_root:
                    blk.append("- (empty -> ls siblings for pattern)")
                return blk

            lines: list[str] = []
            lines.append("[Path/Naming rules]")
            lines.append("- Do not dump files in ~/")
            lines.append("- Unsure = stop + clarify")
            lines.append("- Naming inherits from nearest ancestor with a rule")
            lines.append("- rename/move -> sweep all refs")
            lines.append("")
            lines.append(f"[Atlas slice for {_tilde(str(target))}]")

            root_str = str(root)
            lines.extend(_emit_block(root_str, chain_rows.get(root_str, {}), is_root=True))

            # Mid-chain (between root and parent, exclusive) -
            # only emit if the row has its own description or own naming.
            mid_chain = chain[1:-1] if len(chain) > 2 else []
            for mp in mid_chain:
                ms = str(mp)
                mr = chain_rows.get(ms)
                if mr and ((mr.get("description") or "").strip() or _own_naming(mr)):
                    lines.append("")
                    lines.extend(_emit_block(ms, mr))

            # Parent block - always emit when distinct from root.
            if len(chain) > 1:
                parent = chain[-1]
                parent_str = str(parent)
                lines.append("")
                lines.extend(_emit_block(parent_str, chain_rows.get(parent_str, {})))

            _emit("\n".join(lines))
        finally:
            conn.close()

    except Exception as e:  # noqa: BLE001
        try:
            repo.add_alert("info", "atlas_hook", "atlas_hook_error",
                           message=str(e), source="hooks.py",
                           db=config.db_path())
        except Exception:
            pass
    return 0
