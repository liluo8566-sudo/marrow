"""PreToolUse git guards: force-push deny, revert-type ask,
session write ledger ownership."""
from __future__ import annotations

import json
import os
import re as _re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from .. import config
from .bash_guard import _isolation_hit, _isolation_prefixes
from .lifecycle import _is_worktree_session

# ── git force-push guard (PreToolUse) — hard deny ─────────────────────────────
# Force push rewrites remote history: a hard deny, no escape hatch, no worktree
# exemption. Tokenized per shell segment so a commit -m "...--force..." message
# can never false-positive. Config: [hooks].git_force_push_guard (default true).
# Message is mechanism-defining copy (_GIT_FORCE_PUSH_MSG).
_GIT_FORCE_PUSH_MSG = (
    "BLOCKED — force push rewrites remote history and can permanently destroy "
    "commits. Never force push. If the remote rejected your push, stop and "
    "report to the user."
)


def _git_force_push_matches(cmd: str) -> bool:
    """True if any shell segment is a `git push` carrying --force / -f /
    --force-with-lease. Tokenized (shlex) so flags inside a quoted commit
    message are single tokens and cannot match."""
    if not cmd:
        return False
    import shlex
    for seg in _GIT_REVERT_SEP_RE.split(cmd):
        seg = seg.strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        if not toks or toks[0] != "git" or "push" not in toks:
            continue
        for t in toks:
            if t in ("--force", "-f") or t.startswith("--force-with-lease"):
                return True
    return False


def _git_force_push_guard(inp: dict) -> str | None:
    """Force-push deny reason, or None. Config: [hooks].git_force_push_guard
    (default true). Fail-open: any error → None."""
    try:
        if not isinstance(inp, dict) or inp.get("tool_name") != "Bash":
            return None
        hooks_cfg = config.load().get("hooks", {}) or {}
        if not hooks_cfg.get("git_force_push_guard", True):
            return None
        cmd = (inp.get("tool_input") or {}).get("command", "") or ""
        if not isinstance(cmd, str) or not _git_force_push_matches(cmd):
            return None
        return _GIT_FORCE_PUSH_MSG
    except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
        return None


# ── git revert-type guard (PreToolUse) — held for authorship check ────────────
# Distinct from the backup guard: revert-type git ops discard work, so the
# risk is WHOSE work. Decision is "ask" (surface to the user), not a silent
# deny — the model must first verify the diff's authorship. Worktree/agent
# cleanup (branch -D teardown, worktree remove) is exempt.
_GIT_REVERT_DEFAULT_PATTERNS = [
    r"\bgit\s+reset\s+--hard\b",
    # Every `git checkout`, with or without `--`, and with global flags
    # between `git` and the subcommand (`git -C <dir> checkout f`,
    # `git --work-tree=… checkout f`). Only dash-tokens (plus the value of a
    # value-taking global flag) may sit in between, so a `checkout` word
    # inside a quoted argument of another subcommand can't drag it in.
    # Branch switch vs file overwrite is decided in code, not here
    # (_git_revert_loss_holds → _git_checkout_file_operands).
    r"\bgit\s+(?:(?:-C|--git-dir|--work-tree|--namespace)\s+\S+\s+|-\S+\s+)*"
    r"checkout\b",
    r"\bgit\s+restore\b",                            # worktree discard
    r"\bgit\s+clean\s+-\w*f",                        # -f / -fd
    r"\bgit\s+branch\s+-\w*D\w*\b",
    r"\bgit\s+stash\s+(?:drop|clear)\b",
    r"\bgit\s+revert\b[^\n]*--no-edit\b",
    r"\bgit\s+switch\b[^\n]*--discard-changes\b",
    r"\bgit\s+worktree\s+remove\b",
]

_GIT_REVERT_MSG = (
    "BLOCKED — git revert/reset requested. Confirm with the user before "
    "proceeding."
)


# Split on shell control operators (&&, ||, ;, |, &, newline) so pattern
# matching is evaluated PER SEGMENT — a safe segment (`git restore --staged
# a`) can never launder an unsafe one later in the same compound command
# (`git restore --staged a && git restore b`). Not a full shell parser
# (simple split — this guard is fail-open assist), mirrors the backup
# guard's `_BG_SHELL_SEP_RE` with newline added per review.
_GIT_REVERT_SEP_RE = _re.compile(r"&&|\|\||[;&|]|\n")


