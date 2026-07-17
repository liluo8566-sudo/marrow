"""Cortex bridge — the optional autonomous-wake ("cortex") organs, extracted
from daemon.py / hooks.py / llm.py into one module.

Cortex is an optional layer that lives in a SEPARATE repo (~/CC-Lab/cortex);
this module is marrow's side of that bridge: the MCP tools it exposes to a
cortex session, the SessionStart / PreToolUse / turn_inject hook branches that
only a cortex window takes, and the full-env LLM runner the cortex repo calls
back into.

Two independent gates, both must be open for any cortex behaviour:

  1. [cortex].enabled (config, default false) — "are the organs installed".
     enabled() == False  => register() is a no-op (no cortex tool reaches
     the MCP schema) and every hook helper here short-circuits to its inert
     value. A clean marrow install shows ZERO cortex behaviour.
  2. MARROW_CORTEX (env) — "is this the cortex session". Set by the cortex
     runner (see run_claude_cortex below) on the spawned marrow subprocess.
     The lie_down / wait / say tools additionally require it at import time;
     the hook branches require it at call time.

So enabled == organs installed; MARROW_CORTEX == this is the cortex window.

This is a verbatim MOVE of the pre-existing cortex code — names, logic and
behaviour are unchanged. daemon/hooks/llm keep one-line, enabled-gated call
sites into here.
"""
from __future__ import annotations

import os
import re as _re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import Field

from . import config, storage


# ── gates ─────────────────────────────────────────────────────────────────────

def enabled() -> bool:
    """Master switch: [cortex].enabled in config (default false when absent)."""
    return bool(config.load().get("cortex", {}).get("enabled", False))


def is_cortex_session() -> bool:
    """True inside a cortex-spawned marrow window (MARROW_CORTEX in env)."""
    return bool(os.environ.get("MARROW_CORTEX"))


