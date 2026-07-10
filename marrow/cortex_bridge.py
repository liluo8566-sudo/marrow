"""Cortex bridge — the optional autonomous-wake ("cortex") organs, extracted
from daemon.py / hooks.py / llm.py into one module.

Cortex is an optional layer that lives in a SEPARATE repo (~/CC-Lab/cortex);
this module is marrow's side of that bridge: the MCP tools it exposes to a
cortex session, the SessionStart / PreToolUse / turn_inject hook branches that
only a cortex window takes, and the full-env LLM runner the cortex repo calls
back into.

Two independent gates, both must be open for any cortex behaviour:

  1. [cortex].enabled (config, default false) — "are the organs installed".
     enabled() == False  => register() is a no-op (none of the six tools reach
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


def wish(text: str) -> dict:
    """Our wishlist — personal wishes & cravings (hers and yours), promises
    made, and shared plans. e.g. 你说好请我喝奶茶 / 最近想买耳钉 / 约好周末去看海.
    This tool appends one line verbatim; update / delete = edit
    ~/.config/marrow/cortex/wishlist.md directly."""
    import fcntl
    from datetime import datetime

    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "text required"}
    path = _wishlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    date = datetime.now(config.get_tz()).strftime("%Y-%m-%d")
    line = f"- {date} {text}\n"
    lock_path = str(path) + ".lock"
    lf = open(lock_path, "a")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        existing = path.read_text(encoding="utf-8") if path.exists() else _WISHLIST_HEADER
        from ._atomic import atomic_write
        atomic_write(str(path), existing + line)
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
    """Respond to the Cortex First section (notes/concerns injected into context).
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
    """Timetrack weekly goals e.g. study, sleep, exercise.
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


def lie_down(rotate: bool = False, next_wake_min: float | None = None) -> dict:
    """Sleep and set the next wake: lie_down(next_wake_min=N) [N=11-55],
    omit = dice. rotate=True = fresh window on next wake (last lie_down of a
    full window; write your handoff section first — guarded)."""
    args = ["--rotate"] if rotate else []
    if next_wake_min is not None:
        args += ["--next-wake-min", str(next_wake_min)]
    return _run_cortex_module("cortex.lie_down", args or None)


def wait(minutes: float) -> dict:
    """Stay awake: wait(minutes=N) [N=11-55] holds the silence timeout once,
    e.g. you expect a reply soon. Max twice per wake."""
    return _run_cortex_module("cortex.wait", ["--minutes", str(minutes)])


def say() -> dict:
    """Urgent only: quiet notification ping for her attention (no focus steal).
    Normal in-window talk needs no say — she reads when free."""
    return _run_cortex_module("cortex.say")


# ── daemon registration ───────────────────────────────────────────────────────

# DB the tools read/write. Set at register() time from the daemon's own _DB so
# it tracks the same source the daemon resolved at import; tests patch this.
_DB = config.db_path()


def register(marrow_tool, db: str | None = None) -> None:
    """Install the cortex MCP tools onto the daemon when [cortex].enabled.

    `marrow_tool` is daemon.marrow_tool (the alwaysLoad tool decorator). enabled
    == False => no-op (none of the six tools reach the schema). When enabled:
      - wish / first / goal register for ALL sessions;
      - lie_down / wait / say register ONLY in a cortex session (_CORTEX, the
        import-time MARROW_CORTEX capture — the original inner env gate).
    Idempotent per process (FastMCP tolerates re-adding the same tool name)."""
    global _DB
    if db is not None:
        _DB = db
    if not enabled():
        return
    marrow_tool()(wish)
    marrow_tool()(first)
    marrow_tool()(goal)
    if _CORTEX:
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
    """Wall-clock start of this window = the first transcript line's timestamp
    (a resume opens a new file; a fresh window's first line is its birth). Falls
    back to the file's own ctime, then None."""
    if not tpath:
        return None
    try:
        with open(tpath, encoding="utf-8") as f:
            for line in f:
                m = _re.search(r'"timestamp":"([^"]+)"', line)
                if m:
                    try:
                        return datetime.fromisoformat(
                            m.group(1).replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        break
                break
    except OSError:
        return None
    try:
        return os.path.getmtime(tpath)
    except OSError:
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


def wake_emoji() -> str:
    """The single-emoji launch prompt that triggers wake-instruction injection.
    Must match the cortex-side wake.wake_prompt (kept in config on both sides so
    they can't drift). Empty -> feature off (no emoji matches)."""
    cx = config.load().get("cortex", {}) or {}
    return str(cx.get("wake_emoji") or "").strip()


def cortex_wake_instructions() -> str | None:
    """Wake instructions injected as UserPromptSubmit additionalContext when the
    emoji is submitted in a cortex window. {note}/{signal_log} are substituted
    with resolved absolute paths (config-routed, never hardcoded). None when
    disabled/misconfigured (empty template) so the caller injects nothing."""
    try:
        cx = config.load().get("cortex", {}) or {}
        tmpl = str(cx.get("wake_instructions") or "").strip()
        if not tmpl:
            return None
        note = _cortex_path("wakeup_note_file", "wakeup_note.md")
        signal_log = _cortex_path("wake_signal_log_file", "wake_signal.log")
        return tmpl.replace("{note}", str(note)).replace("{signal_log}", str(signal_log))
    except Exception:
        return None


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


def _cortex_show_context(tpath: str) -> str:
    """Cortex-only (MARROW_CORTEX=1) window-occupancy 亮牌 at show_tokens (10万
    soft, ahead of the 15万 fuse). Empty for normal sessions, below threshold,
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
    if _window_tokens_from_transcript(tpath) >= show:
        return text
    return ""


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
    deduped by requestId — a single turn streams as several assistant
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
    requestId) and terminates cleanly when the current turn's window
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