def _git_repo_dir(raw: str, cwd: str) -> str:
    """Absolute repo dir for a `-C` / `--work-tree` operand. A RELATIVE dir is
    resolved against the tool command's cwd — `_git_read` shells out without
    `cwd=`, so a bare relative dir would otherwise resolve against the hook
    process's own directory and query the wrong repo (or none)."""
    if not raw:
        return ""
    p = os.path.expanduser(raw)
    if os.path.isabs(p) or not cwd:
        return p
    return os.path.normpath(os.path.join(cwd, p))


def _git_path_tracked(cwd: str, path: str) -> bool:
    """True when *path* is tracked in the index. Disk presence is irrelevant —
    `git ls-files` still lists a tracked file that was rm'd, and checking it
    out would resurrect it over the deletion."""
    return bool((_git_read(cwd, ["ls-files", "--", path]) or "").strip())


def _git_checkout_file_operands(pos: list, rest: list, cwd: str) -> list:
    """Path operands of a no-`--` `git checkout` that would overwrite the
    working tree. Tracked operands are file targets; an operand that is only a
    ref is a branch switch (dropped). Tracked AND a valid ref is ambiguous —
    kept, so the guard asks. `-b`/`-B`/`--orphan` (new branch) and a bare
    `git checkout` yield nothing."""
    if any(f in rest for f in ("-b", "-B", "--orphan")):
        return []
    return [t for t in pos if _git_path_tracked(cwd, t)]


def _git_worktree_dirty(cwd: str, paths: list) -> bool:
    """True when `git status --porcelain` reports anything for *paths* —
    staged, unstaged or deleted. Unknown (git can't answer) → True: the guard
    asks rather than silently letting a destructive op through."""
    args = ["status", "--porcelain"] + (["--", *paths] if paths else [])
    out = _git_read(cwd, args)
    return True if out is None else bool(out.strip())


def _git_revert_loss_holds(seg: str, cwd: str) -> bool:
    """Loss gate for checkout/restore segments: hold only when uncommitted
    work would actually be destroyed. A checkout with no file target (branch
    switch, `-b`, bare) never holds; clean targets pass silently. Segments the
    parser can't read hold, as before."""
    parsed = _git_revert_parse(seg, cwd)
    if not parsed:
        return True
    action = parsed["action"]
    if action not in ("checkout-file", "restore"):
        # The pattern hit a `checkout`/`restore` word inside another
        # subcommand's argument (e.g. a commit message) — not the op itself.
        return False
    if action == "checkout-file" and not parsed["paths"]:
        return False
    return _git_worktree_dirty(parsed["repo"] or cwd, parsed["paths"])


# ── session write ledger (git-revert exemption) ──────────────────────────────
# A session may silently discard files IT wrote this session — those are its
# own drafts. Anything it never wrote may carry the user's (or another
# session's) edits, so the ask stands. The write set comes from the session's
# own transcript, cached per sid with a byte offset so only the tail is
# rescanned (same shape as the ct cursor). Unreadable → empty set → ask.
_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _write_ledger_path(sid: str) -> Path:
    return config.DATA_DIR / "state" / "write_ledger" / f"{sid}.json"


def _scan_write_paths(records: list) -> set:
    out: set = set()
    for r in records:
        content = (r.get("message") or {}).get("content") if isinstance(r, dict) else None
        if not isinstance(content, list):
            continue
        for it in content:
            if not isinstance(it, dict) or it.get("type") != "tool_use":
                continue
            if it.get("name") not in _WRITE_TOOLS:
                continue
            args = it.get("input") or {}
            for key in ("file_path", "notebook_path"):
                v = args.get(key)
                if isinstance(v, str) and v.strip():
                    out.add(os.path.realpath(os.path.expanduser(v.strip())))
    return out


