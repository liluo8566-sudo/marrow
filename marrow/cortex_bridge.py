"""Cortex bridge — the optional autonomous-wake ("cortex") organs, extracted
from daemon.py / hooks.py / llm.py into one module.

Cortex is an optional layer that lives in a SEPARATE repo (~/CC-Lab/cortex);
this module is marrow's side of that bridge: the MCP tools it exposes to a
cortex session and the SessionStart / PreToolUse / turn_inject hook branches
that only a cortex window takes.

Two independent gates, both must be open for any cortex behaviour:

  1. [cortex].enabled (config, default false) — "are the organs installed".
     enabled() == False  => register() is a no-op (no cortex tool reaches
     the MCP schema) and every hook helper here short-circuits to its inert
     value. A clean marrow install shows ZERO cortex behaviour.
  2. MARROW_CORTEX (env) — "is this the cortex session". Set by the shell's
     host on the cortex window it spawns.
     The lie_down / wait / say tools additionally require it at import time;
     the hook branches require it at call time.

So enabled == organs installed; MARROW_CORTEX == this is the cortex window.

This is a verbatim MOVE of the pre-existing cortex code — names, logic and
behaviour are unchanged. daemon/hooks/llm keep one-line, enabled-gated call
sites into here.
"""
from __future__ import annotations

import contextlib as _contextlib
import fcntl as _fcntl
import json as _json_ws
import os
import re as _re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from pydantic import Field

from . import config, storage


# ── gates ─────────────────────────────────────────────────────────────────────

def enabled() -> bool:
    """Master switch: [cortex].enabled in config (default false when absent)."""
    return bool(config.load().get("cortex", {}).get("enabled", False))


def is_cortex_session(transcript_path: str | None = None) -> bool:
    """True inside a cortex-spawned marrow window (MARROW_CORTEX in env). The
    conversation is the identity: a resumed/manually-opened cortex window carries
    the same env marker its spawn baked in. `transcript_path` is accepted for
    call-site compatibility but not used."""
    return bool(os.environ.get("MARROW_CORTEX"))


# Import-time capture of the cortex-session env marker, mirroring the original
# daemon._CORTEX: the lie_down / wait / say tools register into the MCP schema
# only when this daemon subprocess was spawned by a cortex window (the window
# sets MARROW_CORTEX explicitly before spawn). Normal sessions never see them.
_CORTEX = bool(os.environ.get("MARROW_CORTEX"))

# Fallback when [cortex].shells is missing/malformed: the cli shell only.
_DEFAULT_SHELLS = ("cli",)


def _shells() -> list[str]:
    """[cortex].shells — the shell ids that run as cortex shells. A shell absent
    from the list runs plain: no cortex tools, no cortex hook branches."""
    try:
        raw = config.load().get("cortex", {}).get("shells")
    except Exception:
        raw = None
    if not isinstance(raw, list):
        raw = list(_DEFAULT_SHELLS)
    return [str(s).strip().lower() for s in raw if str(s).strip()]


def _shell_enabled() -> bool:
    """True when this window is a cortex session (MARROW_CORTEX) AND its shell
    id is listed in [cortex].shells. The per-shell switch every cortex hook
    branch goes through."""
    if not os.environ.get("MARROW_CORTEX"):
        return False
    return _cortex_shell_id() in _shells()


# ── wish ─────────────────────────────────────────────────────────────────────

def _wishlist_path() -> Path:
    cortex_cfg = config.load().get("cortex", {})
    wp = (cortex_cfg.get("wishlist_path") or "").strip()
    if wp:
        return Path(wp).expanduser()
    home = cortex_cfg.get("home") or "~/.config/marrow/cortex"
    return Path(home).expanduser() / "wishlist.md"


_WISHLIST_HEADER = (
    "# Wishlist\n\n"
    "> Owed treats, wants, self-rewards. Append-only — hand edits are sacred.\n\n"
)


_HEADING_RE = _re.compile(r"^(#{2,3})\s+(.*)$")


def _insert_at_section_end(existing: str, section: str, line: str) -> str:
    """Insert `line` after the last non-empty line of the first heading (##
    or ###) whose text contains `section` (case-insensitive substring), and
    before the next heading of same-or-higher level. Falls back to plain
    append when no heading matches."""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    lines = existing.splitlines(keepends=True)
    needle = section.strip().lower()
    start = None
    start_level = None
    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln.rstrip("\n"))
        if m and needle in m.group(2).strip().lower():
            start = i
            start_level = len(m.group(1))
            break
    if start is None:
        return existing + line

    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = _HEADING_RE.match(lines[j].rstrip("\n"))
        if m and len(m.group(1)) <= start_level:
            end = j
            break

    last_content = start
    for j in range(start + 1, end):
        if lines[j].strip():
            last_content = j
    insert_at = last_content + 1
    return "".join(lines[:insert_at]) + line + "".join(lines[insert_at:])


def wish(
    text: Annotated[str, Field(description="The wish/promise/plan line to append; required. A leading '- ' is stripped. Stored as '[] <date> <text>' with the configured wish_date_format.")],
    section: Annotated[str | None, Field(description="Heading substring (## or ###, e.g. 心愿单/约定/种草) to insert the line at that section's end; omit to append at end of file.")] = None,
    due: Annotated[str | None, Field(description="Optional due tag appended as ' [<due>]' after the text (free-form, e.g. a date).")] = None,
) -> dict:
    """Our wishlist — personal wishes & cravings, promises
    made, and shared plans. e.g. 你说好请我喝奶茶 / 最近想买耳钉 / 约好周末去看海.
    Markdown structure (headings, subsections) is user-managed — this tool
    only adds lines, never edits existing content: ~/.config/marrow/cortex/wishlist.md."""
    import fcntl
    from datetime import datetime

    text = (text or "").strip()
    if text.startswith("- "):
        text = text[2:].strip()
    if not text:
        return {"ok": False, "error": "text required"}
    path = _wishlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    date_fmt = config.load().get("cortex", {}).get("wish_date_format", "%y/%m/%d")
    date = datetime.now(config.get_tz()).strftime(date_fmt)
    due = (due or "").strip()
    suffix = f" [{due}]" if due else ""
    line = f"[] {date} {text}{suffix}\n"
    lock_path = str(path) + ".lock"
    lf = open(lock_path, "a")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        existing = path.read_text(encoding="utf-8") if path.exists() else _WISHLIST_HEADER
        section = (section or "").strip()
        new_content = (
            _insert_at_section_end(existing, section, line)
            if section else existing + line
        )
        from ._atomic import atomic_write
        atomic_write(str(path), new_content)
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        lf.close()
    return {"ok": True, "path": str(path), "line": line.strip()}


# ── first ────────────────────────────────────────────────────────────────────

_FIRST_ACTIONS = {"tick", "untick", "list"}
_FIRST_STATUSES = {"done", "tried"}


def first(
    action: str,
    item: str | None = None,
    note: str | None = None,
    sid: str | None = None,
    status: str = "done",
) -> dict | list[dict]:
    """(pending — not registered; original description saved in CC-Lab/docs/notes/ct-first-goal-reconnect.md)
    Respond to the Cortex First section (notes/concerns injected into context).
    'tick' each item you acted on + a tiny note (1-10 chars), e.g. 处理好啦；等会儿再跟进。
    status='tried' when attempted but unsolved — note what blocked.
    'untick' a wrong ack; 'list' current ticks."""
    if action not in _FIRST_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_FIRST_ACTIONS)}"}

    if action == "list":
        conn = storage.connect(_DB)
        try:
            rows = conn.execute(
                "SELECT item, seen_at, sid, note, status FROM ct_first_tick ORDER BY seen_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    item = (item or "").strip()
    if not item:
        return {"ok": False, "error": "item required"}

    if action == "untick":
        conn = storage.connect(_DB)
        try:
            with conn:
                cur = conn.execute("DELETE FROM ct_first_tick WHERE item=?", (item,))
            return {"ok": cur.rowcount > 0, "item": item}
        finally:
            conn.close()

    # tick
    if status not in _FIRST_STATUSES:
        return {"ok": False, "error": f"status must be one of {sorted(_FIRST_STATUSES)}"}
    note = (note or "").strip() or None
    conn = storage.connect(_DB)
    try:
        if not sid:
            from .timeline import _query_current_sid
            sid = _query_current_sid(conn)
        with conn:
            conn.execute(
                "INSERT INTO ct_first_tick (item, seen_at, sid, note, status)"
                " VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?, ?)"
                " ON CONFLICT(item) DO UPDATE SET"
                " seen_at=excluded.seen_at, sid=excluded.sid, note=excluded.note,"
                " status=excluded.status",
                (item, sid, note, status),
            )
        return {"ok": True, "item": item, "sid": sid, "note": note, "status": status}
    finally:
        conn.close()


# ── goal ─────────────────────────────────────────────────────────────────────

_GOAL_ACTIONS = {"set", "list", "delete"}


def goal(
    action: str,
    key: str | None = None,
    value: str | None = None,
    unit: str | None = None,
) -> dict | list[dict]:
    """(pending — not registered; original description saved in CC-Lab/docs/notes/ct-first-goal-reconnect.md)
    Timetrack weekly goals e.g. study, sleep, exercise.
    action='set': create / update goals
    e.g. 'sleep goal 8h' → key='sleep' value='8' unit='h';
    'list'; 'delete' by key when dropped or achieved."""
    if action not in _GOAL_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_GOAL_ACTIONS)}"}

    if action == "set":
        key = (key or "").strip()
        value = (value or "").strip()
        if not key:
            return {"ok": False, "error": "key required"}
        if not value:
            return {"ok": False, "error": "value required"}
        conn = storage.connect(_DB)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO goals (key, value, unit, updated_at)"
                    " VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
                    " ON CONFLICT(key) DO UPDATE SET"
                    " value=excluded.value, unit=excluded.unit,"
                    " updated_at=excluded.updated_at",
                    (key, value, unit),
                )
            return {"ok": True, "key": key, "value": value, "unit": unit}
        finally:
            conn.close()

    if action == "list":
        conn = storage.connect(_DB)
        try:
            rows = conn.execute(
                "SELECT key, value, unit, updated_at FROM goals ORDER BY key"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # delete
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "key required"}
    conn = storage.connect(_DB)
    try:
        with conn:
            cur = conn.execute("DELETE FROM goals WHERE key=?", (key,))
        return {"ok": True, "key": key, "deleted": cur.rowcount > 0}
    finally:
        conn.close()