# Import-time capture of the cortex-session env marker, mirroring the original
# daemon._CORTEX: the lie_down / wait / say tools register into the MCP schema
# only when this daemon subprocess was spawned by a cortex window (the window
# sets MARROW_CORTEX explicitly before spawn). Normal sessions never see them.
_CORTEX = bool(os.environ.get("MARROW_CORTEX"))


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
    """Our wishlist — personal wishes & cravings (hers and yours), promises
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


def lie_down(next_wake_min: float, rotate: bool = False,
             mode: str | None = None) -> dict:
    # Description rendered from cortex config at register() (C9); see
    # _lie_down_doc. Kept minimal here — FastMCP reads __doc__ at registration.
    # mode='night' = the night package (forces rotate, clamps N to the night
    # band, sets the persistent night flag). Cleared by the morning kick.
    """lie_down(next_wake_min=N)."""
    args = ["--next-wake-min", str(next_wake_min)]
    if rotate:
        args += ["--rotate"]
    if mode:
        args += ["--mode", str(mode)]
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
        except (ValueError, TypeError):
            pass
    return out


def wait(minutes: float) -> dict:
    # Description rendered from cortex config at register() (C10); see _wait_doc.
    """wait(N)."""
    return _run_cortex_module("cortex.wait", ["--minutes", str(minutes)])


def say() -> dict:
    """Urgent only: quiet notification ping for her attention (no focus steal).
    Normal in-window talk needs no say — she reads when free."""
    return _run_cortex_module("cortex.say")


# ── daemon registration ───────────────────────────────────────────────────────

# DB the tools read/write. Set at register() time from the daemon's own _DB so
# it tracks the same source the daemon resolved at import; tests patch this.
_DB = config.db_path()


def _lie_down_doc() -> str:
    """C9 (user-final): lie_down description with all four clamp numbers rendered
    from cortex config — day bounds [wake].next_wake_min/max, night bounds
    [night].floor_min/max. Never hardcoded in the string."""
    day_min = int(_cortex_toml_section("wake", "next_wake_min", 21))
    day_max = int(_cortex_toml_section("wake", "next_wake_max", 240))
    night_min = int(_cortex_toml_section("night", "floor_min", 120))
    night_max = int(_cortex_toml_section("night", "floor_max", 360))
    return (f"lie_down(next_wake_min=N) "
            f"[N={day_min}-{day_max} (Day); {night_min}-{night_max} (Night)]")


def _wait_doc() -> str:
    """C10 (user-final): wait description with the wait clamp numbers rendered
    from cortex config [wake].wait_min/wait_max, and the chat-tier auto-timer
    length from [wake.watchdog].silent_max_min. Never hardcoded."""
    lo = int(_cortex_toml_section("wake", "wait_min", 1))
    hi = int(_cortex_toml_section("wake", "wait_max", 20))
    auto = int(_cortex_toml_section("wake.watchdog", "silent_max_min", 20))
    return (f"wait(N) [N={lo}-{hi}] — one wait per wake. Each user reply "
            f"triggers a {auto}-min auto timer and resets all other timers. "
            f"The auto timer also counts — expiry brings the 3-choice menu.")


def register(marrow_tool, db: str | None = None) -> None:
    """Install the cortex MCP tools onto the daemon when [cortex].enabled.

    `marrow_tool` is daemon.marrow_tool (the alwaysLoad tool decorator). enabled
    == False => no-op (none of the tools reach the schema). When enabled:
      - wish registers for ALL sessions;
      - first / goal are PENDING — not registered anywhere yet (no injection
        mechanism wired; keep the functions + storage, just don't expose them);
      - lie_down / wait / say register ONLY in a cortex session (_CORTEX, the
        import-time MARROW_CORTEX capture — the original inner env gate).
    Idempotent per process (FastMCP tolerates re-adding the same tool name)."""
    global _DB
    if db is not None:
        _DB = db
    if not enabled():
        return
    marrow_tool()(wish)
    if _CORTEX:
        # Render the clamp numbers from cortex config into the tool descriptions
        # at registration (C9/C10) — never hardcoded in the docstring.
        lie_down.__doc__ = _lie_down_doc()
        wait.__doc__ = _wait_doc()
        marrow_tool()(lie_down)
        marrow_tool()(wait)
        marrow_tool()(say)


# ── hooks: kickout immunity / lie_down deny / handoff page-turn / show 亮牌 ──────
# _window_tokens_from_transcript stays in hooks.py (shared with the all-session
# _usage_threshold_context); it is imported lazily where needed below.


def _cortex_lie_down_deny(inp: dict) -> str | None:
    """Deny lie_down until the handoff is written this window, when the
    session asked to rotate OR the window is at the fuse line (force_tokens).
    A plain lie_down under the line is allowed. Cortex window only. None = allow."""
    if not os.environ.get("MARROW_CORTEX"):
        return None
    if inp.get("tool_name") != "mcp__marrow__lie_down":
        return None
    ti = inp.get("tool_input", {}) or {}
    tpath = inp.get("transcript_path") or ""
    cx = config.load().get("cortex", {}) or {}
    force = int(cx.get("force_tokens", 150_000) or 0)
    wants_rotate = bool(ti.get("rotate"))
    from .hooks import _window_tokens_from_transcript
    occupancy = _window_tokens_from_transcript(tpath)
    if not wants_rotate and not (force > 0 and occupancy >= force):
        return None  # plain lie_down under the line — allow
    # Guard fires: require a handoff written after this window's spawn.
    p = _cortex_handoff_path()
    spawn = _window_spawn_epoch(tpath)
    written = False
    if p is not None and spawn is not None:
        try:
            written = p.stat().st_mtime >= spawn and bool(
                p.read_text(encoding="utf-8").strip())
        except OSError:
            written = False
    if written:
        return None
    return (cx.get("handoff_deny_text")
            or "Write your handoff first, then call lie_down again.")


def _window_spawn_epoch(tpath: str) -> float | None:
    """Wall-clock start of this window = the first timestamp in the transcript
    (a resume opens a new file; a fresh window's first line is its birth).
    Leading metadata lines carry no timestamp, so scan up to the first ~50 lines
    for one. Falls back to the file's birthtime (never mtime — the transcript is
    a live file appended every turn), then None."""
    if not tpath:
        return None
    try:
        with open(tpath, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 50:
                    break
                m = _re.search(r'"timestamp":"([^"]+)"', line)
                if m:
                    try:
                        return datetime.fromisoformat(
                            m.group(1).replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        break
    except OSError:
        return None
    try:
        return os.stat(tpath).st_birthtime
    except (OSError, AttributeError):
        return None


def _cortex_handoff_path():
    """<[cortex].home>/<[cortex].handoff_file> — the handoff file a fresh cortex
    window reads at SessionStart. None on config error."""
    try:
        cx = config.load().get("cortex", {}) or {}
        home = (cx.get("home") or "~/.config/marrow/cortex")
        name = (cx.get("handoff_file") or "handoff.md")
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


def arm_ear_text() -> str | None:
    """SessionStart arm line for a fresh cortex window: the one-shot reminder to
    start the ear tail. {signal_log} is substituted with the resolved absolute
    path (config-routed). None when disabled/blanked so the caller injects
    nothing."""
    try:
        cx = config.load().get("cortex", {}) or {}
        tmpl = str(cx.get("arm_ear_text") or "").strip()
        if not tmpl:
            return None
        signal_log = _cortex_path("wake_signal_log_file", "wake_signal.log")
        return tmpl.replace("{signal_log}", str(signal_log))
    except Exception:
        return None


def resume_ear_text() -> str | None:
    """SessionStart line for a RESUMED cortex window ([cortex].resume_ear_text):
    drop stale pre-resume task notifications and re-arm a fresh ear tail. The
    fresh-window counterpart is arm_ear_text. {signal_log} substituted with the
    resolved absolute path. None when disabled/blanked so the caller injects
    nothing."""
    try:
        cx = config.load().get("cortex", {}) or {}
        tmpl = str(cx.get("resume_ear_text") or "").strip()
        if not tmpl:
            return None
        signal_log = _cortex_path("wake_signal_log_file", "wake_signal.log")
        return tmpl.replace("{signal_log}", str(signal_log))
    except Exception:
        return None


def retired_ear_text() -> str | None:
    """SessionStart line for a RESUMED cortex window that is NO LONGER the
    resident ([cortex].retired_ear_text): a newer cortex has taken over, or this
    window was rotated out and is being reopened only to read history. Tells the
    model to stay an archive/read-only window — arm nothing, touch no wake_state.
    {signal_log} substituted with the resolved absolute path. None when
    disabled/blanked so the caller injects nothing."""
    try:
        cx = config.load().get("cortex", {}) or {}
        tmpl = str(cx.get("retired_ear_text") or "").strip()
        if not tmpl:
            return None
        signal_log = _cortex_path("wake_signal_log_file", "wake_signal.log")
        return tmpl.replace("{signal_log}", str(signal_log))
    except Exception:
        return None


def is_resident_session(transcript_path: str | None) -> bool:
    """True when the given transcript is the active resident cortex, per
    wake_state.json's `transcript` pointer. Deterministic — no model judgement.

    MATCH (resident): the recorded transcript equals this one, OR no transcript
    is recorded yet (no newer cortex has claimed residency — mirrors the
    cortex_window_closed empty-pointer semantics).
    NO MATCH (retired): wake_state records a DIFFERENT transcript — a newer
    cortex took over, or this window was rotated out. On any read failure defaults
    to True (resident) so a transient error never silently retires a live window."""
    try:
        d = _wake_state_load(_cortex_wake_state_path())
    except Exception:
        return True
    state_tpath = d.get("transcript")
    if not state_tpath or not transcript_path:
        return True
    return str(state_tpath) == str(transcript_path)


def wake_marker() -> str:
    """Marker prefixing a cortex wake signal line ([cortex].wake_marker). A
    UserPromptSubmit carrying it is a wake turn (full wakeup-note inject)."""
    cx = config.load().get("cortex", {}) or {}
    return str(cx.get("wake_marker") or "").strip()


def _render_note_fresh(transcript_path: str | None) -> str | None:
    """Fresh render via the cortex venv (config [cortex].render_module, e.g.
    cortex.note_render). Reflects the current time + the CALLER's transcript SID
    even after a window rotation, unlike the frozen file. Unset module / any
    failure / empty output -> None so the caller falls back to the static file."""
    c = config.load().get("cortex", {})
    module = str(c.get("render_module") or "").strip()
    py, root = _cortex_paths()
    if not module or not py or not root:
        return None
    py = str(Path(py).expanduser())
    root = str(Path(root).expanduser())
    cmd = [py, "-m", module]
    if transcript_path:
        cmd += ["--transcript", str(transcript_path)]
    try:
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return None
    if p.returncode != 0:
        return None
    text = (p.stdout or "").strip()
    return text or None


import re as _re_ws

_GEN_TOKEN_RE = _re_ws.compile(r"\{g(\d+):([0-9a-fA-F]+)\}")


def parse_gen_token(line: str) -> tuple[int, str] | None:
    """Extract the cancellation-epoch token ' {g<gen>:<state_id>}' a wake signal
    line may carry. None when absent (legacy token-less line = process as before)
    or unparseable."""
    if not line:
        return None
    m = _GEN_TOKEN_RE.search(line)
    if not m:
        return None
    try:
        return int(m.group(1)), m.group(2)
    except (TypeError, ValueError):
        return None


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


def wakeup_note_text(transcript_path: str | None = None) -> str | None:
    """Full text of the wakeup note. Tries a fresh render first (current time +
    caller's transcript SID, correct after rotation); on any failure falls back
    to the frozen file (config wakeup_note_file under home). None when both yield
    nothing so the caller injects nothing."""
    fresh = _render_note_fresh(transcript_path)
    if fresh:
        _mirror_wakeup_note(fresh)
        return fresh
    try:
        note = _cortex_path("wakeup_note_file", "wakeup_note.md")
        text = note.read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def _mirror_wakeup_note(text: str) -> None:
    """Best-effort mirror of the injected note to wakeup_note_file so the on-disk
    copy always equals the latest full note the session received. Never raises —
    a write failure must not break injection."""
    try:
        path = _cortex_path("wakeup_note_file", "wakeup_note.md")
        from ._atomic import atomic_write
        atomic_write(str(path), text)
    except Exception:
        pass


def rearm_text() -> str | None:
    """Rearm line injected when the ear Monitor dies mid-window ([cortex].
    rearm_text). {signal_log} substituted with the resolved absolute path. None
    when blanked so the caller injects nothing."""
    try:
        cx = config.load().get("cortex", {}) or {}
        tmpl = str(cx.get("rearm_text") or "").strip()
        if not tmpl:
            return None
        signal_log = _cortex_path("wake_signal_log_file", "wake_signal.log")
        return tmpl.replace("{signal_log}", str(signal_log))
    except Exception:
        return None


# Monitor-death signature (Item 3). Verified against live cortex/synapse
# transcripts (~/.claude/projects/**/*.jsonl): a Monitor that exits or is
# killed arrives as a user-turn wrapped in <task-notification>…</task-notification>
# whose <event> body is `[Monitor stopped — …]`. Matching on both the
# task-notification wrapper AND the literal "Monitor stopped" event marker is
# conservative — normal chat never contains this harness-generated pair.
def is_monitor_death(prompt: str) -> bool:
    """True when the incoming prompt is the harness notification for a Monitor
    (the ear tail) that stopped. Conservative two-token match — never fires on
    ordinary chat.

    Explicitly excludes the on-resume "No completion record was found for this
    background shell command" notice: the harness emits it for every background
    shell that outlived the prior process (the ear tail among them), and its
    correct handling is the resume_ear_text re-arm at SessionStart, NOT the
    mid-window rearm flow. Treating it as a death fires a spurious wake/rotate.
    """
    if not prompt:
        return False
    if "No completion record was found" in prompt:
        return False
    return "<task-notification>" in prompt and "Monitor stopped" in prompt


def tuck_in_marker() -> str:
    """Marker inside the free-round line the cortex watchdog appends to
    wake_signal.log (surfaces down the ear channel). A prompt carrying it is a
    machine line, not a real user message — excluded from the user-wake reset."""
    cx = config.load().get("cortex", {}) or {}
    return str(cx.get("tuck_in_marker") or "[NEW ROUND]").strip()


# C2 free-round menu body (user-final, verbatim). The cortex watchdog writes ONLY
# the [NEW ROUND] marker to wake_signal.log; that marker line renders on screen in
# the ear Monitor event. This menu body is injected INVISIBLY via UserPromptSubmit
# additionalContext on the marker turn, so she never SEES the menu text in the
# cortex window. Config-first: override via marrow [cortex].tuck_in_menu_text.
_DEFAULT_TUCK_IN_MENU = (
    "No need to wait in silence — 3 choices: "
    "1) talk to her in session (second person) or msg another session  "
    "2) do your own things — see Playbook  3) lie_down..."
)


def tuck_in_menu_text() -> str | None:
    """The 3-choice free-round menu (C2) injected as additionalContext when the
    cortex ear surfaces a [NEW ROUND] marker turn — the covert half that never
    renders on screen. None/blank -> inject nothing (marker-only round)."""
    cx = config.load().get("cortex", {}) or {}
    if "tuck_in_menu_text" in cx:
        txt = str(cx.get("tuck_in_menu_text") or "").strip()
        return txt or None
    return _DEFAULT_TUCK_IN_MENU


# ── Covert machine-marker bodies (FUSE / CTL). Cortex writes only the marker line
#    to wake_signal.log; the ear Monitor surfaces just the marker, and these full
#    instruction bodies are injected INVISIBLY via UserPromptSubmit additionalContext
#    so she never SEES the instruction text in the cortex window. Config-first. ──

_FUSE_MARKER = "[FUSE]"
_CTL_MARKER = "[CTL]"

_DEFAULT_FUSE_PROMPT = (
    "Summarise this whole session into one section and append it to handoff.md — "
    "follow the format and style of the preceding sections. Call "
    "lie_down(rotate=True) when done."
)

_DEFAULT_CTL_SLEEP = (
    "Wrap up this turn: {rotate}lie_down(next_wake_min={mins}{rotate_arg})."
)

_CTL_ARGS_RE = _re.compile(r"mins=(\d+)\s+rotate=(true|false)", _re.IGNORECASE)


def fuse_prompt_text() -> str | None:
    """FUSE instruction body injected as additionalContext when the cortex ear
    surfaces a [FUSE] marker turn. Override [cortex].fuse_prompt_text; blank/None
    -> inject nothing (marker-only)."""
    cx = config.load().get("cortex", {}) or {}
    if "fuse_prompt_text" in cx:
        txt = str(cx.get("fuse_prompt_text") or "").strip()
        return txt or None
    return _DEFAULT_FUSE_PROMPT


def ctl_sleep_text(marker_line: str) -> str | None:
    """CTL sleep instruction body injected as additionalContext when the cortex
    ear surfaces a [CTL] marker turn. The marker line carries the dynamic args
    ('mins=N rotate=true|false'); this renders {mins}/{rotate}/{rotate_arg} from
    them. Override [cortex].ctl_sleep_text; blank/None -> inject nothing."""
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
    rot = "write your handoff then " if rotate else ""
    rotate_arg = ", rotate=true" if rotate else ""
    return (tmpl.replace("{mins}", str(mins))
            .replace("{rotate_arg}", rotate_arg)
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
    bell, free-round, night, fuse, ctl and ⚙️ [CMD ct-*] slash-command bodies.
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
    down the ear channel (wake bell / monitor death / free-round / night / fuse /
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
    m = wake_marker()
    if m and _line_starts_with(p, m):
        return True
    tm = tuck_in_marker()
    if tm and _line_starts_with(p, tm):
        return True
    if is_monitor_death(p):
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


import contextlib as _contextlib
import fcntl as _fcntl
import json as _json_ws


@_contextlib.contextmanager
def _wake_state_lock(p: Path):
    """Blocking exclusive flock on <wake_state>.lock, byte-compatible with
    cortex.wake_state._flock (same sibling .lock, same protocol). Best-effort:
    an unacquirable lock still proceeds (matches cortex's fallback).
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


def _clear_floor_deadline() -> None:
    """Clear the pending floor deadline (next_floor_due_at) on the single-row
    ct_pacemaker_state JSON. Semantics: cortex triggers._floor_trigger treats
    None as DUE (fail-safe = a spurious wake, heartbeat preserved). Net effect
    is still correct — the awake gate blocks any signal while awake, and the
    user-wake reset that calls this also flips awake=true, so the next lie_down
    redraws a fresh floor before None could fire a wake."""
    import sqlite3
    dbp = config.db_path()
    try:
        conn = sqlite3.connect(dbp, timeout=30)
    except sqlite3.Error:
        return
    try:
        row = conn.execute(
            "SELECT state FROM ct_pacemaker_state WHERE id = 1").fetchone()
        if not row:
            return
        try:
            obj = _json_ws.loads(row[0])
        except (ValueError, TypeError):
            return
        if obj.get("next_floor_due_at") is None:
            return
        obj["next_floor_due_at"] = None
        conn.execute(
            "UPDATE ct_pacemaker_state SET state = ? WHERE id = 1",
            (_json_ws.dumps(obj),))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


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


def kill_orphan_ear_tails() -> int:
    """Kill leftover ear-tail processes (`tail … -f <signal_log>`) whose owning
    cc session already exited. Called at resume SessionStart: the resumed window
    has not armed its own tail yet, so every match at this instant is an orphan
    from the dead prior process. Match is narrowed to the exact resolved
    signal-log path (pgrep -f) so unrelated tails are never touched; our own pid
    is skipped defensively. Best-effort — returns the count signalled for kill,
    0 on any failure (missing pgrep, no matches, disabled path)."""
    try:
        signal_log = str(_cortex_path("wake_signal_log_file", "wake_signal.log"))
    except Exception:
        return 0
    if not signal_log:
        return 0
    try:
        proc = subprocess.run(
            ["pgrep", "-f", f"-f {signal_log}"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return 0
    if proc.returncode not in (0, 1):
        return 0
    killed = 0
    me = os.getpid()
    for raw in (proc.stdout or "").split():
        try:
            pid = int(raw)
        except ValueError:
            continue
        if pid <= 0 or pid == me:
            continue
        _kill_pid(pid)
        killed += 1
    return killed


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


def _is_live_wait_until(raw: str) -> bool:
    """True if raw (an ISO wake_state silence_wait_until value) parses and is
    still in the future -> the in-flight wait it guards has not expired yet."""
    from datetime import timezone as _tz
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return dt > datetime.now(_tz.utc)


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
            "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons) "
            "VALUES (?, 1, 0, 'user')", (ts,))
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _cortex_user_wake_reset(inp: dict) -> None:
    """Real user message in a cortex window -> flip awake, kill the pending
    alarm (floor deadline + sentinel), mark the user reply, and ensure a
    watchdog is alive. A wait only counts if its timer ran to expiry with no
    user: if a LIVE (unexpired) silence_wait_until is interrupted here, the
    in-flight wait never completed, so refund it (wait_count -= 1, floored at
    0); an expired/absent wait_until leaves wait_count untouched. Fast +
    idempotent: already-awake + watchdog-alive collapses to cheap no-op
    writes. Cortex session only; the caller has already excluded machine
    lines (is_machine_line)."""
    if not os.environ.get("MARROW_CORTEX"):
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
        raw_wait_until = d.pop("silence_wait_until", None)
        if raw_wait_until is not None and _is_live_wait_until(raw_wait_until):
            d["wait_count"] = max(0, int(d.get("wait_count") or 0) - 1)
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
    # Kill the pending alarm: floor deadline + the exact-time sentinel.
    if sentinel_pid is not None:
        _wake_audit("sentinel_kill", trigger, f"pid={sentinel_pid}")
    _wake_audit("floor_clear", trigger, "next_floor_due_at->None")
    _clear_floor_deadline()
    _kill_pid(sentinel_pid)
    _spawn_watchdog_if_absent()
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


# ── external wake (cortex.kick) — cli morning flag-pull (P6) ──────────────────

def _cortex_toml_path() -> Path:
    """cortex.toml lives beside marrow's config (shared config dir). Read
    directly (marrow venv cannot import cortex) for the few kick knobs the cli
    morning path needs."""
    return Path(config.db_path()).parent / "cortex.toml"


def _cortex_toml_section(section: str, key: str, default):
    """One value from cortex.toml [section] (dotted `section` walks nested TOML
    tables, e.g. "wake.watchdog" -> data["wake"]["watchdog"]). Tolerant: missing
    file/key -> default. marrow venv cannot import cortex, so the few numbers the
    tool descriptions render (wait/lie_down clamps) are read straight from the
    shared cortex.toml."""
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


def _cortex_night(key: str, default):
    """One value from cortex.toml [night]. Tolerant: missing file/key -> default."""
    return _cortex_toml_section("night", key, default)


def _cortex_night_mode() -> bool:
    """True when cortex wake_state carries the night flag (mode == 'night').
    Absent / unreadable file -> False (day = no morning kick). The flag lifecycle
    itself is P8; this only reads it."""
    try:
        d = _wake_state_load(_cortex_wake_state_path())
        return str(d.get("mode") or "") == "night"
    except Exception:
        return False


def _past_morning_start() -> bool:
    """True when local time is at/after night.morning_start (default 06:00)."""
    from zoneinfo import ZoneInfo
    raw = str(_cortex_night("morning_start", "06:00") or "06:00")
    try:
        hh, mm = (int(x) for x in raw.split(":"))
    except (ValueError, TypeError):
        hh, mm = 6, 0
    tz = str(config.load().get("core", {}).get("timezone")
             or config.load().get("timezone") or "Australia/Melbourne")
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        now = datetime.now()
    return (now.hour, now.minute) >= (hh, mm)


def kick_cortex(kind: str, note_id=None, minutes=None) -> None:
    """Fire cortex.kick as a detached, fire-and-forget subprocess in the cortex
    venv (marrow venv cannot import cortex). Never blocks the caller; any launch
    failure is swallowed. `kind` in {reply, timeout, morning}."""
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


def maybe_morning_kick_cli() -> None:
    """cli morning flag-pull: a real user turn in a NON-cortex cli session, with
    the cortex night flag set and local time past morning_start, pokes cortex to
    clear the flag and return to day cadence. The caller has already excluded
    machine/subagent turns and confirmed this is NOT the cortex session.
    No-op unless enabled + configured + night + past morning_start."""
    if not enabled():
        return
    if os.environ.get("MARROW_CORTEX"):
        return  # cortex session takes its own reset path, not this
    if not (_cortex_night_mode() and _past_morning_start()):
        return
    kick_cortex("morning")


def cortex_window_closed(transcript_path: str | None) -> None:
    """Cortex window really ending (session_end, non-'clear' reason): if the
    wake_state is awake AND (no transcript recorded yet OR it matches this
    session's), end the wake immediately via a proxy lie_down instead of
    waiting for a 20-min fallback to discover the dead window. Idempotent —
    lie_down already no-ops when not awake. Best-effort: never raises."""
    if not os.environ.get("MARROW_CORTEX"):
        return
    try:
        p = _cortex_wake_state_path()
        d = _wake_state_load(p)
        if not d.get("awake"):
            return
        state_tpath = d.get("transcript")
        if state_tpath and transcript_path and str(state_tpath) != str(transcript_path):
            return
        cx = config.load().get("cortex", {}) or {}
        next_wake_min = cx.get("close_next_wake_min", 55)
        py, root = _cortex_paths()
        if not py or not root:
            return
        py = str(Path(py).expanduser())
        root = str(Path(root).expanduser())
        cmd = [py, "-m", "cortex.lie_down",
               "--force-slept", "auto",
               "--next-wake-min", str(next_wake_min)]
        subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — never block session_end
        pass


_HANDOFF_DATE_RE = _re.compile(
    r"\[(\d{4}-\d{2}-\d{2})\]|(\d{4}-\d{2}-\d{2})\s*$")


def _handoff_l1_date(text: str) -> str | None:
    """L1 date: `[YYYY-MM-DD]` bracketed or a bare trailing YYYY-MM-DD.
    None if L1 is missing/unparsable (e.g. the literal template placeholder)."""
    l1 = text.splitlines()[0] if text else ""
    m = _HANDOFF_DATE_RE.search(l1)
    if not m:
        return None
    date_str = m.group(1) or m.group(2)
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    return date_str


def _cortex_page_turn(p: Path, old_text: str) -> None:
    """Archive a stale handoff.md and replace it with a fresh dated copy of the
    template. Best-effort: any failure leaves the stale file in place (the
    NEXT SessionStart will retry the page-turn)."""
    cx = config.load().get("cortex", {}) or {}
    home = (cx.get("home") or "~/.config/marrow/cortex")
    home_p = Path(home).expanduser()
    archive_dir = home_p / (cx.get("handoff_archive_dir") or "handoff_archive")
    template_name = cx.get("handoff_template_file") or "handoff_template.md"
    template_p = home_p / template_name
    old_date = _handoff_l1_date(old_text)
    try:
        old_mtime = p.stat().st_mtime
    except OSError:
        old_mtime = time.time()
    try:
        template_text = template_p.read_text(encoding="utf-8")
    except OSError:
        return  # no template to copy from — leave the stale file in place
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / f"{old_date}.md"
        n = 1
        while dest.exists():
            n += 1
            dest = archive_dir / f"{old_date}-{n}.md"
        shutil.move(str(p), str(dest))

        today = datetime.now(config.get_tz()).date().isoformat()
        new_text = template_text.replace("[YYYY-MM-DD]", f"[{today}]")
        p.write_text(new_text, encoding="utf-8")
        # Backdate mtime so _cortex_lie_down_deny's "written this window" gate
        # doesn't wrongly read the fresh copy as this window's own handoff.
        os.utime(p, (old_mtime, old_mtime))
    except OSError:
        pass


def _cortex_handoff_page_turn_if_stale() -> None:
    """Daily file side effect for a fresh cortex window: a stale (before-today)
    L1 date triggers archive + fresh dated template copy for tomorrow's read.
    No parsable date or unreadable file -> no-op. No content is returned; the
    user's cortex CLAUDE.md `@handoff.md` import is the sole read path now."""
    p = _cortex_handoff_path()
    if p is None:
        return
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not text:
        return
    date_str = _handoff_l1_date(text)
    if date_str is None:
        return
    today = datetime.now(config.get_tz()).date().isoformat()
    if date_str < today:
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


def _cortex_show_context(tpath: str) -> str:
    """Cortex-only (MARROW_CORTEX=1) window-occupancy 亮牌 at show_tokens (12万
    soft, ahead of the 15万 fuse). Suppressed when user is chatting
    (user_replied_this_wake). Empty for normal sessions, below threshold,
    or with the text blanked out."""
    if not os.environ.get("MARROW_CORTEX"):
        return ""
    cr = config.load().get("cortex_rotate", {}) or {}
    text = (cr.get("show_text") or "").strip()
    if not text:
        return ""
    show = int(cr.get("show_tokens", 100_000) or 0)
    if show <= 0:
        return ""
    from .hooks import _window_tokens_from_transcript
    if _window_tokens_from_transcript(tpath) < show:
        return ""
    try:
        ws = _wake_state_load(_cortex_wake_state_path())
    except Exception:
        ws = {}
    # Presence gate: hold the nudge while the user's last real message is younger
    # than show_silent_min (mid-chat — the 150k fuse is the backstop). It retries
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


# ── llm: full-env cortex runner + per-wake accounting ─────────────────────────

def _cortex_stream_timer():
    """Env-driven stream-event timing probe for cortex wakes (wake latency
    diagnosis). Returns a callback appending one line per notable stage to
    CORTEX_WAKE_TIMING_LOG, or None when cortex did not request it. Best-effort:
    never raises into the stream loop. spawned -> first_event isolates claude
    CLI startup cost (MCP/env load) before the first token."""
    from .llm import _utcnow_iso
    path = os.environ.get("CORTEX_WAKE_TIMING_LOG")
    if not path:
        return None
    path = os.path.expanduser(path)
    wake_id = os.environ.get("CORTEX_WAKE_ID", "?")
    origin = time.monotonic()
    seen: set[str] = set()

    def _emit(ev: dict, mono: float) -> None:
        try:
            etype = ev.get("type", "?")
            if etype == "__spawned__":
                label = "spawned"
            elif "first" not in seen:
                seen.add("first")
                label = "first_event"
            else:
                sub = ev.get("subtype")
                key = f"{etype}/{sub}" if sub else etype
                if key in seen:
                    return
                seen.add(key)
                label = f"ev.{key}"
            ms = (mono - origin) * 1000.0
            with open(path, "a") as f:
                f.write(f"{_utcnow_iso()} wake={wake_id} stream.{label} +{ms:.0f}ms\n")
        except Exception:
            pass

    return _emit


def call_cortex(client, prompt: str, *, cwd: str | None = None,
                resume_sid: str | None = None,
                timeout: float | None = None,
                max_tokens: int | None = None) -> dict:
    """Full-environment resumed session for cortex (C3, Decided 07-03):
    no isolation flags — persona/rules/MCP/agents load like a real
    session. Always injects MARROW_CORTEX=1 (identity marker, e.g. B8
    kickout immunity) and MARROW_CHANNEL=ct so this session's turns get
    full marrow memory (events/recall/tl) attributed to the cortex
    channel, same as any other session. cwd defaults to [cortex].home;
    resume_sid=None starts a fresh
    session (daily rebirth). Single attempt — no chain/retry, caller
    (cortex pacemaker) owns retry policy. `timeout` (s) overrides the
    provider default so the caller's config is the single source of truth
    for the call budget (cortex derives its outer kill from the same value).
    `max_tokens` (>0) caps the per-wake CURRENT WINDOW SIZE — the latest
    turn's (input+cache_read+cache_creation), i.e. the same figure the
    caller reasons about via the statusline "total" (Decided 07-04, not
    cumulative consumption across turns): accumulated mid-stream (usage
    deduped by request_id — a single turn streams as several assistant
    lines each repeating identical usage), breach terminates the
    subprocess and returns capped=True so the caller rebirths. Returns
    {"text": str, "session_id": str | None} (+ capped / total_tokens
    [= final window size] when a cap is active).
    """
    from .llm import LLMError
    spec = client.specs.get("claude_cli_cortex")
    if not spec:
        raise LLMError("no claude_cli_cortex provider configured")
    cortex_cfg = client.cfg.get("cortex", {})
    run_cwd = os.path.expanduser(
        cwd or cortex_cfg.get("home") or "~/.config/marrow/cortex")
    Path(run_cwd).mkdir(parents=True, exist_ok=True)
    tier = cortex_cfg.get("tier", "top")
    model = cortex_cfg.get("model") or client.tiers.get(tier) or client.tiers.get("top")
    effort = cortex_cfg.get("effort") or ""
    return run_claude_cortex(
        client, spec, model, prompt, cwd=run_cwd, resume_sid=resume_sid,
        timeout=timeout, effort=effort, max_tokens=max_tokens)


def run_claude_cortex(client, spec: dict, model: str, prompt: str, *,
                      cwd: str, resume_sid: str | None,
                      timeout: float | None = None,
                      effort: str = "",
                      max_tokens: int | None = None) -> dict:
    """Stream-json runner with NO isolation flags (cortex full-env, C3).
    Mirrors _run_claude_stream's spawn/timeout/kill contract exactly —
    only the isolation flags, env var, cwd, --resume, and --effort differ.
    When max_tokens>0, accumulates per-wake usage mid-stream (deduped by
    request_id) and terminates cleanly when the current turn's window
    size breaches the cap (capped=True). Env-driven stream-event timing
    is attached best-effort for wake-latency diagnosis."""
    from .llm import _claude_bin, _snapshot_window_tokens
    timeout = timeout if timeout is not None else spec.get("timeout_s", 600)
    cmd = [_claude_bin(), "--output-format", "stream-json",
           "--input-format", "stream-json", "--verbose", "--model", model,
           "--permission-mode", "bypassPermissions"]
    if effort:
        cmd.extend(["--effort", effort])
    if resume_sid:
        cmd.extend(["--resume", resume_sid])
    env = {**os.environ, "MARROW_CORTEX": "1", "MARROW_CHANNEL": "ct"}
    on_event = _cortex_stream_timer()
    cap_active = bool(max_tokens and max_tokens > 0)
    extra: dict = {}
    if on_event is not None:
        extra["on_event"] = on_event
    sink = None
    if cap_active:
        sink = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0,
                "window": 0, "capped": False, "by_request": {},
                "has_usage": False}
        extra["max_tokens"] = max_tokens
        extra["usage_sink"] = sink
    raw = client._stream_subprocess(cmd, prompt, timeout, env, cwd=cwd, **extra)
    if sink is not None and sink["capped"]:
        client._log_usage(client._sink_usage(sink), model, "stream-json",
                          window=sink["window"])
        _log_cortex_cap(sink, max_tokens, model)
        _snapshot_window_tokens(sink["window"])
        return {"text": "", "session_id": None, "capped": True,
                "total_tokens": sink["window"]}
    text = client._parse_claude(raw, "stream-json")
    session_id = client._extract_session_id(raw)
    if sink is not None:
        if sink["has_usage"]:
            client._log_usage(client._sink_usage(sink), model, "stream-json",
                             window=sink["window"])
        else:
            client._log_usage(client._extract_usage(raw, "stream-json"),
                             model, "stream-json")
        _snapshot_window_tokens(sink["window"])
        return {"text": text, "session_id": session_id,
                "total_tokens": sink["window"]}
    client._log_usage(client._extract_usage(raw, "stream-json"), model, "stream-json")
    return {"text": text, "session_id": session_id}


def _log_cortex_cap(sink: dict, cap: int, model: str) -> None:
    """One audit line marking a per-wake token-cap breach. Reports the
    breaching turn's window size (input+cache_read+cache_creation — the
    figure compared against cap) alongside the deduped cumulative usage
    fields (true consumption across the wake so far) so a breach is
    auditable without ambiguity about what tripped it. Best-effort."""
    try:
        conn = storage.connect()
        with conn:
            conn.execute(
                "INSERT INTO audit_log (target_table, action, summary)"
                " VALUES (?, ?, ?)",
                ("llm_usage", "llm_cortex_cap",
                 f"model={model} capped window={sink['window']} cap={cap} "
                 f"cumulative(in={sink['in']} out={sink['out']} "
                 f"cache_read={sink['cache_read']} cache_write={sink['cache_write']})"),
            )
        conn.close()
    except Exception:
        pass