def _session_write_set(sid: str, tpath: str) -> set:
    """Absolute realpaths this session wrote via Edit/Write/MultiEdit/
    NotebookEdit. Incremental: cached paths + byte offset per sid, tail-only
    rescan. Any failure returns an empty set (fail toward asking)."""
    if not tpath:
        return set()
    try:
        size = os.path.getsize(tpath)
        cached: dict = {}
        if sid:
            try:
                d = json.loads(_write_ledger_path(sid).read_text())
                cached = d if isinstance(d, dict) else {}
            except Exception:  # noqa: BLE001 — absent/corrupt cache → full scan
                cached = {}
        paths = {p for p in cached.get("paths") or [] if isinstance(p, str)}
        off = cached.get("offset")
        start = off if isinstance(off, int) and 0 <= off <= size else 0
        if start >= size:
            return paths
        with open(tpath, "rb") as f:
            f.seek(start)
            blob = f.read()
        records: list = []
        for raw in blob.split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                records.append(json.loads(raw.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        paths |= _scan_write_paths(records)
        if sid:
            try:
                p = _write_ledger_path(sid)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"offset": size, "paths": sorted(paths)}))
            except Exception:  # noqa: BLE001 — cache is an optimisation only
                pass
        return paths
    except Exception:  # noqa: BLE001
        return set()


def _memo0(fn):
    """Memoise a zero-arg callable — the wrapped call runs at most once."""
    box: list = []

    def _get():
        if not box:
            box.append(fn())
        return box[0]
    return _get


def _git_revert_own_writes(seg: str, cwd: str, write_set_fn) -> bool:
    """True when EVERY explicit path operand of a checkout/restore segment is a
    file this session wrote itself. Operands resolve against the segment's repo
    dir and the repo root; both sides compared as realpaths.

    *write_set_fn* is a memoised zero-arg provider, resolved only once a
    segment actually needs the ledger — a non-git Bash call never reads or
    writes the ledger files."""
    if write_set_fn is None:
        return False
    parsed = _git_revert_parse(seg, cwd)
    if not parsed or parsed["action"] not in ("checkout-file", "restore"):
        return False
    paths = parsed["paths"]
    if not paths:
        return False
    write_set = write_set_fn()
    if not write_set:
        return False
    base = parsed["repo"] or cwd
    root = (_git_read(base, ["rev-parse", "--show-toplevel"]) or "").strip()
    for raw in paths:
        pp = (raw or "").strip().strip("'\"")
        if not pp:
            return False
        e = os.path.expanduser(pp)
        if os.path.isabs(e):
            cands = [os.path.realpath(e)]
        else:
            cands = [os.path.realpath(os.path.join(b, e))
                     for b in (base, root) if b]
        if not any(c in write_set for c in cands):
            return False
    return True


_AGENT_BRANCH_DEFAULT_PREFIXES = ["worktree-agent-"]


def _git_seg_tokens(seg: str) -> list[str]:
    import shlex
    try:
        return shlex.split(seg)
    except Exception:  # noqa: BLE001 — unbalanced quotes
        return (seg or "").split()


def _git_worktree_remove_holds(seg: str, cwd: str = "") -> bool:
    """Hold `git worktree remove` only in its forced form — git itself refuses
    to remove a dirty worktree, so the plain form can't destroy work."""
    parsed = _git_revert_parse(seg, cwd)
    if not parsed:
        return True
    if parsed["action"] != "worktree-remove":
        return False
    return any(
        t == "--force" or (t.startswith("-") and not t.startswith("--") and "f" in t)
        for t in _git_seg_tokens(seg)
    )


def _git_branch_delete_holds(seg: str, cwd: str, hooks_cfg: dict) -> bool:
    """Hold `git branch -D` unless EVERY operand is an agent branch (worktree
    teardown). Config: [hooks].agent_branch_prefixes."""
    parsed = _git_revert_parse(seg, cwd)
    if not parsed:
        return True
    if parsed["action"] != "branch-D":
        return False
    ops = [r for r in parsed["refs"] if r != "branch"]
    pres = [p for p in (hooks_cfg or {}).get("agent_branch_prefixes") or []
            if isinstance(p, str) and p.strip()] or _AGENT_BRANCH_DEFAULT_PREFIXES
    return not (ops and all(any(o.startswith(p) for p in pres) for o in ops))