# ── cortex (lie_down / say) ───────────────────────────────────────────────────

def _cortex_paths() -> tuple[str, str]:
    """(venv_python, repo_root) from marrow config [cortex]; either empty =
    not configured. Both drive the cortex subprocess; repo_root is the cwd so
    `python -m cortex.X` resolves the package regardless of the caller's cwd."""
    c = config.load().get("cortex", {})
    return (str(c.get("venv_python") or "").strip(),
            str(c.get("repo_root") or "").strip())


def _run_cortex_module(module: str, extra_args: list[str] | None = None) -> dict:
    py, root = _cortex_paths()
    if not py or not root:
        return {"ok": False, "error": "cortex not configured "
                "([cortex].venv_python + repo_root in config.toml)"}
    py = str(Path(py).expanduser())
    root = str(Path(root).expanduser())
    cmd = [py, "-m", module] + (extra_args or [])
    try:
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{module} timed out after 30s"}
    except OSError as exc:
        return {"ok": False, "error": f"{module} failed to launch: {exc}"}
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or p.stdout or "").strip()
                or f"{module} exited {p.returncode}"}
    return {"ok": True, "stdout": (p.stdout or "").strip()}


def _shell_socket_path(shell: str) -> Path | None:
    """[cortex].shell_socket — the tg host's kick socket. Empty =
    <DATA_DIR>/state/shells/tg.sock. That single config value belongs to the tg
    bridge, so any other shell has no socket: None (the caller skips the kick
    and the host reads the ledger on its next tick). Short by contract: macOS
    caps an AF_UNIX path at 104 bytes."""
    if shell != "tg":
        return None
    cx = config.load().get("cortex", {}) or {}
    raw = str(cx.get("shell_socket") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return config.DATA_DIR / "state" / "shells" / "tg.sock"


def _shell_kick(shell: str) -> bool:
    """Poke a shell host's scheduler: one line "<shell>\\n" over its unix
    stream socket (the wire format synapse_core.scheduler.send_kick writes).
    Best-effort — a host that is down, or a shell with no socket configured,
    just means the ledger is read on its next recompute tick. Never raises."""
    import socket as _socket
    p = _shell_socket_path(shell)
    if p is None:
        return False
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(str(p))
            s.sendall((shell + "\n").encode("utf-8"))
        return True
    except OSError:
        return False


def shell_direct(text: str, shell: str = "tg") -> dict:
    """Directed kick: leave `text` in the shell's ledger (pending_note) and poke
    its host. The host claims the text on its next pass and feeds it as that
    round's turn — asleep shells wake for it, live ones take it next turn. Host
    down = the text still lands on the host's next recompute tick."""
    body = (text or "").strip()
    if not body:
        return {"ok": False, "error": "empty direction"}
    shell_state_write({"pending_note": body}, shell=shell)
    return {"ok": True, "shell": shell, "kicked": _shell_kick(shell)}


def _log_shell_sleep_row(shell: str) -> int | None:
    """Insert this shell's ct_wake_log row for a voluntary lie_down, so the
    sleep ledger covers every shell, not just cli (the cli row is written by
    cortex itself). Same shape as the cli rows: wake=1, dry_run=0, reasons
    tagging the origin, force_slept NULL (a voluntary call is no incident) and
    the shell stamp the note's force-slept filter reads. Best-effort: None on
    any error (old DB without the shell column included) — a missing ledger row
    must never fail the sleep."""
    import sqlite3
    from datetime import timezone as _tz
    try:
        conn = sqlite3.connect(config.db_path(), timeout=30)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute(
            "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons, shell) "
            "VALUES (?, 1, 0, 'lie_down', ?)",
            (datetime.now(_tz.utc).isoformat(), shell))
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _lie_down_shell(shell: str, next_wake_min: float,
                    rotate: bool = False) -> dict:
    """lie_down for a non-cli shell: the host owns the timing, so this only
    writes the wake ledger (<shell_state_dir>/<shell>.json) and kicks the host.
    Minutes are clamped to cortex's [wake].next_wake_max band; 0 = wake now.

    rotate=True adds the rotate_pending flag: the host claims it on the kicked
    pass and respawns the resident session (same end-of-window semantics as the
    cli shell's rotate) instead of feeding it a note. next_wake_at still stands,
    so the fresh session sleeps until the wake booked here.

    The sleep also lands one ct_wake_log row stamped with this shell, so the
    ledger records every shell's sleep (_log_shell_sleep_row)."""
    day_max = float(_cortex_toml_section("wake", "next_wake_max", 240))
    mins = max(0.0, min(float(next_wake_min), day_max))
    when = datetime.now(config.get_tz()) + timedelta(minutes=mins)
    payload = {"next_wake_at": when.isoformat()}
    if rotate:
        payload["rotate_pending"] = True
    shell_state_write(payload, shell=shell)
    _log_shell_sleep_row(shell)
    kicked = _shell_kick(shell)
    hm = when.strftime("%H:%M")
    return {"ok": True, "shell": shell, "next_wake": hm, "kicked": kicked,
            "rotate": bool(rotate), "text": f"next wake ≈ {hm}"}


def lie_down(next_wake_min: float, rotate: bool = False,
             mode: str | None = None, human_override: bool = False) -> dict:
    # Description rendered from cortex config at register() (C9); see
    # _lie_down_doc. Kept minimal here — FastMCP reads __doc__ at registration.
    # human_override = explicit minutes pierce the rotate clamp band (threaded
    # straight to cortex's --human-override).
    """lie_down(next_wake_min=N)."""
    shell = _cortex_shell_id()
    if not shell:
        return {"ok": False, "error": "MARROW_CORTEX is not a valid shell id"}
    if shell != "cli":
        return _lie_down_shell(shell, next_wake_min, rotate)
    args = ["--next-wake-min", str(next_wake_min)]
    if rotate:
        args += ["--rotate"]
    if mode:
        args += ["--mode", str(mode)]
    if human_override:
        args += ["--human-override"]
    out = _run_cortex_module("cortex.lie_down", args)
    # Surface the chosen wake time (cortex.lie_down prints JSON with a
    # "next_wake":"HH:MM" field). Old cortex builds omit it — tolerate silently.
    if out.get("ok"):
        try:
            import json as _json
            data = _json.loads(out.get("stdout") or "{}")
            nw = data.get("next_wake")
            if nw:
                out["next_wake"] = nw
                out["text"] = f"next wake ≈ {nw}"
            # Rotate precondition refusal (P17): cortex refused because this
            # window's own wake-signal ear tail is still alive. Surface the
            # refusal text as the tool result text — without this the model
            # never sees "TaskStop your monitor first, then call lie_down
            # again" and the whole rotate precondition is invisible.
            refused = data.get("refused")
            if data.get("skipped") == "rotate_refused" and refused:
                out["refused"] = refused
                out["text"] = refused
        except (ValueError, TypeError):
            pass
    return out


def say() -> dict:
    """Pop-up the window to seek attention. Use when you really want to find me."""
    return _run_cortex_module("cortex.say")


# ── daemon registration ───────────────────────────────────────────────────────

# DB the tools read/write. Set at register() time from the daemon's own _DB so
# it tracks the same source the daemon resolved at import; tests patch this.
_DB = config.db_path()


def _lie_down_doc() -> str:
    """C9 (user-final): lie_down description with the clamp range rendered
    from cortex config — [wake].next_wake_max (T3: 0-day_max for every hour,
    night band retired). Never hardcoded in the string."""
    day_max = int(_cortex_toml_section("wake", "next_wake_max", 360))
    return (f'lie_down(next_wake_min=N) [N=0-{day_max}]; '
            f'rotate to next window - lie_down(next_wake_min=N, rotate=True) '
            f'[N=0-{day_max}, 0=rotate now]')


def register(marrow_tool, db: str | None = None) -> None:
    """Install the cortex MCP tools onto the daemon when [cortex].enabled.

    `marrow_tool` is daemon.marrow_tool (the alwaysLoad tool decorator). enabled
    == False => no-op (none of the tools reach the schema). When enabled:
      - wish registers for ALL sessions;
      - first / goal are PENDING — not registered anywhere yet (no injection
        mechanism wired; keep the functions + storage, just don't expose them);
      - lie_down / say register ONLY in a cortex session (_CORTEX, the
        import-time MARROW_CORTEX capture — the original inner env gate) whose
        shell id is listed in [cortex].shells (shell resolved lazily here);
        lie_down serves every listed shell, say the cli shell only.
    Idempotent per process (FastMCP tolerates re-adding the same tool name)."""
    global _DB
    if db is not None:
        _DB = db
    if not enabled():
        return
    marrow_tool()(wish)
    if _CORTEX:
        shell = _cortex_shell_id()
        if shell not in _shells():
            return
        # Render the clamp numbers from cortex config into the tool description
        # at registration (C9) — never hardcoded in the docstring.
        lie_down.__doc__ = _lie_down_doc()
        marrow_tool()(lie_down)
        if shell == "cli":
            marrow_tool()(say)


# ── hooks: kickout immunity / lie_down nudge / handoff page-turn / show 亮牌 ────
# _window_tokens_from_transcript stays in hooks/inject.py (shared with the all-session
# _usage_threshold_context); it is imported lazily where needed below.

def _cortex_lie_down_nudge(inp: dict) -> str | None:
    """Non-blocking PreToolUse additionalContext for every cortex lie_down call:
    reminds the session to log/handoff. Never denies. rotate arg selects the
    rotate copy. Cortex window + lie_down only; None otherwise."""
    if not _shell_enabled():
        return None
    if inp.get("tool_name") != "mcp__marrow__lie_down":
        return None
    ti = inp.get("tool_input", {}) or {}
    wants_rotate = bool(ti.get("rotate"))
    cx = config.load().get("cortex", {}) or {}
    key = "lie_down_nudge_rotate_text" if wants_rotate else "lie_down_nudge_text"
    text = cx.get(key)
    if not text:
        return None
    return _fill_handoff(text)


# MARROW_CORTEX values that mean "the default (cli) shell": unset, or the
# legacy boolean marker from before the var carried a shell id.
_LEGACY_CLI_MARKERS = frozenset({"1", "true", "yes", "on"})
# A shell id names files (handoff-<shell>.md, <shell>.json) and is passed as a
# subprocess argument, so it must be a bare token.
_SHELL_ID_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_bad_shell_id_warned: set[str] = set()


def _warn_bad_shell_id(value: str) -> None:
    """Alert once per process per bad MARROW_CORTEX value. Never raises."""
    if value in _bad_shell_id_warned:
        return
    _bad_shell_id_warned.add(value)
    try:
        from . import repo
        repo.add_alert(
            "warn", "cortex_bad_shell_id", "cortex_bad_shell_id",
            source="cortex_bridge.py",
            message=f"MARROW_CORTEX={value!r} is not a valid shell id — "
                    "cortex shell state refused for this window",
            db=config.db_path(),
        )
    except Exception:  # noqa: BLE001 — a bad env var must not break the window
        pass


def _cortex_shell_id() -> str | None:
    """This window's shell id, from MARROW_CORTEX. Unset or a legacy truthy
    marker -> "cli"; any other value is the id itself ("tg"). A value that is
    not a bare id token is refused (alert + None) instead of collapsing onto
    cli — a malformed marker must never read or overwrite the cli shell's
    ledger, handoff or replay state."""
    v = (os.environ.get("MARROW_CORTEX") or "").strip().lower()
    if not v or v in _LEGACY_CLI_MARKERS:
        return "cli"
    if not _SHELL_ID_RE.match(v):
        _warn_bad_shell_id(v)
        return None
    return v


def _cortex_handoff_path():
    """<[cortex].home>/handoff-<shell>.md — the per-shell handoff file a fresh
    cortex window reads at SessionStart. None on config error or unusable
    shell id."""
    shell = _cortex_shell_id()
    if not shell:
        return None
    try:
        cx = config.load().get("cortex", {}) or {}
        home = (cx.get("home") or "~/.config/marrow/cortex")
        pattern = (cx.get("handoff_file_pattern") or "handoff-{shell}.md")
        name = pattern.replace("{shell}", shell)
        return Path(home).expanduser() / name
    except Exception:
        return None


def _cortex_home() -> Path:
    cx = config.load().get("cortex", {}) or {}
    return Path(cx.get("home") or "~/.config/marrow/cortex").expanduser()


def _cortex_path(key: str, default_name: str) -> Path:
    """Resolve a cortex-home file config value: absolute path used as-is,
    bare name resolved under <home>."""
    cx = config.load().get("cortex", {}) or {}
    raw = (cx.get(key) or default_name)
    p = Path(raw).expanduser()
    return p if p.is_absolute() else _cortex_home() / raw


def _render_note_fresh(transcript_path: str | None,
                       shell: str | None = None) -> str | None:
    """Fresh render via the cortex venv (config [cortex].render_module, e.g.
    cortex.note_render). Reflects the current time + the CALLER's transcript SID
    even after a window rotation, unlike the frozen file. Unset module / any
    failure / empty output -> None so the caller falls back to the static file.
    The window note carries no replay block — turn_inject is that outlet.

    `shell` (caller's shell id) is passed through as --shell so the render is
    scoped to THIS shell — its wake ledger, its own session's activity rows and
    its breaker pause state; without it every note rendered here silently used
    the default (cli) shell's data, even on tg."""
    c = config.load().get("cortex", {})
    module = str(c.get("render_module") or "").strip()
    py, root = _cortex_paths()
    if not module or not py or not root:
        return None
    py = str(Path(py).expanduser())
    root = str(Path(root).expanduser())
    # --no-ct: the wake-branch hook delivers ct notes via outbox.deliver, so the
    # fresh render must not also peek them (would double them in one payload).
    # --mirror: the renderer itself writes this shell's section of the on-disk
    # note (cortex owns that file format — see cortex/note_file.py), so marrow
    # never rewrites the whole file and never clobbers another shell.
    shell = shell or _cortex_shell_id()
    if not shell:
        return None
    cmd = [py, "-m", module, "--no-ct", "--mirror", "--shell", shell]
    if transcript_path:
        cmd += ["--transcript", str(transcript_path)]
    try:
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return None
    if p.returncode != 0:
        return None
    return (p.stdout or "").strip() or None


# ── wake bell: receipt sidecar (new) + shape fallback ─────────────────────────
# The visible bell line is human text only ([cortex].wake_bell_template). The
# machine data (gen/state_id/rearm) lives in a wake_state `wake_receipt` written
# by the producer at send time. Recognition order (match_wake_bell):
#   (a) receipt exact full-text match -> machine bell, consume receipt, epoch-
#       check as today (stale -> suppress).
#   (b) receipt missing/unreadable/expired -> shape fallback: the line starts
#       with the persisted template prefix -> treat as bell, fail OPEN on
#       staleness (process it), audit the degraded path.
#   (c) none -> normal user prompt.
# Failure direction (mirrors wake_token_current): never DROP a genuine wake; a
# swallowed user message is the only unacceptable outcome, so the receipt path
# is EXACT-match only.

def wake_bell_template(cfg: dict | None = None) -> str:
    """Template of the bell TYPED into a live resident cortex window."""
    cx = (cfg or config.load()).get("cortex", {}) or {}
    return str(cx.get("wake_bell_template") or "⏰ {hm}")


def spawn_opener_template(cfg: dict | None = None) -> str:
    """Template of the first prompt baked into a FRESHLY SPAWNED cortex window
    (cortex [wake].spawn_opener_template). A distinct shape from the resident
    bell, so the receipt-less shape fallback must accept both."""
    cx = (cfg or config.load()).get("cortex", {}) or {}
    return str(cx.get("spawn_opener_template") or "☀️ {hm}")


def _receipt_ttl_sec(cfg: dict | None = None) -> float:
    cx = (cfg or config.load()).get("cortex", {}) or {}
    try:
        return float(cx.get("receipt_ttl_min", 15)) * 60.0
    except (TypeError, ValueError):
        return 15 * 60.0


def _load_wake_receipt() -> dict | None:
    """Read the pending bell receipt from wake_state (no consume). None when
    absent/unreadable/expired past receipt_ttl_min."""
    try:
        d = _wake_state_load(_cortex_wake_state_path())
    except Exception:
        return None
    r = d.get("wake_receipt")
    if not isinstance(r, dict) or not r.get("text"):
        return None
    raw = r.get("ts")
    if raw:
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            from datetime import timezone as _tz
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            if (datetime.now(_tz.utc) - ts).total_seconds() > _receipt_ttl_sec():
                return None
        except (ValueError, TypeError):
            pass
    return r


def _consume_wake_receipt() -> None:
    """Drop the pending receipt under the flock (consume-once). Best-effort."""
    p = _cortex_wake_state_path()
    try:
        with _wake_state_lock(p):
            d = _wake_state_load(p)
            if d.pop("wake_receipt", None) is not None:
                _wake_state_save(p, d)
    except Exception:
        pass


def _receipt_matches(prompt: str, receipt: dict) -> bool:
    """Exact full-text match of the on-screen bell against the receipt text,
    tolerating the ear envelope/decoration wrapper (the ear delivers the line
    wrapped as '<event>☀️ HH:MM'). Line-by-line exact compare of the unwrapped
    text — never a substring, so a user message quoting the bell text mid-line
    is not swallowed."""
    text = str(receipt.get("text") or "").strip()
    if not text:
        return False
    for line in prompt.splitlines() or [prompt]:
        if _strip_envelope(line) == text:
            return True
    return False


_ENVELOPE_TRAIL_RE = _re.compile(r"(?:\s*</?[a-z][a-z0-9_-]*>){0,3}\s*$")


def _strip_envelope(line: str) -> str:
    """Drop the ear-Monitor envelope tags around a delivered line (leading
    <event>/<task-notification> and their trailing closers) for an exact compare."""
    head = _ENVELOPE_LEAD_RE.sub("", line, count=1)
    head = _ENVELOPE_TRAIL_RE.sub("", head, count=1)
    return head.strip()


def match_wake_bell(prompt: str) -> tuple[str, tuple[int, str] | None, bool] | None:
    """Classify a cortex-window prompt as a wake bell. Returns
    (kind, token, degraded) or None (not a bell):
      kind='receipt' -> exact receipt match; token=(gen,state_id) or None;
                        caller consumes the receipt + epoch-checks (suppress stale).
      kind='shape'   -> receipt missing but the line starts with the template
                        prefix; token=None, degraded=True; caller fails OPEN.
    A blank template prefix disables the shape fallback (never matches on empty)."""
    if not prompt:
        return None
    # (a) receipt exact match
    receipt = _load_wake_receipt()
    if receipt is not None and _receipt_matches(prompt, receipt):
        gen = receipt.get("gen")
        state_id = receipt.get("state_id")
        token = (int(gen), str(state_id)) if gen is not None and state_id else None
        return ("receipt", token, False)
    # (b) shape fallback: match the on-screen line against the template shape.
    #   - template WITH an {hm} placeholder -> '<prefix> HH:MM' (whole line), so
    #     an ordinary user message merely OPENING with the prefix emoji is NOT
    #     swallowed — only the near-exact bell shape is (documented accepted
    #     residue: a user typing the literal '<prefix> HH:MM' with no live receipt).
    #   - fully STATIC template (no {hm}) -> exact match of the static text.
    # Two producer shapes exist (resident bell + fresh-spawn opener); with no
    # usable receipt BOTH are candidates, else the receipt's own template wins.
    cands = [receipt.get("template")] if receipt and receipt.get("template") else []
    cands += [wake_bell_template(), spawn_opener_template()]
    for tmpl in cands:
        if tmpl and _line_matches_bell_shape(prompt, str(tmpl)):
            return ("shape", None, True)
    return None


def _line_matches_bell_shape(prompt: str, template: str) -> bool:
    """True iff ANY line of *prompt* matches the bell *template* shape after
    tolerating the ear envelope wrapper. A template with an '{hm}' placeholder
    matches '<prefix> HH:MM' (whole line); a fully static template (no {hm})
    matches its literal text exactly. Blank prefix on an {hm} template disables
    the fallback (never matches on empty)."""
    if "{hm}" in template:
        head, tail = template.split("{hm}", 1)
        prefix = head.strip()
        suffix = tail.strip()
        if not prefix:
            return False
        # Honor whatever wraps {hm} on BOTH sides (e.g. a bracketed template
        # '[<prefix> HH:MM]'): build the anchor from prefix AND suffix rather
        # than hardcoding a bare end-of-line, so a fully-bracketed bell matches.
        rx = _re.compile(
            rf"^{_re.escape(prefix)}\s*\d{{1,2}}:\d{{2}}\s*{_re.escape(suffix)}$")
        return any(rx.match(_strip_envelope(line)) for line in (prompt.splitlines() or [prompt]))
    static = template.strip()
    if not static:
        return False
    return any(_strip_envelope(line) == static for line in (prompt.splitlines() or [prompt]))


def wake_token_current(token: tuple[int, str] | None) -> bool:
    """True if a wake line's (gen, state_id) token still matches the live epoch.
    token=None (legacy line) -> True (process as before). A stale token (a newer
    epoch superseded this wake — e.g. a user message landed first) -> False, so
    the consumer suppresses the wake-note injection. Reads the shared wake_state
    under its flock; on any read/lock failure returns True (fail OPEN here: a
    doubtful read must never DROP a genuine wake — a spurious extra note is
    strictly safer than a missed wake)."""
    if token is None:
        return True
    try:
        p = _cortex_wake_state_path()
        with _wake_state_lock(p):
            d = _wake_state_load(p)
        gen, state_id = token
        if not isinstance(d.get("gen"), int):
            return True  # no epoch recorded yet -> nothing to invalidate against
        return d.get("gen") == gen and str(d.get("state_id") or "") == str(state_id)
    except Exception:
        return True


def wakeup_note_text(transcript_path: str | None = None,
                     shell: str | None = None) -> str | None:
    """Note text for the wake-bell injection. Tries a fresh render first (current
    time + caller's transcript SID, correct after rotation); on any failure falls
    back to the frozen file (config wakeup_note_file under home). None when both
    yield nothing so the caller injects nothing.

    `shell` defaults to this window's shell id (MARROW_CORTEX) — the same
    resolver the handoff file uses. Both paths are shell-scoped: the fresh
    render passes --shell, and the fallback slices THIS shell's section out of
    the sectioned note file (never another shell's, and never the heading —
    that line is display-only)."""
    shell = shell or _cortex_shell_id()
    if not shell:
        return None
    fresh = _render_note_fresh(transcript_path, shell)
    if fresh:
        return fresh
    try:
        raw = _cortex_path("wakeup_note_file", "wakeup_note.md").read_text(
            encoding="utf-8")
    except OSError:
        return None
    return _note_section(raw, shell)


# Cross-repo contract with cortex/note_file.py: `## <shell> · sid=<8 chars>`
# (sid optional). Duplicated as a 10-line reader rather than imported — marrow
# and cortex are separate repos/venvs with no shared package.
_NOTE_HEADING = "## "
_NOTE_SEP = " · "


def _note_section(text: str, shell: str) -> str | None:
    """Body of THIS shell's section, heading stripped. None when the section is
    absent or empty. A legacy heading-less file belongs to no shell -> None."""
    body: list[str] = []
    inside = False
    for line in (text or "").splitlines():
        if line.startswith(_NOTE_HEADING):
            rest = line[len(_NOTE_HEADING):].strip().split(_NOTE_SEP)[0].strip()
            head = rest.split()[0] if rest else ""
            if inside:
                break
            inside = head == shell
            continue
        if inside:
            body.append(line)
    return "\n".join(body).strip() or None


def free_round_note_text() -> str | None:
    """Read + CONSUME the invisible free-round payload cortex staged before it
    typed the ⏳ marker (cortex wake_state.free_round_note_path). Same
    marker->body pattern as the wake bell: only the short marker line is on
    screen, the note itself is injected as additionalContext.

    Consume-once (the file is deleted on read) and TTL-guarded by
    receipt_ttl_min: a payload whose marker turn never arrived (window died
    between staging and the prompt landing) is dropped instead of surfacing on
    an unrelated later round. None when absent/empty/stale."""
    p = _cortex_path("free_round_note_file", "free_round_note.md")
    try:
        text = p.read_text(encoding="utf-8").strip()
        fresh = (datetime.now().timestamp() - p.stat().st_mtime) <= _receipt_ttl_sec()
    except OSError:
        return None
    try:
        p.unlink()
    except OSError:
        pass
    return text if (text and fresh) else None


def tuck_in_marker() -> str:
    """Marker inside the free-round line the cortex watchdog appends to
    wake_signal.log (surfaces down the ear channel). A prompt carrying it is a
    machine line, not a real user message — excluded from the user-wake reset."""
    cx = config.load().get("cortex", {}) or {}
    return str(cx.get("tuck_in_marker") or "[NEW ROUND]").strip()


# ── Covert machine-marker bodies (FUSE / CTL). Cortex writes only the marker line
#    to wake_signal.log; the ear Monitor surfaces just the marker, and these full
#    instruction bodies are injected INVISIBLY via UserPromptSubmit additionalContext
#    so she never SEES the instruction text in the cortex window. Config-first. ──

_FUSE_MARKER = "[FUSE]"
_CTL_MARKER = "[CTL]"

_DEFAULT_FUSE_PROMPT = (
    "Session context fused. Update {handoff} before rotate. Add todo if any. "
    "lie_down(rotate=True)"
)

_DEFAULT_CTL_SLEEP = (
    "Wrap up this turn: {rotate}lie_down(next_wake_min={mins}{rotate_arg}{human_arg})."
)

_CTL_ARGS_RE = _re.compile(
    r"mins=(\d+)\s+rotate=(true|false)(?:\s+human=(true|false))?", _re.IGNORECASE)


def fuse_prompt_text() -> str | None:
    """FUSE instruction body injected as additionalContext when the cortex ear
    surfaces a [FUSE] marker turn. Override [cortex].fuse_prompt_text; blank/None
    -> inject nothing (marker-only). {handoff} = this shell's own handoff path."""
    cx = config.load().get("cortex", {}) or {}
    if "fuse_prompt_text" in cx:
        txt = str(cx.get("fuse_prompt_text") or "").strip()
        if not txt:
            return None
    else:
        txt = _DEFAULT_FUSE_PROMPT
    return _fill_handoff(txt)


def _fill_handoff(text: str) -> str:
    """Render {handoff} as this shell's own handoff path (shared by the lie_down
    nudge + the FUSE body); bare "handoff" when the path is unresolvable."""
    p = _cortex_handoff_path()
    return text.replace("{handoff}", str(p) if p is not None else "handoff")


def ctl_sleep_text(marker_line: str) -> str | None:
    """CTL sleep instruction body injected as additionalContext when the cortex
    ear surfaces a [CTL] marker turn. The marker line carries the dynamic args
    ('mins=N rotate=true|false [human=true|false]'); this renders
    {mins}/{rotate}/{rotate_arg}/{human_arg} from them. human=true (an explicit
    /ct-sleep minutes choice) renders human_override=True into the lie_down(...)
    call so it pierces the rotate clamp band end to end. Override
    [cortex].ctl_sleep_text; blank/None -> inject nothing."""
    cx = config.load().get("cortex", {}) or {}
    if "ctl_sleep_text" in cx:
        tmpl = str(cx.get("ctl_sleep_text") or "").strip()
        if not tmpl:
            return None
    else:
        tmpl = _DEFAULT_CTL_SLEEP
    m = _CTL_ARGS_RE.search(marker_line or "")
    mins = m.group(1) if m else ""
    rotate = bool(m and m.group(2).lower() == "true")
    human = bool(m and m.group(3) and m.group(3).lower() == "true")
    rot = "write your handoff then " if rotate else ""
    rotate_arg = ", rotate=true" if rotate else ""
    human_arg = ", human_override=True" if human else ""
    return (tmpl.replace("{mins}", str(mins))
            .replace("{rotate_arg}", rotate_arg)
            .replace("{human_arg}", human_arg)
            .replace("{rotate}", rot))


_HARNESS_TAG_RE = _re.compile(r"^<[a-z][a-z0-9_-]*>")


def is_compact_injection(prompt: str) -> bool:
    """True when the prompt is an auto-compact continuation injection (Claude
    Code replays the archived transcript as a user-role turn on a high-token
    session). Matched narrowly: a configured compact marker must appear within
    the leading compact_marker_head_chars of the prompt. Never fires on ordinary
    chat — the markers are literal harness banners. Config-driven so the exact
    banner text stays customisable ([cortex].compact_markers)."""
    if not prompt:
        return False
    cx = config.load().get("cortex", {}) or {}
    markers = cx.get("compact_markers") or []
    if not markers:
        return False
    try:
        head_len = int(cx.get("compact_marker_head_chars") or 200)
    except (TypeError, ValueError):
        head_len = 200
    head = prompt.lstrip()[:head_len]
    return any(str(m) in head for m in markers if m)


# Narrowed decoration class (see marrow.transcript._MARKER_GLYPH): misc-technical
# + symbols + dingbats (⏳U+23F3 ☀️U+2600 ⚙️U+2699), arrows, emoji, VS16, ZWJ.
# EXCLUDES CJK/kana/hangul so a Chinese line quoting a marker keeps its lead char
# and is never misread as a machine line (dropped from the user-wake reset).
_MARKER_LEAD_RE = _re.compile(
    r"^\s*(?:[\U00002300-\U000027BF\U00002B00-\U00002BFF"
    r"\U0001F300-\U0001FAFF\U0000FE0F\U0000200D]\s*){0,3}")

# Ear-Monitor delivery envelope: the free-round/tuck-in block arrives WRAPPED in
# a harness task-notification, so the marker line reads `<event>⏳ [NEW ROUND] …`
# — the marker no longer sits at raw line start. Strip a leading run of XML-ish
# envelope tags (`<event>`, `<task-notification>`) before the glyph strip so the
# wrapped shape still counts as a machine line. Anti-false-positive kept: a prose
# line never opens with such a tag, so a mid-sentence quote still never matches.
_ENVELOPE_LEAD_RE = _re.compile(r"^\s*(?:</?[a-z][a-z0-9_-]*>\s*){0,3}")


def machine_markers() -> tuple[str, ...]:
    """Line-start machine-marker family ([cortex].machine_markers) — the wake
    bell, free-round, fuse, ctl and ⚙️ [CMD ct-*] slash-command bodies.
    Shared by the user-wake reset (below) and marrow.transcript ingestion."""
    cx = config.load().get("cortex", {}) or {}
    return tuple(str(m) for m in (cx.get("machine_markers") or []) if str(m))


def _line_starts_with(prompt: str, marker: str) -> bool:
    """True iff ANY line of *prompt* begins with *marker* after a tolerated
    leading envelope-tag run (<event>/<task-notification>) and decoration run
    (⚙️/⏳/☀️). Machine wake bells and tuck-in/free-round blocks always line-start
    their marker (cortex window._append_signal_line, watchdog._write_tuck_in_line
    — note ABOVE, marker as the final line); when delivered by the ear Monitor
    they arrive wrapped, marker line = `<event>⏳ [NEW ROUND] …`. A real user
    message merely quoting the marker mid-sentence never matches."""
    if not marker:
        return False
    for line in prompt.splitlines() or [prompt]:
        head = _ENVELOPE_LEAD_RE.sub("", line, count=1)
        if _MARKER_LEAD_RE.sub("", head, count=1).startswith(marker):
            return True
    return False


# Public alias — the marrow hook's tuck-in de-dup guard shares this shape check
# so a real user prompt quoting the marker is not swallowed by the early return.
line_starts_with_marker = _line_starts_with


def _starts_with_machine_marker(prompt: str) -> bool:
    """True iff *prompt* BEGINS with a machine marker after a tolerated leading
    emoji/whitespace run (⚙️ [CMD ...], ⏳ [NEW ROUND] ...). Line-start only, so a
    real message quoting a marker mid-body is never misread as machine."""
    markers = machine_markers()
    if not markers:
        return False
    head = _MARKER_LEAD_RE.sub("", prompt, count=1)
    return any(head.startswith(m) for m in markers)


def is_machine_line(prompt: str) -> bool:
    """True when the incoming cortex-window prompt is a machine line arriving
    down the ear channel (wake bell / monitor death / free-round / fuse /
    ctl / slash-command body), NOT a real user message. The user-wake reset must
    fire ONLY on real user messages."""
    if not prompt:
        return True
    p = prompt
    # Harness-style tag at the very start (e.g. <task-notification>,
    # <system-reminder>, future tags): the harness flushes these through the
    # prompt pipeline on events like a background task ending. A real user
    # never opens a message with such a tag.
    if _HARNESS_TAG_RE.match(p.lstrip()):
        return True
    # Critical markers always covered even if machine_markers is overridden.
    # Line-start (not substring): a machine wake bell / tuck-in always opens its
    # line with the marker; a real user prompt quoting it mid-sentence ("did the
    # [NEW ROUND] path fire?") stays a user message.
    # New wake bell = human text: recognized via the receipt sidecar / template
    # prefix (match_wake_bell).
    try:
        if match_wake_bell(p) is not None:
            return True
    except Exception:
        pass
    tm = tuck_in_marker()
    if tm and _line_starts_with(p, tm):
        return True
    if _starts_with_machine_marker(p):
        return True
    if is_compact_injection(p):
        return True
    return False


# ── user-wake reset (Item 3): a real user message in a cortex window flips the
# session awake, kills the pending alarm + sentinel, and (re)spawns the watchdog.
# marrow venv cannot import cortex, so wake_state.json is manipulated directly
# with the SAME flock + atomic-replace protocol as cortex.wake_state. ──────────

def _cortex_wake_state_path() -> Path:
    return _cortex_path("wake_state_file", "state/wake_state.json")


def _wake_audit(action: str, reason: str = "", detail: str = "") -> None:
    """Append one audit line whenever a wake-state alarm is destroyed (sentinel
    kill / floor clear / awake flip). Format: ISO-ts\taction\treason\tdetail.
    Best-effort — an audit failure never breaks the reset path. Path from
    [cortex].wake_audit_log_file (default wake_audit.log under cortex home)."""
    try:
        from datetime import timezone as _tz
        path = _cortex_path("wake_audit_log_file", "state/wake_audit.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(_tz.utc).isoformat()
        line = "\t".join((ts, action, reason.replace("\t", " "),
                          str(detail).replace("\t", " ")))
        with open(path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _cortex_watchdog_pidfile() -> Path:
    return _cortex_path("watchdog_pidfile", "state/watchdog.pid")


class WakeStateLockTimeout(RuntimeError):
    """_wake_state_lock(required=True) could not take the lock in time."""


@_contextlib.contextmanager
def _wake_state_lock(p: Path, *, required: bool = False):
    """Blocking exclusive flock on <wake_state>.lock, byte-compatible with
    cortex.wake_state._flock (same sibling .lock, same protocol). Best-effort by
    default: an unacquirable lock still proceeds (matches cortex's fallback).

    required=True raises WakeStateLockTimeout instead of proceeding unlocked —
    for the replay read/advance paths, where an unlocked cursor read-modify-write
    can lose an update and re-inject rows another writer already consumed.
    Callers skip that round; the cursor is untouched, so its rows replay next
    round.

    COUPLED: base = marrow [cortex].wake_state_file / [cortex].home. Cortex's
    side (wake_state.lock_path) resolves from cortex [paths].wake_state_file /
    [paths].cortex_home — override one without the other and the two lock files
    split (silent lost update)."""
    lp = p.with_suffix(".lock")
    fd = None
    got = False
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        if required and not got:
            raise WakeStateLockTimeout(str(lp))
        yield
    finally:
        if fd is not None:
            if got:
                with _contextlib.suppress(OSError):
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
            with _contextlib.suppress(OSError):
                os.close(fd)


def _wake_state_load(p: Path) -> dict:
    try:
        if p.exists():
            return _json_ws.loads(p.read_text())
    except (OSError, ValueError):
        pass
    return {}


def _ws_ensure_epoch(d: dict) -> None:
    """Mirror of cortex.wake_state._ensure_epoch: initialise the cancellation
    epoch (gen=0 + random state_id) on first touch so both sides agree. state_id
    defends the delete/recreate ABA. Must stay byte-compatible with the cortex
    side (same field names, same secrets.token_hex(8) shape)."""
    import secrets as _secrets
    if not isinstance(d.get("gen"), int):
        d["gen"] = 0
    if not d.get("state_id"):
        d["state_id"] = _secrets.token_hex(8)


def _wake_state_save(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(_json_ws.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


# ── per-shell state file (<shell_state_dir>/<shell>.json) ─────────────────────
# The non-cli shells' ledger, written by their host (tg bridge). The cli shell
# keeps wake_state.json. Same flock + atomic-replace protocol as above.

_SHELL_STATE_KEYS = ("session_id", "next_wake_at", "last_note_ts", "pending_note",
                     "rotate_pending", "last_note_row_id", "pending_note_row_id")


def _shell_state_path(shell: str | None = None) -> Path:
    """<[cortex].shell_state_dir>/<shell>.json. Empty config value =
    <marrow DATA_DIR>/state/shells. shell=None -> this window's shell id;
    ValueError when that id is unusable (bad MARROW_CORTEX), so a malformed
    marker can never claim another shell's ledger."""
    shell = shell or _cortex_shell_id()
    if not shell:
        raise ValueError("no usable cortex shell id")
    cx = config.load().get("cortex", {}) or {}
    raw = str(cx.get("shell_state_dir") or "").strip()
    base = (Path(raw).expanduser() if raw
            else config.DATA_DIR / "state" / "shells")
    return base / f"{shell}.json"


def shell_state_read(shell: str | None = None) -> dict:
    """Read a shell's state file under the flock. Missing/corrupt file -> {}."""
    p = _shell_state_path(shell)
    with _wake_state_lock(p):
        return _wake_state_load(p)


def shell_state_write(data: dict, shell: str | None = None) -> Path:
    """Merge the contract keys of `data` (_SHELL_STATE_KEYS) into the shell's
    state file under the flock; a None value drops its key. Unknown keys are
    ignored. Returns the file path."""
    p = _shell_state_path(shell)
    with _wake_state_lock(p):
        d = _wake_state_load(p)
        for k in _SHELL_STATE_KEYS:
            if k not in data:
                continue
            if data[k] is None:
                d.pop(k, None)
            else:
                d[k] = data[k]
        _wake_state_save(p, d)
    return p


def next_wake_at(shell: str) -> str | None:
    """Raw next_wake_at ISO string for `shell`, or None when unset/no alarm.
    cli reads the cortex wake_state.json ledger (same lock as the hooks pkg's read);
    every other shell reads <shell_state_dir>/<shell>.json via shell_state_read."""
    if shell == "cli":
        p = _cortex_wake_state_path()
        with _wake_state_lock(p):
            d = _wake_state_load(p)
    else:
        d = shell_state_read(shell)
    v = d.get("next_wake_at")
    return str(v) if v else None


def _daemon_socket_path() -> Path:
    """The cortex wake daemon's kick socket. cortex.toml [daemon].socket_path,
    or <cortex home>/state/cortex-daemon.sock when unset — same resolution as
    cortex.config.daemon_socket_path (marrow's venv cannot import cortex)."""
    raw = str(_cortex_toml_section("daemon", "socket_path", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _cortex_home() / "state" / "cortex-daemon.sock"


def _notify_daemon() -> None:
    """Best-effort kick to the cortex wake daemon so it re-reads the ledger
    immediately instead of waiting for its next tick. Pure notification: never
    replaces a state write, and a missing socket / dead daemon is a silent
    no-op. Stdlib socket only."""
    import socket as _socket
    shell = str(_cortex_toml_section("daemon", "shell", "cli") or "cli")
    try:
        timeout = float(_cortex_toml_section("daemon", "kick_timeout_sec", 1.0))
    except (TypeError, ValueError):
        timeout = 1.0
    try:
        path = _daemon_socket_path()
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall((shell + "\n").encode("utf-8"))
    except (OSError, ValueError):
        pass


def _kill_pid(pid_val) -> None:
    try:
        pid = int(pid_val)
    except (TypeError, ValueError):
        return
    if pid <= 0 or pid == os.getpid():
        return
    import signal as _signal
    try:
        os.kill(pid, _signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _pid_alive(pid_val) -> bool:
    try:
        pid = int(pid_val)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _spawn_watchdog_if_absent() -> None:
    """(Re)spawn the cortex watchdog if its pidfile is missing or the pid is
    dead. Uses the cortex venv/repo subprocess (marrow cannot import cortex)."""
    pf = _cortex_watchdog_pidfile()
    try:
        alive = pf.exists() and _pid_alive(pf.read_text().strip())
    except OSError:
        alive = False
    if alive:
        return
    py, root = _cortex_paths()
    if not py or not root:
        return
    py = str(Path(py).expanduser())
    root = str(Path(root).expanduser())
    log = pf.with_suffix(".log")
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        f = open(log, "a")
        proc = subprocess.Popen(
            [py, "-m", "cortex.watchdog"],
            cwd=root, stdout=f, stderr=f, stdin=subprocess.DEVNULL,
            start_new_session=True, env={**os.environ},
        )
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(str(proc.pid))
    except OSError:
        pass


def _latest_wake_log_id() -> int | None:
    """id of the most recent open wake row (ct_wake_log where wake=1), so a
    user-wake reset can rejoin the accounting chain a later lie_down updates.
    Best-effort: None on any error / empty / missing table — never raises."""
    import sqlite3
    try:
        dbp = config.db_path()
        conn = sqlite3.connect(dbp, timeout=30)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT id FROM ct_wake_log WHERE wake=1 "
            "ORDER BY id DESC LIMIT 1").fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _log_user_wake_row() -> int | None:
    """Insert a wake=1 activation row tagged 'user' for a user-triggered cortex
    wake (a real user message flipping a dormant window awake), so the wakeup
    note's "Last wake" segment counts it — the marrow user-wake path wrote NO
    wake row before, so "Last wake" skipped every user wake (BUG A). Mirrors
    cortex.integration.log_activation_wake_row (marrow venv cannot import
    cortex). force_slept is NULL until a later lie_down sets it, so force_slept
    auto-rate stats are unaffected. Best-effort: None on any error — never
    raises, never blocks the wake. This write logs a machine wake row only; it
    is NOT user activity for any silence/presence timer."""
    import sqlite3
    from datetime import timezone as _tz
    try:
        dbp = config.db_path()
        conn = sqlite3.connect(dbp, timeout=30)
    except sqlite3.Error:
        return None
    try:
        ts = datetime.now(_tz.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons, shell) "
            "VALUES (?, 1, 0, 'user', 'cli')", (ts,))
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _cortex_user_wake_reset(inp: dict) -> None:
    """Real user message in a cortex window -> flip awake, kill the pending
    alarm (next_wake_at ledger + sentinel), mark the user reply, reset the
    free-round silence cycle (drop any pending kick carrier + last-injection
    marker so it re-arms fresh from this message), and ensure a watchdog is
    alive. Fast + idempotent: already-awake + watchdog-alive collapses to cheap
    no-op writes. Cortex session only; the caller has already excluded machine
    lines (is_machine_line).

    cli shell ONLY. wake_state.json is the cli window's alarm; a non-cli shell
    (tg/wx) is host-driven and its host already cancels its own booked wake on
    every inbound user message (synapse_tg.shell.on_user_message). Writing here
    from a tg turn cleared the CLI ledger's next_wake_at and flipped it awake, so
    cortex reconcile then read the closed cli window as "accidental close of an
    awake window" and respawned it mid-sleep."""
    if not _shell_enabled() or _cortex_shell_id() != "cli":
        return
    p = _cortex_wake_state_path()
    tpath = inp.get("transcript_path") if isinstance(inp, dict) else None
    trigger = ""
    if isinstance(inp, dict):
        trigger = str(inp.get("prompt") or "").strip().replace("\n", " ")[:80]
    sentinel_pid = None
    flipped_awake = False
    old_gen = new_gen = None
    awake_since = None
    with _wake_state_lock(p):
        d = _wake_state_load(p)
        _ws_ensure_epoch(d)
        was_awake = bool(d.get("awake"))
        if not was_awake:
            from datetime import timezone as _tz
            d["awake"] = True
            awake_since = datetime.now(_tz.utc).isoformat()
            d["awake_since"] = awake_since
            # wake_log_id is bound AFTER this lock releases (P2 fix): the
            # ct_wake_log INSERT (30s busy_timeout + fallback query) must never
            # run while this flock is held, or a slow/contended db stalls the
            # user's prompt and starves cortex's own 5s-deadline strict lock on
            # the SAME wake_state file (fail-closed on concurrent wait/lie_down).
            # Left unset here; the write happens below, outside the lock.
            if tpath:
                d["transcript"] = str(tpath)
            flipped_awake = True
        # BUMP gen on EVERY real user message (even when already awake): a user
        # arrival is a cancellation epoch — it invalidates every in-flight alarm
        # token (a still-running lie_down's sentinel, a stale tick snapshot).
        # This is the single most important bump: BUG A fired because the OLD
        # sentinel pid was killed but a NEWer sentinel armed after the reset
        # survived; bumping here makes that newer sentinel's fire-time epoch
        # check fail, so it exits silently even though its pid was never chased.
        old_gen = int(d["gen"])
        d["gen"] = old_gen + 1
        new_gen = d["gen"]
        d["user_replied_this_wake"] = True
        # Stamp the real user-message time so presence gates (e.g. the 120k
        # show nudge) can hold while the user is actively chatting and fire once
        # they have gone silent. Caller already excluded machine lines, so this
        # never counts an injected/machine turn as user presence.
        from datetime import timezone as _tz_u
        d["last_user_msg_ts"] = datetime.now(_tz_u.utc).isoformat()
        # A user arrival resets the free-round silence cycle: drop the pending
        # kick carrier + the last-injection marker so the cycle re-arms fresh
        # from this message.
        d.pop("kick_round", None)
        d.pop("tuck_pending", None)
        # Clear the durable next-wake ledger too: a user arrival cancels any
        # scheduled alarm, so the tick reconcile must not later fire a stale
        # next_wake_at (it is re-armed by the next lie_down).
        d.pop("next_wake_at", None)
        sentinel_pid = d.pop("sentinel_pid", None)
        _wake_state_save(p, d)
    if flipped_awake:
        _wake_audit("awake_flip", trigger, "false->true")
    if old_gen is not None:
        _wake_audit("user_reset_gen", trigger, f"gen {old_gen}->{new_gen}")
    # Kill the pending alarm: the exact-time sentinel.
    if sentinel_pid is not None:
        _wake_audit("sentinel_kill", trigger, f"pid={sentinel_pid}")
    _kill_pid(sentinel_pid)
    _spawn_watchdog_if_absent()
    # Notify-only: the alarm was already cancelled by the state write above;
    # this just tells a live daemon to re-read now instead of on its next tick.
    _notify_daemon()
    # BUG A wake-row log, done OUTSIDE the wake-state lock (P2 fix, see above):
    # log this user-triggered wake as its own ct_wake_log row so the wakeup
    # note's "Last wake" counts it. Best-effort — bind wake_log_id back only if
    # this is still the SAME wake (awake_since unchanged); a lie_down / newer
    # flip superseding it in the gap just means this wake's tokens go unbound,
    # never a correctness issue, and never re-blocks a live mutation.
    if flipped_awake:
        wid = _log_user_wake_row() or _latest_wake_log_id()
        if wid is not None:
            with _wake_state_lock(p):
                d2 = _wake_state_load(p)
                if d2.get("awake") and d2.get("awake_since") == awake_since:
                    d2["wake_log_id"] = wid
                    _wake_state_save(p, d2)


# ── external wake (cortex.kick) ────────────────────────────────────────────────

def _cortex_toml_path() -> Path:
    """cortex.toml lives beside marrow's config (shared config dir). Read
    directly (marrow venv cannot import cortex) for the few knobs marrow needs
    from cortex's own config."""
    return Path(config.db_path()).parent / "cortex.toml"


def _cortex_toml_section(section: str, key: str, default):
    """One value from cortex.toml [section] (dotted `section` walks nested TOML
    tables, e.g. "wake.watchdog" -> data["wake"]["watchdog"]). Tolerant: missing
    file/key -> default. marrow venv cannot import cortex, so the few numbers the
    tool descriptions render (lie_down clamps) are read straight from the shared
    cortex.toml."""
    import tomllib
    try:
        p = _cortex_toml_path()
        if not p.exists():
            return default
        with p.open("rb") as f:
            data = tomllib.load(f)
        node = data
        for part in section.split("."):
            node = (node or {}).get(part, {})
        v = (node or {}).get(key)
        return v if v is not None else default
    except (OSError, ValueError):
        return default


def kick_cortex(kind: str, note_id=None, minutes=None) -> None:
    """Fire cortex.kick as a detached, fire-and-forget subprocess in the cortex
    venv (marrow venv cannot import cortex). Never blocks the caller; any launch
    failure is swallowed. `kind` in {reply, timeout, note}."""
    py, root = _cortex_paths()
    if not py or not root:
        return
    py = str(Path(py).expanduser())
    root = str(Path(root).expanduser())
    argv = [py, "-m", "cortex.kick", "--kind", str(kind)]
    if note_id is not None:
        argv += ["--note-id", str(note_id)]
    if minutes is not None:
        argv += ["--minutes", str(minutes)]
    try:
        log = _cortex_path("wake_audit_log_file", "state/wake_audit.log").with_suffix(".kick.log")
        log.parent.mkdir(parents=True, exist_ok=True)
        f = open(log, "a")
        subprocess.Popen(
            argv, cwd=root, stdout=f, stderr=f, stdin=subprocess.DEVNULL,
            start_new_session=True, env={**os.environ},
        )
    except OSError:
        pass


_HANDOFF_LOG_DATE_RE = _re.compile(r"^###\s*(\d{4}-\d{2}-\d{2})\b")
_HANDOFF_UNCHECKED_RE = _re.compile(r"^\s*(?:-\s*)?\[\s*\]")
_HANDOFF_HEADING_RE = _re.compile(r"^#{1,6}\s")


def _handoff_headings() -> tuple[str, str, str]:
    """(todo, activity, carry) headings — the sole code<->template contract."""
    cx = config.load().get("cortex", {}) or {}
    return (
        cx.get("handoff_todo_heading") or "## 待办",
        cx.get("handoff_activity_heading") or "## 日志",
        cx.get("handoff_carry_heading") or "### 前情",
    )


def _section_span(lines: list[str], heading: str) -> tuple[int, int] | None:
    """Index of *heading* to the next heading (exclusive). None if absent.
    End = EOF when no later heading exists."""
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == heading.strip():
            start = i
            break
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        if _HANDOFF_HEADING_RE.match(lines[j]):
            return start, j
    return start, len(lines)


def _handoff_page_range(old_text: str) -> tuple[str | None, str]:
    """(start_date, end_date) for the archive name. start = first
    `### YYYY-MM-DD` in the page's log; end = today (rotate day)."""
    end = datetime.now(config.get_tz()).date().isoformat()
    for ln in old_text.splitlines():
        m = _HANDOFF_LOG_DATE_RE.match(ln)
        if m:
            return m.group(1), end
    return None, end


def _handoff_archive_stem(shell: str, old_text: str) -> str:
    """{shell}-{date} single day, {shell}-{date~date} range."""
    start, end = _handoff_page_range(old_text)
    if start is None or start == end:
        return f"{shell}-{end}"
    _, e_md = start[5:], end[5:]  # strip year for the range tail
    return f"{shell}-{start}~{e_md}" if start[:4] == end[:4] else f"{shell}-{start}~{end}"


def _build_fresh_page(template_text: str, todos: list[str], carry: list[str]) -> str:
    """Fresh template with carried unchecked todos merged into the todo section
    and the last activity lines placed under the carry heading."""
    todo_h, _act_h, carry_h = _handoff_headings()
    lines = template_text.splitlines()

    if todos:
        span = _section_span(lines, todo_h)
        if span is not None:
            _s, e = span
            lines[e:e] = todos
            # recompute after insert so the carry heading index stays valid
    if carry:
        span = _section_span(lines, carry_h)
        if span is not None:
            s, _e = span
            lines[s + 1:s + 1] = carry
    return "\n".join(lines) + ("\n" if template_text.endswith("\n") else "")


def _cortex_page_turn(p: Path, old_text: str) -> None:
    """Archive the full page and replace it with a fresh template carrying
    unchecked todos + the last handoff_carry_lines activity lines. Best-effort:
    any failure leaves the file in place (the next SessionStart retries)."""
    cx = config.load().get("cortex", {}) or {}
    home_p = Path(cx.get("home") or "~/.config/marrow/cortex").expanduser()
    archive_dir = home_p / (cx.get("handoff_archive_dir") or "handoff_archive")
    template_p = home_p / (cx.get("handoff_template_file") or "handoff_template.md")
    carry_n = int(cx.get("handoff_carry_lines", 10) or 0)
    todo_h, act_h, _carry_h = _handoff_headings()
    try:
        template_text = template_p.read_text(encoding="utf-8")
    except OSError:
        return  # no template — leave the file in place

    old_lines = old_text.splitlines()
    # Unchecked todo lines from the old todo section.
    todos: list[str] = []
    span = _section_span(old_lines, todo_h)
    if span is not None:
        s, e = span
        todos = [ln for ln in old_lines[s + 1:e]
                 if _HANDOFF_UNCHECKED_RE.match(ln)]
    # Last N non-blank activity lines from the old activity section
    # (activity_heading to EOF; sub-headings excluded, their content kept).
    carry: list[str] = []
    if carry_n > 0:
        for i, ln in enumerate(old_lines):
            if ln.strip() == act_h.strip():
                body = [x for x in old_lines[i + 1:]
                        if x.strip() and not _HANDOFF_HEADING_RE.match(x)]
                carry = body[-carry_n:]
                break

    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        stem = _handoff_archive_stem(_cortex_shell_id(), old_text)
        dest = archive_dir / f"{stem}.md"
        n = 1
        while dest.exists():
            n += 1
            dest = archive_dir / f"{stem}-{n}.md"
        shutil.move(str(p), str(dest))

        p.write_text(_build_fresh_page(template_text, todos, carry),
                     encoding="utf-8")
    except OSError:
        pass


def _cortex_handoff_page_turn_if_stale() -> None:
    """Line-count page-turn side effect for a fresh cortex window: own handoff
    over handoff_max_lines -> archive + fresh template carrying todos/activity.
    First cli run migrates a legacy handoff.md -> handoff-cli.md (one-time mv).
    Under the line cap or unreadable -> no-op. No content returned; the cortex
    CLAUDE.md memory import is the sole read path."""
    p = _cortex_handoff_path()
    if p is None:
        return
    # One-time legacy migration: handoff.md -> handoff-cli.md (cli only).
    if _cortex_shell_id() == "cli" and not p.exists():
        legacy = p.parent / "handoff.md"
        if legacy.exists():
            try:
                shutil.move(str(legacy), str(p))
            except OSError:
                pass
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    if not text.strip():
        return
    cx = config.load().get("cortex", {}) or {}
    max_lines = int(cx.get("handoff_max_lines", 150) or 0)
    if max_lines > 0 and len(text.splitlines()) > max_lines:
        _cortex_page_turn(p, text)


def _cortex_handoff_header(ws: dict) -> str:
    """Build the 'HH:mm-HH:mm | SID xxxxxxxx' line appended to show_text so
    cortex knows the time range and session id to write into its handoff."""
    from datetime import timezone as _tz
    since_raw = ws.get("awake_since")
    since_str = "??:??"
    if since_raw:
        try:
            since_dt = datetime.fromisoformat(since_raw)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=_tz.utc)
            since_str = since_dt.astimezone(config.get_tz()).strftime("%H:%M")
        except (ValueError, TypeError):
            pass
    now_str = datetime.now(config.get_tz()).strftime("%H:%M")
    transcript_raw = ws.get("transcript")
    sid = "unknown"
    if transcript_raw:
        try:
            sid = Path(str(transcript_raw)).stem[:8]
        except (OSError, ValueError):
            pass
    return f"{since_str}-{now_str} | SID {sid}"


def _user_active_within(ws: dict, minutes: int) -> bool:
    """True when the wake_state records a real user message younger than
    *minutes*. No stamp = treat as not-active (silence gate fires). Machine turns
    never write last_user_msg_ts, so they never count as presence."""
    raw = ws.get("last_user_msg_ts")
    if not raw:
        return False
    from datetime import timezone as _tz
    try:
        dt = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    age = (datetime.now(_tz.utc) - dt).total_seconds()
    return age < minutes * 60


def _shell_presence_state() -> dict:
    """Presence + handoff-header view for THIS window's shell. cli reads
    wake_state.json; a non-cli shell reads its OWN host-written ledger and is
    normalised onto the same keys (the host writes last_user_ts / session_id), so
    a tg window never judges presence off the cli shell's state. Missing/corrupt
    -> {}."""
    if _cortex_shell_id() == "cli":
        try:
            return _wake_state_load(_cortex_wake_state_path())
        except Exception:
            return {}
    try:
        st = shell_state_read()
    except Exception:
        return {}
    out: dict = {}
    if st.get("last_user_ts"):
        out["last_user_msg_ts"] = st["last_user_ts"]
    if st.get("session_id"):
        out["transcript"] = f"{st['session_id']}.jsonl"
    return out


# Window-occupancy 亮牌 nudge (mechanism-defining copy). show_tokens is the
# off-switch (<= 0 = inert) and fills {show_k} — the copy never restates the
# threshold, so config stays the single source.
_SHOW_TEXT = (
    "Context ≥{show_k}k - If not actively chatting with user, handoff within 3 "
    "turns and lie_down(rotate=True) to clear (rotate unlocks a short next_wake, "
    "≥16min). If mid-conversation: carry on, the fuse is the backstop."
)


def _cortex_show_context(tpath: str) -> str:
    """Cortex-only (MARROW_CORTEX=1) window-occupancy 亮牌 at show_tokens (soft,
    ahead of the cortex fuse). Suppressed when user is chatting
    (user_replied_this_wake). Empty for normal sessions, below threshold,
    or when show_tokens <= 0 (the off-switch)."""
    if not _shell_enabled():
        return ""
    cr = config.load().get("cortex_rotate", {}) or {}
    show = int(cr.get("show_tokens", 160_000) or 0)
    if show <= 0:
        return ""
    text = _SHOW_TEXT.format(show_k=round(show / 1000))
    from .hooks import _window_tokens_from_transcript
    if _window_tokens_from_transcript(tpath) < show:
        return ""
    ws = _shell_presence_state()
    # Presence gate: hold the nudge while the user's last real message is younger
    # than show_silent_min (mid-chat — the 180k fuse is the backstop). It retries
    # on a later turn while tokens stay over threshold. Falls back to the boolean
    # user_replied_this_wake when no timestamp is stored (legacy state / gate off).
    silent_min = int(cr.get("show_silent_min", 0) or 0)
    if silent_min > 0 and _user_active_within(ws, silent_min):
        return ""
    if silent_min <= 0 and ws.get("user_replied_this_wake"):
        return ""
    header = _cortex_handoff_header(ws)
    if header:
        text = f"{text}\nHandoff section header: {header}"
    return text
