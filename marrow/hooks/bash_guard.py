"""PreToolUse backup guard (reminder/deny tiers) + rm -> trash rewrite."""
from __future__ import annotations

import os
import re as _re
from .. import config

# ── agent/worktree isolation zones ───────────────────────────────────────────
# Path fragments marking throw-away agent workspaces. Shared by the backup
# guard's whitelist (deny tier) and the git-revert guard's exemption (ask
# tier): destructive housekeeping inside these zones can't lose the user's or
# another session's work. Config: [hooks].isolation_prefixes.
_ISOLATION_DEFAULT_PREFIXES = [
    ".claude/worktrees/",
    "/private/tmp/claude-",
    "/tmp/claude-",
]


def _isolation_prefixes(hooks_cfg: dict) -> list[str]:
    out = [p for p in (hooks_cfg or {}).get("isolation_prefixes") or []
           if isinstance(p, str) and p.strip()]
    return out or list(_ISOLATION_DEFAULT_PREFIXES)


def _isolation_hit(text: str, prefixes: list[str]) -> bool:
    """True when *text* (a cwd or a whole command) carries an isolation zone
    fragment. Substring test — the path may sit anywhere in a command."""
    t = text or ""
    return any(p in t for p in prefixes)


# ── backup guard (PreToolUse) — stateless, two tiers ─────────────────────────
# Reminder tier — additionalContext text, fires EVERY matching call (no dedup,
#   no state): any rm on a non-whitelisted path (non-recursive; recursive lands
#   in the deny tier), bulk mv/sed -i with wildcard sources to a non-whitelisted
#   dest, DELETE FROM without WHERE on a line (when not a db-destruction deny),
#   and mcp destructive calls (event_clear/db_clear, sticker delete,
#   mcp__marrow__* action clear/delete).
# Deny tier — permissionDecision "deny", stateless. Recursive rm on a
#   non-whitelisted path, rm of a *.db file outside the whitelist, or a sqlite3
#   command destroying a *.db (DROP TABLE / TRUNCATE / DELETE FROM w/o WHERE).
#   Escape hatch: a backup action (cp/rsync/tar/git commit/git stash push/
#   .backup) in the SAME command → fully silent allow (no deny, no reminder).
#   Downgrades to the reminder tier when [hooks].backup_guard_intercept=false.
# Fail-open throughout: any exception -> treat as no match, never block.
# Git ops are owned entirely by the git-revert ask guard and the force-push
# deny guard below — the backup guard no longer classifies any git command.

_BG_SHELL_SEP_RE = _re.compile(r"&&|\|\||[;&|]")

# A same-command backup action satisfies the deny escape hatch (and silences
# the reminder — they did it right).
_BG_BACKUP_CMD_RE = _re.compile(
    r"\bcp\b|\brsync\b|\btar\b|\bgit\s+commit\b|\bgit\s+stash\s+push\b|\.backup\b",
    _re.IGNORECASE,
)

# db-destruction (deny): a sqlite3 command touching a *.db path with DROP TABLE
# / TRUNCATE / DELETE FROM without WHERE.
_BG_SQLITE_RE = _re.compile(r"\bsqlite3\b", _re.IGNORECASE)
_BG_DB_PATH_RE = _re.compile(r"\S+\.db\b", _re.IGNORECASE)
_BG_DROP_TABLE_RE = _re.compile(r"\bdrop\s+table\b", _re.IGNORECASE)
_BG_TRUNCATE_RE = _re.compile(r"\btruncate\b", _re.IGNORECASE)

_BG_REMIND_MSG = (
    "Warning: back up code/db OR archive docs before anything destructive — "
    "delete, bulk modify, import/export.\n"
    "- Bypass only if the user explicitly said to delete/modify THIS now.\n"
    "- If unsure, stop and ask — never assume the user doesn't need/want it."
)