def _git_revert_segment_matches(
    seg: str, pats: list, cwd: str = "", hooks_cfg: dict | None = None,
    write_set_fn=None,
) -> bool:
    """True if one shell segment contains a git revert-type op.
    `git restore --staged` alone (unstage only, no worktree discard) is safe
    — evaluated within this segment only, never command-wide."""
    # No IGNORECASE: `git branch -D` (force) must stay distinct from the safe
    # lowercase `-d` (git refuses unmerged deletes itself). git verbs/flags are
    # lowercase in practice, so case-sensitive matching loses nothing.
    restore_safe = bool(
        _re.search(r"\bgit\s+restore\b", seg)
        and _re.search(r"--staged\b", seg)
        and not _re.search(r"(--worktree\b|\s-W\b)", seg)
    )
    for p in pats:
        try:
            if not _re.search(p, seg):
                continue
        except _re.error:
            continue
        if restore_safe and "restore" in p:
            continue  # safe unstage-only restore — don't hold on this pattern
        if "checkout" in p or "restore" in p:
            if _git_revert_own_writes(seg, cwd, write_set_fn):
                continue  # only this session's own drafts — nothing to confirm
            if not _git_revert_loss_holds(seg, cwd):
                continue  # nothing uncommitted at the targets — no loss
        if "worktree" in p and not _git_worktree_remove_holds(seg, cwd):
            continue  # unforced worktree remove — git refuses if dirty
        if "branch" in p and not _git_branch_delete_holds(seg, cwd, hooks_cfg or {}):
            continue  # agent-branch teardown only
        return True
    return False


def _git_revert_matches(cmd: str, cwd: str = "", hooks_cfg: dict | None = None,
                        write_set_fn=None) -> bool:
    """True if any shell segment of *cmd* contains a git revert-type op per
    config patterns."""
    if not cmd:
        return False
    if hooks_cfg is None:
        hooks_cfg = config.load().get("hooks", {}) or {}
    pats = hooks_cfg.get("git_revert_patterns") or _GIT_REVERT_DEFAULT_PATTERNS
    for seg in _GIT_REVERT_SEP_RE.split(cmd):
        seg = seg.strip()
        if seg and _git_revert_segment_matches(seg, pats, cwd, hooks_cfg,
                                               write_set_fn):
            return True
    return False


# ── revert-guard reason enrichment ───────────────────────────────────────────
# Last-resort {action} filler when config carries no `unknown` label (English
# in code; the user-facing copy lives in git_revert_action_labels).
_GIT_REVERT_UNKNOWN_ACTION = "touch your git history"

# The ask reason must answer "whose work is about to be destroyed": a config
# action phrase, the git op as invoked, the affected paths, the LOC delta and
# an ownership verdict. Every step below is best-effort and read-only — any
# failure degrades to a shorter reason; the guard's DECISION never changes.

def _git_read(cwd: str, args: list[str], timeout: int = 3) -> str | None:
    """Read-only `git -C <cwd> <args>`; None on failure or non-zero exit."""
    if not cwd:
        return None
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                           text=True, timeout=timeout, check=False)
    except Exception:  # noqa: BLE001
        return None
    return r.stdout if r.returncode == 0 else None


def _numstat_parse(out: str | None) -> tuple[int, int, list[str]]:
    """(added, deleted, paths) from `--numstat` output. Binary rows ('-')
    count zero lines but still contribute their path."""
    add = dele = 0
    files: list[str] = []
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[2].strip():
            continue
        if parts[0].isdigit():
            add += int(parts[0])
        if parts[1].isdigit():
            dele += int(parts[1])
        p = parts[2].strip()
        if p not in files:
            files.append(p)
    return add, dele, files


def _git_revert_parse(seg: str, cwd: str = "") -> dict | None:
    """Classify one matched shell segment.

    Returns {action, cmd, refs, paths, repo}: `action` = stable id used for the
    config label lookup, `cmd` = subcommand + flags + refs as invoked with path
    operands dropped, `repo` = an explicit `git -C <dir>` target if present.
    None when the segment doesn't parse as a git op.
    """
    import shlex
    try:
        toks = shlex.split(seg)
    except Exception:  # noqa: BLE001 — unbalanced quotes
        toks = seg.split()
    while toks and toks[0] != "git":
        toks = toks[1:]
    if len(toks) < 2:
        return None
    i, c_dir, wt_dir = 1, "", ""
    while i < len(toks) and toks[i].startswith("-"):
        t = toks[i]
        if t in ("-C", "--git-dir", "--work-tree") and i + 1 < len(toks):
            if t == "-C":
                c_dir = c_dir or toks[i + 1]
            elif t == "--work-tree":
                wt_dir = wt_dir or toks[i + 1]
            i += 2
            continue
        if t.startswith("--work-tree="):
            wt_dir = wt_dir or t.split("=", 1)[1]
        i += 1
    repo_dir = _git_repo_dir(c_dir or wt_dir, cwd)
    if i >= len(toks):
        return None
    sub, rest = toks[i], toks[i + 1:]
    pos = [t for t in rest if not t.startswith("-")]
    paths: list[str] = []
    action = sub
    if sub == "reset":
        action = ("reset-hard" if "--hard" in rest
                  else "reset-soft" if "--soft" in rest else "reset-mixed")
    elif sub in ("checkout", "switch", "restore"):
        action = ("switch-discard" if sub == "switch"
                  else "restore" if sub == "restore" else "checkout-file")
        if "--" in rest:
            paths = [t for t in rest[rest.index("--") + 1:] if t != "--"]
        elif sub == "restore":
            paths = list(pos)
        elif sub == "checkout":
            paths = _git_checkout_file_operands(pos, rest, repo_dir or cwd)
    elif sub == "revert":
        action = "revert"
    elif sub == "branch":
        action = "branch-D"
    elif sub == "stash":
        action = "stash-drop"
    elif sub == "worktree":
        action = "worktree-remove"
        paths = pos[1:]  # after the `remove` verb
    elif sub == "clean":
        action = "clean"
        paths = list(pos)
    # `--` only separated the path operands that were just dropped — keeping it
    # would trail a bare separator on the human-facing Action line.
    keep = toks[:i + 1] + [t for t in rest if t not in paths and t != "--"]
    return {"action": action, "cmd": " ".join(keep), "refs": pos,
            "paths": paths, "repo": repo_dir}