_BG_DENY_MSG = (
    "BLOCKED — bulk deletion with no backup. This target is not in git/tmp: "
    "once deleted it is gone. Chain a backup into the SAME command and rerun, "
    "e.g. `tar -czf /tmp/bak.tgz <target> && rm -rf <target>` (db: `cp` first). "
    "Even if the user ordered the deletion, back up anyway — it costs one "
    "command. Do NOT work around this guard with alternative delete commands."
)


_BG_WHITELIST_FRAGMENTS = ["/scratchpad/"]


def _bg_is_whitelisted_path(p: str) -> bool:
    """Whitelist scope: /tmp, /private/tmp, any path inside a /scratchpad/ dir,
    and the configured isolation zones — destructive ops there are silent.
    A path that IS the zone directory itself (no trailing slash) counts too."""
    pp = (p or "").strip().strip("'\"")
    if not pp:
        return False
    if pp in ("/tmp", "/private/tmp"):
        return True
    if pp.startswith("/tmp/") or pp.startswith("/private/tmp/"):
        return True
    try:
        prefixes = _isolation_prefixes(config.load().get("hooks", {}) or {})
    except Exception:  # noqa: BLE001 — fail-open to the built-in zones
        prefixes = list(_ISOLATION_DEFAULT_PREFIXES)
    # Probe with a trailing slash so `<...>/scratchpad` matches "/scratchpad/".
    probe = pp if pp.endswith("/") else pp + "/"
    return any(f in probe for f in _BG_WHITELIST_FRAGMENTS + prefixes)


def _bg_resolve_for_whitelist(p: str, cwd: str) -> str:
    """Resolve a relative positional path against the hook-provided cwd,
    purely for the whitelist test (no filesystem access, no realpath — the
    hook must stay fast and side-effect free). Absolute paths, `~`-paths, and
    wildcard-only tokens (glob chars present) are returned unchanged — a glob
    can't be joined meaningfully and is handled by its own broad-path logic.
    If cwd is empty/missing, the raw (relative) path is returned unchanged —
    it will not match the whitelist, which is today's (safe-side) behavior."""
    pp = (p or "").strip().strip("'\"")
    if not pp or not cwd:
        return p
    if pp.startswith("/") or pp.startswith("~") or any(ch in pp for ch in "*?["):
        return p
    return os.path.normpath(os.path.join(cwd, pp))


def _bg_raw_segments(cmd: str) -> list[str]:
    """Ordered raw (untokenized) shell segments, split on &&/||/;/|/&. Used
    for segment-ordered backup/destructive checks — position, not just
    presence, decides the escape hatch (a backup keyword AFTER the
    destructive segment must not launder it)."""
    return [s.strip() for s in _BG_SHELL_SEP_RE.split(cmd or "") if s.strip()]


def _bg_bash_segments(cmd: str) -> list[list[str]]:
    """Best-effort shell split on &&/||/;/|/& then shlex-tokenize each
    segment. Not a full shell parser (mirrors the placement guard's
    trim-to-first-segment approach)."""
    import shlex
    out: list[list[str]] = []
    for seg in _bg_raw_segments(cmd):
        try:
            out.append(shlex.split(seg))
        except ValueError:
            out.append(seg.split())
    return out


def _bg_rm_segment(tokens: list[str], cwd: str = "") -> str | None:
    """Classify a single `rm` segment: 'deny' (recursive on a non-whitelisted
    path, or a *.db target), 'remind' (non-recursive on a non-whitelisted
    non-db path), or None (whitelisted / not rm).

    `cwd` (from the hook's `cwd` input, may be empty) is joined onto relative
    positional paths ONLY for the whitelist test below — the raw token still
    drives the .db-suffix and recursive-flag checks (blast-radius signal is
    about what the user/model typed, not the resolved path; a relative path
    resolved into /Users/... is not "broad" just because it resolved there —
    see _bg_resolve_for_whitelist)."""
    if not tokens or tokens[0] != "rm":
        return None
    args = tokens[1:]
    flags = [t for t in args if t.startswith("-")]
    positional = [t for t in args if not t.startswith("-")]
    if not positional:
        return None
    non_wl = [
        p for p in positional
        if not _bg_is_whitelisted_path(_bg_resolve_for_whitelist(p, cwd))
    ]
    if not non_wl:
        return None  # all whitelisted — silent
    if any((p or "").strip().strip("'\"").endswith(".db") for p in non_wl):
        return "deny"
    recursive = any("r" in f.lower() for f in flags)
    if recursive:
        return "deny"
    return "remind"


def _bg_bulk_modify_segment(tokens: list[str]) -> bool:
    """True for a bulk mv (wildcard source → non-whitelisted dest) or a bulk
    in-place sed edit (sed -i over a wildcard non-whitelisted file). Single-file
    mv / sed stays silent."""
    if not tokens:
        return False
    op = tokens[0]
    args = tokens[1:]
    flags = [t for t in args if t.startswith("-")]
    positional = [t for t in args if not t.startswith("-")]
    if op == "mv":
        if len(positional) < 2:
            return False
        sources, dest = positional[:-1], positional[-1]
        if not any(any(ch in s for ch in "*?[") for s in sources):
            return False  # only bulk (wildcard-source) mv is flagged
        return not _bg_is_whitelisted_path(dest)
    if op == "sed":
        if not any(f == "-i" or f.startswith("-i") or f == "--in-place" for f in flags):
            return False
        for p in positional[1:]:  # positional[0] is the sed script
            if any(ch in p for ch in "*?[") and not _bg_is_whitelisted_path(p):
                return True
        return False
    return False


def _bg_sqlite_db_destruction(cmd: str) -> bool:
    """True for a sqlite3 command touching a *.db path that DROP TABLE /
    TRUNCATE / DELETE FROM without WHERE."""
    if not (_BG_SQLITE_RE.search(cmd) and _BG_DB_PATH_RE.search(cmd)):
        return False
    if _BG_DROP_TABLE_RE.search(cmd) or _BG_TRUNCATE_RE.search(cmd):
        return True
    for line in cmd.splitlines():
        low = f" {line.lower()} "
        if "delete from" in low and " where " not in low:
            return True
    return False


def _bg_has_backup(cmd: str) -> bool:
    return bool(cmd) and bool(_BG_BACKUP_CMD_RE.search(cmd))


def _bg_bash_category(cmd: str, cwd: str = "") -> str | None:
    """'deny', 'remind', or None for a Bash command. Stateless and
    intercept-agnostic.

    `cwd` is the hook-provided working directory (may be empty) — it is only
    used to resolve relative positional paths for the whitelist test inside
    _bg_rm_segment (see there). No attempt is made to emulate `cd` across
    shell segments (e.g. `cd X && rm -rf Y` — out of scope); the single
    hook-provided cwd is used as-is for every segment.

    The escape hatch is segment-ORDERED, not whole-string: a backup action
    (cp/rsync/tar/git commit/git stash push/.backup) only satisfies it when it
    appears in an EARLIER segment than the first destructive segment
    (recursive rm on a non-whitelisted path, rm of a *.db, or sqlite3 *.db
    destruction). A backup keyword landing AFTER the destructive segment (or
    absent) does not launder it — deny stands. No backup-target/path matching
    is done (position-only check — same segment order both `cp a /tmp && rm
    -rf x` and `cp <target> /tmp && rm -rf <target>` are treated identically)."""
    if not cmd:
        return None
    import shlex
    segments = _bg_raw_segments(cmd)
    if not segments:
        return None

    backup_idx: int | None = None
    destructive_idx: int | None = None
    seg_tokens: list[list[str]] = []
    for i, seg in enumerate(segments):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        seg_tokens.append(toks)
        if backup_idx is None and _bg_has_backup(seg):
            backup_idx = i
        if destructive_idx is None and (
            _bg_rm_segment(toks, cwd) == "deny" or _bg_sqlite_db_destruction(seg)
        ):
            destructive_idx = i

    if destructive_idx is not None:
        if backup_idx is not None and backup_idx < destructive_idx:
            return None  # backup landed BEFORE the destructive segment — silent
        return "deny"

    # No deny-tier match anywhere — compute the reminder tier as before.
    remind = False
    for toks in seg_tokens:
        if _bg_rm_segment(toks, cwd) == "remind":
            remind = True
        if _bg_bulk_modify_segment(toks):
            remind = True
    # DELETE FROM without WHERE on a line that is NOT a sqlite .db destruction
    # (that path already returned "deny" above).
    for line in cmd.splitlines():
        low = f" {line.lower()} "
        if "delete from" in low and " where " not in low:
            remind = True
    return "remind" if remind else None