def _git_default_branch(cwd: str) -> str:
    head = (_git_read(cwd, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
            or "").strip()
    if head:
        return head
    for cand in ("main", "master"):
        if _git_read(cwd, ["rev-parse", "--verify", "--quiet", cand]):
            return cand
    return "HEAD"


def _git_revert_impact(parsed: dict, cwd: str) -> dict:
    """{files, loc: (add, del) | None, note, ts: epoch | None} for the parsed
    op. Empty-ish dict on anything unrecognised — caller degrades."""
    action, paths, refs = parsed["action"], parsed["paths"], parsed["refs"]
    out: dict = {"files": [], "loc": None, "note": "", "ts": None}
    if action in ("checkout-file", "restore", "switch-discard"):
        args = ["diff", "--numstat"] + (["--"] + paths if paths else [])
        add, dele, files = _numstat_parse(_git_read(cwd, args))
        out["files"] = paths or files
        out["loc"] = (add, dele)
        out["ts"] = _max_mtime(cwd, files or paths)
    elif action.startswith("reset"):
        add, dele, files = _numstat_parse(_git_read(cwd, ["diff", "--numstat", "HEAD"]))
        out["files"], out["loc"] = files, (add, dele)
        out["ts"] = _max_mtime(cwd, files)
        ref = next((r for r in refs if r not in ("reset",)), "")
        if ref and ref != "HEAD":
            log = _git_read(cwd, ["log", "--oneline", f"{ref}..HEAD"])
            n = len([x for x in (log or "").splitlines() if x.strip()])
            if n:
                out["note"] = f"({n} commit{'s' if n > 1 else ''})"
        if out["ts"] is None:
            out["ts"] = _commit_ts(cwd, "HEAD")
    elif action == "revert":
        ref = next((r for r in refs if r != "revert"), "HEAD")
        add, dele, files = _numstat_parse(
            _git_read(cwd, ["show", "--numstat", "--format=", ref]))
        out["files"], out["loc"] = files, (add, dele)
        out["ts"] = _commit_ts(cwd, ref)
    elif action == "branch-D":
        br = next((r for r in refs if r != "branch"), "")
        if br:
            base = _git_default_branch(cwd)
            add, dele, files = _numstat_parse(
                _git_read(cwd, ["log", "--numstat", "--format=", f"{base}..{br}"]))
            out["files"], out["loc"] = files, (add, dele)
            out["ts"] = _commit_ts(cwd, br)
    elif action == "stash-drop":
        add, dele, files = _numstat_parse(_git_read(cwd, ["stash", "show", "--numstat"]))
        out["files"], out["loc"] = files, (add, dele)
    elif action == "worktree-remove":
        target = paths[0] if paths else ""
        st = _git_read(target, ["status", "--porcelain"]) if target else None
        n = len([x for x in (st or "").splitlines() if x.strip()])
        if target:
            out["files"] = [target + (f" ({n} uncommitted)" if n else "")]
    elif action == "clean":
        listing = _git_read(cwd, ["clean", "-nd"] + (["--"] + paths if paths else []))
        out["files"] = [x.split(" ", 2)[-1].strip()
                        for x in (listing or "").splitlines()
                        if x.startswith("Would remove")]
        out["ts"] = _max_mtime(cwd, out["files"])
    return out


def _max_mtime(cwd: str, rel_paths: list[str]) -> float | None:
    """Newest mtime among repo-relative paths, resolved from the repo root."""
    if not rel_paths:
        return None
    root = (_git_read(cwd, ["rev-parse", "--show-toplevel"]) or "").strip() or cwd
    best: float | None = None
    for rp in rel_paths[:20]:
        for base in (root, cwd):
            try:
                m = os.stat(os.path.join(base, rp)).st_mtime
            except OSError:
                continue
            best = m if best is None or m > best else best
            break
    return best


def _commit_ts(cwd: str, ref: str) -> float | None:
    out = (_git_read(cwd, ["log", "-1", "--format=%ct", ref]) or "").strip()
    try:
        return float(out.splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


def _iso_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _cwd_related(a: str | None, b: str | None) -> bool:
    """Same directory, or one is an ancestor of the other."""
    if not a or not b:
        return False
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _git_revert_owner(sid: str, cwd: str, ts: float | None) -> str | None:
    """Ownership verdict for a change made at *ts*. None = can't tell (the
    caller omits the line rather than guessing)."""
    if not sid or ts is None:
        return None
    try:
        conn = sqlite3.connect(f"file:{config.db_path()}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT sid, channel, cwd, created_at, last_active, ended_at "
            "FROM sessions ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
        conn.close()
    except Exception:  # noqa: BLE001
        return None
    cur = next((r for r in rows if r["sid"] == sid), None)
    if cur is None:
        return None
    created = _iso_utc(cur["created_at"]) or _iso_utc(cur["last_active"])
    if created is None:
        return None
    ts_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        hhmm = ts_dt.astimezone(config.get_tz()).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        hhmm = ts_dt.strftime("%H:%M")

    def _covers(r) -> bool:
        st = _iso_utc(r["created_at"])
        en = _iso_utc(r["ended_at"]) or _iso_utc(r["last_active"])
        return bool(st and en and st <= ts_dt <= en)

    others = [r for r in rows
              if r["sid"] != sid and _cwd_related(r["cwd"], cwd) and _covers(r)]
    label = (f"{others[0]['channel'] or 'cli'}·{(others[0]['sid'] or '')[:4]}"
             if others else "")
    if ts_dt < created:
        return (f"⚠️ Other Session {label} · {hhmm}" if label
                else f"⚠️ Other Session · {hhmm}")
    if others:
        return f"⚠️ Overlapping with {label} · unclear"
    return f"Current Session · {hhmm}"


def _git_revert_reason(inp: dict, hooks_cfg: dict, cmd: str,
                       write_set_fn=None) -> str:
    """Full ask reason: headline + Action/File/LOC/By. Degrades to headline +
    Action, and to the bare headline, when git or the DB can't answer."""
    template = hooks_cfg.get("git_revert_guard_message") or _GIT_REVERT_MSG
    pats = hooks_cfg.get("git_revert_patterns") or _GIT_REVERT_DEFAULT_PATTERNS
    labels = hooks_cfg.get("git_revert_action_labels") or {}
    parsed = None
    hook_cwd = inp.get("cwd") or ""
    for seg in _GIT_REVERT_SEP_RE.split(cmd):
        seg = seg.strip()
        if seg and _git_revert_segment_matches(seg, pats, hook_cwd, hooks_cfg,
                                               write_set_fn):
            parsed = _git_revert_parse(seg, hook_cwd)
            break
    if not parsed:
        # Matched the pattern but not classifiable (e.g. the git text sits
        # inside a quoted argument). Generic label — never an empty {action}.
        return template.replace(
            "{action}", str(labels.get("unknown") or _GIT_REVERT_UNKNOWN_ACTION)
        ).strip()
    action = parsed["action"]
    lines = [template.replace("{action}", str(labels.get(action) or action))]
    lines.append(f"Action: {parsed['cmd']}")
    cwd = parsed["repo"] or (inp.get("cwd") or "")
    try:
        imp = _git_revert_impact(parsed, cwd)
    except Exception:  # noqa: BLE001 — degrade to headline + Action
        return "\n".join(lines)
    files = [f for f in imp.get("files") or [] if f]
    if files:
        shown = ", ".join(files[:3])
        if len(files) > 3:
            shown += f" (+{len(files) - 3})"
        lines.append(f"File: {shown}")
    loc = imp.get("loc")
    if loc and (loc[0] or loc[1]):
        note = f" {imp['note']}" if imp.get("note") else ""
        lines.append(f"LOC:  +{loc[0]} −{loc[1]}{note}")
    try:
        owner = _git_revert_owner(inp.get("session_id") or "", cwd, imp.get("ts"))
    except Exception:  # noqa: BLE001
        owner = None
    if owner:
        lines.append(f"By:   {owner}")
    return "\n".join(lines)


def _git_revert_guard(inp: dict) -> str | None:
    """Git revert-type authorship gate.

    Returns the 'ask' reason string to hold the call; "" when a git-revert op
    matched but it's worktree/agent cleanup (allow silently, skip the backup
    deny gate); None when it isn't a git-revert op at all (fall through).
    Config: [hooks].git_revert_guard (default true) + git_revert_patterns,
    isolation_prefixes, agent_branch_prefixes. checkout/restore of files this
    session wrote itself is exempt (session write ledger).
    Fail-open: any error returns None."""
    try:
        if not isinstance(inp, dict) or inp.get("tool_name") != "Bash":
            return None
        hooks_cfg = config.load().get("hooks", {}) or {}
        if not hooks_cfg.get("git_revert_guard", True):
            return None
        cmd = (inp.get("tool_input") or {}).get("command", "") or ""
        cwd = inp.get("cwd") or ""
        if not isinstance(cmd, str):
            return None
        # Lazy: the ledger is read/written only if a checkout/restore segment
        # with path operands actually needs it, and then only once.
        sid, tpath = inp.get("session_id") or "", inp.get("transcript_path") or ""
        write_set_fn = _memo0(lambda: _session_write_set(sid, tpath))
        if not _git_revert_matches(cmd, cwd, hooks_cfg, write_set_fn):
            return None
        # Worktree/agent cleanup teardown stays allowed — the isolation zone in
        # the cwd or anywhere in the command, or a live worktree-session
        # detection when the cwd resolves. Whole-command level by design.
        prefixes = _isolation_prefixes(hooks_cfg)
        if (
            _isolation_hit(cwd, prefixes)
            or _isolation_hit(cmd, prefixes)
            or _is_worktree_session(cwd)
        ):
            return ""
        reason = ""
        try:
            reason = _git_revert_reason(inp, hooks_cfg, cmd, write_set_fn)
        except Exception:  # noqa: BLE001 — enrichment is additive, never fatal
            reason = ""
        # Never return "" here — the caller reads "" as worktree-exempt allow.
        return reason or hooks_cfg.get("git_revert_guard_message") or _GIT_REVERT_MSG
    except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
        return None