def _bg_mcp_destructive(tool_name: str, tool_input: dict) -> bool:
    name = (tool_name or "").lower()
    if "db_clear" in name or "event_clear" in name:
        return True
    if "sticker" in name and "delete" in name:
        return True
    if name.startswith("mcp__marrow__"):
        action = str((tool_input or {}).get("action", "")).strip().lower()
        if action in {"clear", "delete"}:
            return True
    return False


def _bg_category(tool_name: str, tool_input: dict, cwd: str = "") -> str | None:
    """Return 'deny' / 'remind' / None. Write/Edit are no longer guarded
    (a write requires a prior read, so it is recoverable)."""
    ti = tool_input or {}
    if tool_name == "Bash":
        return _bg_bash_category(ti.get("command", "") or "", cwd)
    if tool_name and tool_name not in ("Bash", "Write", "Edit"):
        if _bg_mcp_destructive(tool_name, ti):
            return "remind"
    return None


def _backup_guard_deny(inp: dict) -> str | None:
    """Stateless deny reason for the deny tier, or None to allow. None when
    disabled, downgraded ([hooks].backup_guard_intercept=false → becomes a
    reminder instead), not a deny-tier match, or on any error (fail-open)."""
    try:
        if not isinstance(inp, dict):
            return None
        hooks_cfg = config.load().get("hooks", {}) or {}
        if not hooks_cfg.get("backup_guard", True):
            return None
        if not hooks_cfg.get("backup_guard_intercept", True):
            return None
        tool_name = inp.get("tool_name", "") or ""
        ti = inp.get("tool_input", {}) or {}
        cwd = inp.get("cwd") or ""
        if _bg_category(tool_name, ti, cwd) != "deny":
            return None
        return _BG_DENY_MSG
    except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
        return None


def _backup_guard_line(inp: dict) -> str | None:
    """Reminder text (additionalContext), fires EVERY matching call — no dedup,
    no state. Also carries deny-tier calls downgraded to a reminder when
    [hooks].backup_guard_intercept is false.

    Config-gated via [hooks].backup_guard (default enabled). Fail-open: any
    exception returns None so the guard never breaks the hook."""
    try:
        if not isinstance(inp, dict):
            return None
        hooks_cfg = config.load().get("hooks", {}) or {}
        if not hooks_cfg.get("backup_guard", True):
            return None
        tool_name = inp.get("tool_name", "") or ""
        ti = inp.get("tool_input", {}) or {}
        cwd = inp.get("cwd") or ""
        cat = _bg_category(tool_name, ti, cwd)
        if cat == "deny" and not hooks_cfg.get("backup_guard_intercept", True):
            cat = "remind"  # downgraded — no deny gate, surface the reminder
        if cat != "remind":
            return None
        return _BG_REMIND_MSG
    except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
        return None


# ── rm → trash auto-rewrite (PreToolUse) ─────────────────────────────────────
# Rewrite a Bash `rm` segment to `/usr/bin/trash <paths>` when ALL its
# positional targets resolve under a configured trash_paths prefix — the delete
# becomes recoverable from Trash. Runs BEFORE the backup-guard classification so
# a rewritten segment (no longer an rm) reclassifies as harmless; any remaining
# un-rewritten rm segment still classifies normally. Mixed targets, zero
# positionals, and wildcard tokens are left untouched (a quoted glob would not
# expand). Fail-open: any error leaves the command unchanged.
_RM_TRASH_BIN = "/usr/bin/trash"
# Capturing split so the original separators survive round-trip reassembly.
_RM_TRASH_SPLIT_RE = _re.compile("(" + _BG_SHELL_SEP_RE.pattern + ")")


def _rm_trash_prefixes(hooks_cfg: dict) -> list[str]:
    """Expanded, normalised trash_paths prefixes (each ends with '/')."""
    out: list[str] = []
    for p in hooks_cfg.get("trash_paths") or []:
        if not isinstance(p, str) or not p.strip():
            continue
        e = os.path.normpath(os.path.expanduser(p.strip()))
        out.append(e if e.endswith("/") else e + "/")
    return out


def _rm_trash_resolve(p: str, cwd: str) -> str:
    """Resolve a positional token to an absolute path for the prefix test:
    expand ~, join a relative path onto cwd. No filesystem access."""
    pp = (p or "").strip().strip("'\"")
    if not pp:
        return pp
    pp = os.path.expanduser(pp)
    if pp.startswith("/"):
        return os.path.normpath(pp)
    if cwd:
        return os.path.normpath(os.path.join(cwd, pp))
    return pp


def _rm_trash_under(path: str, prefixes: list[str]) -> bool:
    if not path:
        return False
    for pre in prefixes:
        if path == pre.rstrip("/") or path.startswith(pre):
            return True
    return False


def _rm_trash_rewrite_segment(seg: str, cwd: str, prefixes: list[str]) -> str | None:
    """Return the rewritten segment (surrounding whitespace preserved) if it is
    an `rm` whose positional paths ALL fall under a trash prefix, else None."""
    import shlex
    core = seg.strip()
    if not core:
        return None
    try:
        toks = shlex.split(core)
    except ValueError:
        return None
    if not toks or toks[0] != "rm":
        return None
    positional = [t for t in toks[1:] if not t.startswith("-")]
    if not positional:
        return None
    # A quoted glob would not expand — leave wildcard segments for the guard.
    if any(any(ch in t for ch in "*?[") for t in positional):
        return None
    resolved = [_rm_trash_resolve(t, cwd) for t in positional]
    if not all(_rm_trash_under(r, prefixes) for r in resolved):
        return None
    new_core = _RM_TRASH_BIN + " " + " ".join(shlex.quote(r) for r in resolved)
    lead = seg[: len(seg) - len(seg.lstrip())]
    trail = seg[len(seg.rstrip()):]
    return f"{lead}{new_core}{trail}"


def _rm_to_trash_rewrite(inp: dict) -> tuple[dict | None, str | None]:
    """Rewrite qualifying rm segments to /usr/bin/trash. Mutates
    inp['tool_input']['command'] in place so downstream guards reclassify the
    rewritten command. Returns (updated_input, context_line) or (None, None).
    Config-gated via [hooks].rm_to_trash. Fail-open."""
    try:
        if not isinstance(inp, dict) or inp.get("tool_name") != "Bash":
            return None, None
        hooks_cfg = config.load().get("hooks", {}) or {}
        if not hooks_cfg.get("rm_to_trash", True):
            return None, None
        prefixes = _rm_trash_prefixes(hooks_cfg)
        if not prefixes:
            return None, None
        ti = inp.get("tool_input") or {}
        cmd = ti.get("command", "") or ""
        if not isinstance(cmd, str) or "rm" not in cmd:
            return None, None
        cwd = inp.get("cwd") or ""
        parts = _RM_TRASH_SPLIT_RE.split(cmd)
        rewritten: list[str] = []
        for i in range(0, len(parts), 2):
            new_seg = _rm_trash_rewrite_segment(parts[i], cwd, prefixes)
            if new_seg is not None:
                parts[i] = new_seg
                rewritten.append(new_seg.strip())
        if not rewritten:
            return None, None
        new_cmd = "".join(parts)
        ti["command"] = new_cmd
        inp["tool_input"] = ti
        ctx = ("rm auto-rewritten to trash (recoverable from Trash): "
               + "; ".join(rewritten))
        return {"command": new_cmd}, ctx
    except Exception:  # noqa: BLE001 — fail-open, never break the hook
        return None, None
