"""Thin CC hook entrypoints. `python -m marrow.hooks <event>`.

Code-only, no LLM. Parallel-safe with the legacy ny-memm hooks —
marrow registers ALONGSIDE them, never replaces. Logic lives in the marrow
package; this only does hook I/O (stdin JSON in, stdout JSON for
SessionStart additionalContext, side effects for SessionEnd).

  session_start      -> inject open tasks + alerts + affect backdrop; clear skip on resume
  session_end        -> clean transcript, archive events, regen dashboard top
  user_prompt_submit -> mm-/mm+ skip control + recall fallback

PreToolUse is the global prompt-guard.py (scope already covers
~/CC-Lab/marrow/), not duplicated here.

mm- prefix: writes audit_log manual_skip row; sessionend_async skips LLM pipeline.
mm+ prefix: immediately reruns sessionend_async for current (or named) sid.
resume detection: session_start fires on cc resume with same sid; if skip row exists,
  write skip_cleared row so sessionend_async runs normally.
"""
from __future__ import annotations

import json
import os
import re as _re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import config, repo, storage, top_sections, transcript
from .popen_detach import popen_detach

SESSION_START_HARD_CAP = 6000

# ── recall dedup state (per-session, hook-only) ──────────────────────────────

_TABLE_KINDS = {"milestone", "memes", "entity", "diary", "task"}


def _recall_seen_path(sid: str) -> Path:
    return config.DATA_DIR / "state" / "recall_seen" / f"{sid}.json"


def _load_recall_seen(sid: str) -> set[tuple[str, int]]:
    if not sid:
        return set()
    try:
        data = json.loads(_recall_seen_path(sid).read_text())
        return {(str(k), int(i)) for k, i in data}
    except Exception:
        return set()


def _save_recall_seen(sid: str, seen: set[tuple[str, int]]) -> None:
    if not sid:
        return
    p = _recall_seen_path(sid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(seen)))
    except Exception:
        pass


def _wipe_recall_seen(sid: str) -> None:
    if not sid:
        return
    try:
        _recall_seen_path(sid).unlink(missing_ok=True)
    except Exception:
        pass


def _rotate_recall_log() -> None:
    """Rotate logs/recall.md → recall.md.prev so each session starts fresh."""
    log = config.DATA_DIR / "logs" / "recall.md"
    if not log.exists():
        return
    try:
        log.replace(log.with_suffix(".md.prev"))
    except Exception:
        pass


# ── manual skip helpers ───────────────────────────────────────────────────────

_MANUAL_SKIP_ACTION = "manual_skip"
_STATUS_SKIP = "skip"
_STATUS_SKIP_CLEARED = "skip_cleared"
_STATUS_SKIP_BRIDGE_OWNS = "bridge_owns"
_SESSION_BLOCK_ACTION = "session_block"
_STATUS_BLOCK_ARCHIVE = "archive"
_STATUS_BLOCK_CLEARED = "cleared"


def _write_manual_skip_flag(conn: sqlite3.Connection, sid: str, status: str) -> None:
    """Write a manual_skip audit row. status = 'skip' or 'skip_cleared'."""
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, ?, ?)",
            (sid, _MANUAL_SKIP_ACTION, status),
        )


def _write_session_block_flag(conn: sqlite3.Connection, sid: str, status: str) -> None:
    """Write a session_block audit row. status = 'archive' -> block events insert."""
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, ?, ?)",
            (sid, _SESSION_BLOCK_ACTION, status),
        )


def _is_session_blocked(conn: sqlite3.Connection, sid: str) -> bool:
    """Latest session_block row wins. archive -> True, cleared/absent -> False.
    Lets mm+ override a prior mm- by writing a cleared row."""
    row = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action=? AND target_id=?"
        " ORDER BY id DESC LIMIT 1",
        (_SESSION_BLOCK_ACTION, sid),
    ).fetchone()
    if not row:
        return False
    return row["summary"] == _STATUS_BLOCK_ARCHIVE


def _is_manual_skip(conn: sqlite3.Connection, sid: str) -> bool:
    """Latest manual_skip row wins. skip -> True, skip_cleared/absent -> False."""
    row = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action=? AND target_id=?"
        " ORDER BY id DESC LIMIT 1",
        (_MANUAL_SKIP_ACTION, sid),
    ).fetchone()
    if not row:
        return False
    return row["summary"] == _STATUS_SKIP


def _has_prior_lifecycle_start(conn: sqlite3.Connection, sid: str) -> bool:
    """True iff sid already has at least one session_lifecycle:start row — i.e. this
    is a resume, not a fresh start."""
    row = conn.execute(
        "SELECT 1 FROM audit_log"
        " WHERE action='session_lifecycle:start' AND target_id=?"
        " LIMIT 1",
        (sid,),
    ).fetchone()
    return row is not None


def _primary_worktree(cwd: str) -> str | None:
    """Return realpath of the primary worktree of the repo containing *cwd*,
    or None if cwd is not in a git repo.

    `git worktree list --porcelain` lists the primary worktree FIRST.
    """
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=False, timeout=2,
        ).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        if line.startswith("worktree "):
            return os.path.realpath(line[len("worktree "):].strip())
    return None


def _is_worktree_session(cwd: str) -> bool:
    """True iff *cwd* is inside a NON-primary git worktree.

    Worktree sessions are independent cc processes (new sid, new jsonl) doing
    task-isolated work; their dialogue is not part of the user's continuous
    memory and must not enter marrow events. Detection: cwd's git toplevel
    differs from the repo's primary worktree (first row of `git worktree list
    --porcelain`).
    """
    if not cwd or not os.path.isdir(cwd):
        return False
    try:
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False, timeout=2,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return False
    if not top:
        return False
    primary = _primary_worktree(cwd)
    if not primary:
        return False
    return os.path.realpath(top) != primary


_PPID_MODEL_RE = _re.compile(r"--model[\s=]+['\"]?([^\s'\"]+)['\"]?")


def _maybe_set_session_title(sid: str | None, prompt_text: str) -> None:
    """Sticky title — first non-empty prompt of a session lands as sessions.title.

    Runs on every user_prompt_submit, but `upsert_session`-with-title is gated
    on `sessions.title` being empty so existing titles never get rewritten.
    Powers wx /resume picker's `— <title>` suffix for cli + wx sessions alike.
    """
    if not sid or not prompt_text:
        return
    try:
        cur = repo.get_session(sid)
        if cur and (cur.get("title") or "").strip():
            return  # already titled — sticky
        head = prompt_text.splitlines()[0].strip()
        head = _re.sub(r"\s+", " ", head)[:40]
        if not head:
            return
        channel = (cur or {}).get("channel") or os.environ.get("MARROW_CHANNEL") or "cli"
        repo.upsert_session(sid, None, channel, title=head)
    except Exception:  # noqa: BLE001 — never block user prompt
        pass


def _cli_model_from_ppid(ppid: int) -> str | None:
    """Read `--model <id>` from cc's launch args via `ps -p <ppid> -o command=`.

    cc's jsonl strips the `[1m]` context-window suffix from `model`, so wx
    /resume picker can't tell a 1M-mode cli session from a 200k one. This
    peeks at the parent process's argv and returns the model id verbatim only
    when it carries the `[1m]/[1M]` suffix — bare ids are already what jsonl
    fallback produces, so writing them here would add no information.
    """
    try:
        out = subprocess.run(
            ["ps", "-p", str(ppid), "-o", "command="],
            capture_output=True, text=True, check=False, timeout=2,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — never block session_start
        return None
    if not out:
        return None
    m = _PPID_MODEL_RE.search(out)
    if not m:
        return None
    val = m.group(1).strip()
    return val if _re.search(r"\[1[mM]\]$", val) else None


def _started_at_for(ppid: int) -> int:
    """Return process start time as epoch for *ppid* via `ps -o lstart=`.
    Falls back to current time on any failure.

    LC_ALL=C forces POSIX time format so the strptime mask works under any
    user locale (en_AU prints day-before-month by default, breaking parsing
    and silently rotting catchup's ppid liveness check)."""
    try:
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        env["LC_TIME"] = "C"
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(ppid)],
            capture_output=True, text=True, check=False, timeout=2, env=env,
        ).stdout.strip()
        if out:
            return int(datetime.strptime(out, "%a %b %d %H:%M:%S %Y").timestamp())
    except Exception:  # noqa: BLE001
        pass
    return int(time.time())


def _last_ok_user_count(conn: sqlite3.Connection, sid: str) -> int | None:
    """Return N from the most recent `ok,user_count=N` audit row, or None."""
    row = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=?"
        " AND summary LIKE 'ok,user_count=%'"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if not row:
        return None
    try:
        return int(row["summary"].split("=", 1)[1])
    except (ValueError, IndexError):
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── affect heartbeat ─────────────────────────────────────────────────────────

def _affect_heartbeat(conn: sqlite3.Connection) -> str | None:
    """Return block line if a day in last 7d had events but no affect, else None.

    DECISIONS line 37: fires ONLY on a day that HAD events but NO affect.
    Checks the past 7 calendar days (UTC date boundary), but ignores days
    earlier than the affect pipeline's first-seen date — historical events
    before AFFECT extraction shipped never had a chance to produce rows.
    """
    pipeline_start_row = conn.execute(
        "SELECT MIN(date) FROM affect_live"
    ).fetchone()
    pipeline_start = pipeline_start_row[0] if pipeline_start_row else None
    if not pipeline_start:
        return None  # pipeline never produced anything → warning is noise
    today = _now_utc().date()
    gap_day: str | None = None
    for delta in range(1, 8):
        d = (today - timedelta(days=delta)).isoformat()
        if d < pipeline_start:
            continue
        has_events = conn.execute(
            "SELECT 1 FROM events WHERE date(timestamp) = ? LIMIT 1", (d,)
        ).fetchone()
        if not has_events:
            continue
        has_affect = conn.execute(
            "SELECT 1 FROM affect_live WHERE date = ? LIMIT 1", (d,)
        ).fetchone()
        if not has_affect:
            gap_day = d
            break  # report the most recent gap only
    if gap_day:
        return f"[⚠ (情感记录可能中断): {gap_day}]"
    return None


# Affect backdrop = top_sections.render_affect (shared with dashboard).


# ── session-start payload ────────────────────────────────────────────────────

def _read_input() -> dict:
    # Manual CLI runs (tty stdin) skip the blocking read so devs can
    # invoke `python -m marrow.hooks <event>` without piping JSON.
    if sys.stdin.isatty():
        return {}
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _handoff_text(conn) -> str:
    h = repo.handoff(conn)
    lines = ["# Marrow handoff", ""]
    archived = repo.archived_today(conn)
    if archived:
        lines.append(f"## Today Archived [{len(archived)}]")
        for t in archived:
            lines.append(f"- [{t['category']}] {t['title']} #{t['id']}")
        lines.append("")
    lines.append("## Open Tasks")
    if h["tasks"]:
        for t in h["tasks"]:
            due = f" [Due {t['due']}]" if t.get("due") else ""
            nxt = f" — {t['next_step']}" if t.get("next_step") else ""
            lines.append(f"- [{t['category']}] {t['title']}{nxt}{due} #{t['id']}")
    else:
        lines.append("- none")
    lines += ["", "## Alerts"]
    if h["alerts"]:
        for a in h["alerts"]:
            lines.append(f"- #{a['id']} [{a['severity']}] {a['message']}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def session_start() -> int:
    try:
        log = config.DATA_DIR / "logs" / "sessionstart_catchup.log"
        popen_detach([sys.executable, "-m", "marrow.sessionstart_catchup"], log_path=log)
    except Exception as e:
        try:
            repo.add_alert("warn", "catchup",
                           f"session_start catchup spawn failed: {e}",
                           source="hooks.py", db=config.db_path())
        except Exception:
            pass
    # Recall housekeeping — rotate side log + wipe per-session dedup state so
    # every fresh window starts with a clean recall slate.
    _rotate_recall_log()
    inp = _read_input()
    db = config.db_path()
    conn = storage.connect(db)
    try:
        # Write lifecycle:start marker so catchup can detect live vs dead sessions.
        sid = inp.get("session_id") if isinstance(inp, dict) else None
        cwd = inp.get("cwd") if isinstance(inp, dict) else None
        is_worktree = _is_worktree_session(cwd or "")
        if sid:
            # Fresh window or resume — drop prior recall dedup state either way
            # (cheap; resume re-shows seen rows once, acceptable).
            _wipe_recall_seen(sid)
            try:
                # Resume detection: if sid already has a lifecycle:start row, this
                # is a cc resume. Clear any manual skip so sessionend runs normally.
                is_resume = _has_prior_lifecycle_start(conn, sid)
                if is_resume and _is_manual_skip(conn, sid):
                    _write_manual_skip_flag(conn, sid, _STATUS_SKIP_CLEARED)
                ppid = os.getppid()
                started_at = _started_at_for(ppid)
                summary = f"ppid={ppid},source=cc,started_at={started_at}"
                if is_worktree:
                    summary += ",worktree=1"
                with conn:
                    conn.execute(
                        "INSERT INTO audit_log"
                        " (target_table, target_id, action, summary)"
                        " VALUES ('events', ?, 'session_lifecycle:start', ?)",
                        (sid, summary),
                    )
            except Exception:  # noqa: BLE001 — never block session_start
                pass
            # B1 cli half: every cc session (cli or bridge-spawned) lands a row in
            # `sessions` so /resume's recent-picker sees all channels. Channel
            # hint from MARROW_CHANNEL env (bridge sets =wx; default cli).
            # No-op for worktree sessions to keep /resume focused on real work.
            if not is_worktree:
                try:
                    channel = os.environ.get("MARROW_CHANNEL") or "cli"
                    # cli: peek ppid argv for --model claude-opus-X[1m] so the
                    # picker can display the [1M] tag (cc jsonl drops it).
                    cli_model = (
                        _cli_model_from_ppid(os.getppid())
                        if channel == "cli" else None
                    )
                    repo.upsert_session(sid, cli_model, channel, db=db)
                except Exception:  # noqa: BLE001 — never block session_start
                    pass

        if is_worktree:
            # Worktree session: task-isolated work, no personal memory needed.
            ctx = ""
        else:
            parts: list[str] = []

            # Heartbeat block goes first so it is never buried.
            heartbeat = _affect_heartbeat(conn)
            if heartbeat:
                parts.append(heartbeat)

            parts.append(_handoff_text(conn))

            backdrop = top_sections.render_affect(conn)
            if backdrop:
                parts.append(backdrop)

            ctx = "\n\n".join(p for p in parts if p)

            # Hard cap: never exceed 6000 chars total for SessionStart.
            if len(ctx) > SESSION_START_HARD_CAP:
                ctx = ctx[: SESSION_START_HARD_CAP - 1] + "…"
    finally:
        conn.close()

    json.dump(
        {"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }},
        sys.stdout,
    )
    return 0


def session_end() -> int:
    inp = _read_input()
    tpath = inp.get("transcript_path")
    if not tpath:
        return 0
    if transcript.is_headless(tpath):
        return 0  # spawned claude -p fires SessionEnd too; not our session

    # Worktree-session gate: cc instances launched inside a NON-primary git
    # worktree are task-isolated runs; their dialogue must not enter marrow.
    # Skip archive_events + LLM spawn entirely. Still write lifecycle:end so
    # catchup doesn't tag this sid as silent_death.
    cwd = inp.get("cwd") or ""
    if _is_worktree_session(cwd):
        sid = inp.get("session_id") or ""
        if sid:
            try:
                _conn = storage.connect(config.db_path())
                try:
                    with _conn:
                        _conn.execute(
                            "INSERT INTO audit_log"
                            " (target_table, target_id, action, summary)"
                            " VALUES ('events', ?, 'session_lifecycle:end', 'worktree=1')",
                            (sid,),
                        )
                finally:
                    _conn.close()
            except Exception:  # noqa: BLE001 — never block session_end
                pass
        return 0

    db = config.db_path()
    conn = storage.connect(db)
    try:
        # mm- block gate: if Lumi typed mm- at any point during this session,
        # _handle_mm_prefix wrote a session_block=archive flag. Skip the entire
        # archive path so events table receives ZERO rows for this sid. Still
        # write lifecycle:end so catchup doesn't flag this as silent_death.
        early_sid = (inp.get("session_id") or "").strip()
        if early_sid and _is_session_blocked(conn, early_sid):
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO audit_log"
                        " (target_table, target_id, action, summary)"
                        " VALUES ('events', ?, 'session_lifecycle:end', 'mm_minus_blocked')",
                        (early_sid,),
                    )
            except Exception:  # noqa: BLE001 — never block session_end
                pass
            _wipe_recall_seen(early_sid)
            return 0

        rows = transcript.clean(tpath)
        if rows:
            repo.archive_events(conn, rows)

        # ── CRITICAL PATH (must complete within ms of archive) ────────────────
        # cc reaps the whole hook process group on session close. dashboard
        # write + embed_pending below run for seconds and routinely get
        # killed mid-run, which used to also kill the lifecycle:end INSERT
        # and the popen spawn -> sids stuck with no terminal marker. Keep
        # this block tight and ahead of every slow side-effect.
        sid = rows[0]["session_id"] if rows else None
        if sid:
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO audit_log"
                        " (target_table, target_id, action, summary)"
                        " VALUES ('events', ?, 'session_lifecycle:end', '')",
                        (sid,),
                    )
            except Exception:  # noqa: BLE001
                pass
            # Drop per-session recall dedup state — next window starts clean.
            _wipe_recall_seen(sid)

            # Bridge gate: when synapse-wx wraps cc, it owns sessionend timing
            # (fires on 6h idle, not on every /model swap). Archive runs, marker
            # written, popen suppressed. Catchup honors the bridge_owns marker
            # until a later fail row (manual fire that failed) supersedes it.
            if os.environ.get("MARROW_BRIDGE") == "1":
                try:
                    _write_manual_skip_flag(conn, sid, _STATUS_SKIP_BRIDGE_OWNS)
                except Exception:  # noqa: BLE001
                    pass
                return 0

            # Idempotent gate: skip popen if events haven't grown since last ok.
            skip_spawn = False
            try:
                last_ok = _last_ok_user_count(conn, sid)
                if last_ok is not None:
                    current_user = conn.execute(
                        "SELECT COUNT(*) c FROM events"
                        " WHERE session_id=? AND role='user'",
                        (sid,),
                    ).fetchone()["c"]
                    if current_user <= last_ok:
                        skip_spawn = True
            except Exception:  # noqa: BLE001 — gate failure → safe to spawn
                pass

            if not skip_spawn:
                log = config.DATA_DIR / "logs" / f"sessionend_async_{sid}.log"
                # cwd lets sessionend_async locate the repo for git_log evidence.
                # Absent (study / ny chat) → "" → _load_git_log returns "".
                cwd = inp.get("cwd") or ""
                try:
                    popen_detach(
                        [sys.executable, "-m", "marrow.sessionend_async",
                         "--sid", sid, "--cwd", cwd, "--log-path", str(log)],
                        log_path=log,
                    )
                except Exception as e:  # noqa: BLE001
                    try:
                        repo.add_alert(
                            "warn", "sessionend_async",
                            f"session_end async spawn failed: {e}",
                            source="hooks.py", db=db,
                        )
                    except Exception:  # noqa: BLE001
                        pass

    finally:
        conn.close()
    return 0


_SID_RE = _re.compile(
    r"^[0-9a-f]{8}(-[0-9a-f]{4}){0,3}(-[0-9a-f]{4,12})?$",
    _re.IGNORECASE,
)


def _looks_like_sid(arg: str) -> bool:
    """Return True if arg matches a full UUID or a short hex-prefix Lumi might type."""
    return bool(_SID_RE.match(arg.strip())) if arg and " " not in arg else False


def _inject_silent_ack(prefix: str) -> None:
    """Tell the LLM this prompt is a control signal — reply minimally, no chatter."""
    ctx = (
        f"## {prefix} control signal\n"
        f"念念发的 `{prefix}` 是 marrow skip/rerun 控制信号，不是对话。\n"
        f"hook 已经处理 (manual_skip / sessionend rerun)。\n"
        f"无需任何回话，只用一个极短动作或一个字回应。"
    )
    json.dump(
        {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }},
        sys.stdout,
    )


def _inject_locate_request(prefix: str, clue: str) -> None:
    """Write a UserPromptSubmit additionalContext asking the LLM to locate the sid."""
    action = "mm+ <full-sid>" if prefix == "mm+" else "mm- <full-sid>"
    ctx = (
        f"## {prefix} 定位请求\n"
        f"念念发了 `{prefix} <clue>`，clue 不是有效 sid 格式。请帮她定位目标 session：\n"
        f"- clue 原文: {clue}\n"
        f"- 建议查询: events / audit_log 表中匹配 timestamp / content / role 的 sid\n"
        f"- 找到后用 `{action}` 重新触发，或调用 `mw sessionend rerun <sid>`"
    )
    json.dump(
        {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }},
        sys.stdout,
    )


def _locate_jsonl(sid: str) -> str | None:
    """Glob ~/.claude/projects/**/<sid>.jsonl; return most-recent match or None."""
    import pathlib
    matches = list(pathlib.Path.home().glob(f".claude/projects/**/{sid}.jsonl"))
    if not matches:
        return None
    return str(max(matches, key=lambda p: p.stat().st_mtime))


def _pre_archive_jsonl(conn: sqlite3.Connection, tpath: str | None) -> None:
    """Archive events from an active-session jsonl. Fail-soft — never raises."""
    if not tpath:
        return
    try:
        if transcript.is_headless(tpath):
            return
        rows = transcript.clean(tpath)
        if rows:
            repo.archive_events(conn, rows)
    except Exception:  # noqa: BLE001
        pass


def _handle_mm_prefix(inp: dict) -> bool:
    """Handle mm- / mm+ prefixes. Returns True if handled (skip further processing).

    mm-: writes manual_skip audit row for current (or named) sid.
    mm+ / mm-: three-branch on arg after prefix:
      - empty          → current sid (existing behaviour)
      - UUID-like      → named sid
      - natural-lang   → inject additionalContext to help LLM locate sid; no spawn
    Fail-soft: any error is swallowed — hook must never block the user turn.
    """
    prompt = (inp.get("prompt") or "").strip()
    if not (prompt.startswith("mm-") or prompt.startswith("mm+")):
        return False

    sid = (inp.get("session_id") or "").strip()
    prefix = prompt[:3]
    rest = prompt[3:].strip()

    # Natural-language branch — hand off to main LLM, no DB writes, no spawn.
    if rest and not _looks_like_sid(rest):
        _inject_locate_request(prefix, rest)
        return True

    # Empty or UUID-like arg: empty → current sid, UUID-like → that sid.
    target_sid = rest if rest else sid

    try:
        db = config.db_path()
        conn = storage.connect(db)
        try:
            if prefix == "mm-":
                if target_sid:
                    _write_manual_skip_flag(conn, target_sid, _STATUS_SKIP)
                    # Block events archive entirely for this sid — session_end
                    # will skip transcript.clean + archive_events, leaving the
                    # events table with zero rows for this session.
                    _write_session_block_flag(conn, target_sid, _STATUS_BLOCK_ARCHIVE)
            else:  # mm+
                if target_sid:
                    # Force-clear any done marker so sessionend_async reruns.
                    with conn:
                        conn.execute(
                            "INSERT INTO audit_log"
                            " (target_table, target_id, action, summary)"
                            " VALUES ('events', ?, 'sessionend_extract', 'reset:mm_plus')",
                            (target_sid,),
                        )
                    # Last-wins: clear any prior mm- flags so session_end runs
                    # archive_events normally and sessionend_async runs the LLM
                    # pipeline. Without this, mm- would permanently win.
                    _write_manual_skip_flag(conn, target_sid, _STATUS_SKIP_CLEARED)
                    _write_session_block_flag(conn, target_sid, _STATUS_BLOCK_CLEARED)
                    # Active-session pre-archive: events table is empty until
                    # SessionEnd fires. Archive now so sessionend_async finds rows.
                    if target_sid == sid:
                        tpath = inp.get("transcript_path") or _locate_jsonl(target_sid)
                        _pre_archive_jsonl(conn, tpath)
                    conn.close()
                    conn = None
                    log = config.DATA_DIR / "logs" / f"sessionend_async_{target_sid}.log"
                    popen_detach(
                        [sys.executable, "-m", "marrow.sessionend_async",
                         "--sid", target_sid, "--log-path", str(log)],
                        log_path=log,
                    )
        finally:
            if conn is not None:
                conn.close()
    except Exception:  # noqa: BLE001 — never block prompt
        pass
    if prefix == "mm-":
        _inject_silent_ack("mm-")
    return True


def user_prompt_submit() -> int:
    """Inject top-K recall hits as UserPromptSubmit additionalContext.

    Also handles mm- (manual skip) and mm+ (sessionend rerun) prefixes.
    Config flag: [recall] vector = true (default on). Set false to disable.
    Fusion weights come from [recall] in config; recall.recall_fusion blends
    vec + bm25 + recency + affect. Fail-soft: any error falls through to a
    no-op so the user prompt always reaches the model.
    """
    inp = _read_input()

    # mm- / mm+ control plane — check before recall, independent of recall config.
    if isinstance(inp, dict) and _handle_mm_prefix(inp):
        return 0  # no additionalContext injection for control prompts

    # Worktree-session gate: cc instances in a NON-primary git worktree are
    # task-isolated runs. They take direction from the user prompt + main
    # session only; no personal recall context, no token spend on hits the
    # worktree session can't act on anyway.
    cwd = inp.get("cwd") if isinstance(inp, dict) else None
    if _is_worktree_session(cwd or ""):
        return 0

    prompt_text = (inp.get("prompt") or "").strip() if isinstance(inp, dict) else ""
    sid = inp.get("session_id") if isinstance(inp, dict) else None

    # Sticky title for wx /resume picker — runs regardless of recall config.
    _maybe_set_session_title(sid, prompt_text)

    cfg = config.load()
    if not cfg.get("recall", {}).get("vector", False):
        return 0

    if not prompt_text:
        return 0

    rcfg = cfg.get("recall", {})
    ctx_n = int(rcfg.get("event_context_window", 1))
    event_max = int(rcfg.get("event_max_chars", 150))
    budget_chars = int(rcfg.get("budget_chars", 800))
    try:
        from . import recall as recall_mod
        conn = storage.connect(config.db_path())
        try:
            hits = recall_mod.recall_with_config(conn, prompt_text)
            # Attach ±N adjacent same-session turns to each event hit.
            if ctx_n > 0:
                for h in hits:
                    if h.get("kind") in (None, "event") and h.get("session_id") and h.get("id"):
                        h["_context"] = recall_mod.fetch_event_context(
                            conn, h["session_id"], int(h["id"]), n=ctx_n
                        )
        finally:
            conn.close()
    except Exception:
        return 0  # fail-soft: never break the user turn

    if not hits:
        return 0

    # ── per-session dedup: drop hits already injected this session ────────────
    seen = _load_recall_seen(sid)
    visible: list[dict] = []
    for h in hits:
        hid = int(h.get("id") or 0)
        kind = h.get("kind") or "event"
        if hid and (kind, hid) in seen:
            continue  # already shown — skip slot, no backfill
        visible.append(h)
        if hid:
            seen.add((kind, hid))
    if not visible:
        return 0
    _save_recall_seen(sid, seen)

    lines = [
        "## Recall (auto) — passive context, do not answer",
        "> 命中可能不全；相关或缺失 → mcp__marrow__recall",
        "",
    ]
    for h in visible:
        ts = (h.get("timestamp") or "")[:10]
        kind = h.get("kind") or "event"
        content_full = (h.get("content") or "").replace("\n", " ")
        if kind in _TABLE_KINDS:
            # Anchor rows ship full content — they're already short and dense.
            lines.append(f"- [{ts}] {content_full}")
            continue
        # Event: main + ↑prev + ↓next combined ≤ event_max chars (content only).
        ctxs = h.get("_context") or []
        main_cap = max(40, event_max - 60) if ctxs else event_max
        main = content_full[:main_cap]
        lines.append(f"- [{ts}] {main}")
        remaining = max(0, event_max - len(main))
        if ctxs and remaining > 0:
            per_ctx = max(0, remaining // len(ctxs))
            for c in ctxs:
                if per_ctx <= 0:
                    break
                cts = (c.get("timestamp") or "")[:16].replace("T", " ")
                csnip = (c.get("content") or "").replace("\n", " ")[:per_ctx]
                if not csnip:
                    continue
                arrow = "↑" if c.get("rel") == "prev" else "↓"
                lines.append(f"    {arrow} [{cts}] ({c.get('role')}) {csnip}")
    ctx = "\n".join(lines)
    # Final backstop: cap injected block at budget_chars (kind-blind tail trim).
    if len(ctx) > budget_chars:
        ctx = ctx[:budget_chars]

    # Side log — markdown append so VSCode preview / tail both readable.
    # Mirror what actually got injected: dedup-filtered `visible`, not raw hits.
    try:
        _append_recall_log(prompt_text, visible)
    except Exception:
        pass

    json.dump(
        {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }},
        sys.stdout,
    )
    return 0


def _append_recall_log(prompt_text: str, hits: list[dict]) -> None:
    """Append one markdown block per turn to ~/.config/marrow/logs/recall.md.

    Each block: timestamp header + prompt (truncated) + bullet list of hits
    with kind, id, score, content snippet. Open in VSCode → preview reads
    cleanly; `tail -F` also legible.
    """
    log_dir = Path.home() / ".config" / "marrow" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "recall.md"
    now = datetime.now(timezone.utc).astimezone()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    prompt_oneline = prompt_text.replace("\n", " ")[:200]
    parts = [f"\n### {ts} · prompt: {prompt_oneline}", ""]
    for h in hits:
        kind = h.get("kind") or "event"
        hid = h.get("id", "?")
        score = h.get("score", 0.0)
        content = (h.get("content") or "").replace("\n", " ")
        # Mirror injection-side shaping: anchor tables ship full content
        # (rows are short + dense); only event hits get the 120-char cap.
        snip = content if kind in _TABLE_KINDS else content[:120]
        parts.append(f"- `{kind}#{hid}` score={score:.2f} — {snip}")
        for c in h.get("_context", []) or []:
            arrow = "↑prev" if c.get("rel") == "prev" else "↓next"
            cs = (c.get("content") or "").replace("\n", " ")[:80]
            parts.append(f"    - {arrow} ({c.get('role')}) {cs}")
    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")


_PLACEMENT_BASH_OPS = {"mv", "cp", "rename", "mmv", "touch", "mkdir"}


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

        _literal = "[Path] Use paths with /, not bare filenames."

        def _emit(text: str) -> None:
            # PreToolUse JSON output -> additionalContext is the only stdout form
            # cc injects into assistant context. Plain stdout only hits transcript.
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": text,
                }
            }))

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
            _emit(_literal)
            return 0

        # Resolve target path
        if not target_path_str:
            _emit(_literal)
            return 0

        target = Path(target_path_str).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        target = target.resolve()

        # Check against AUTHORIZED_ROOTS
        from . import atlas as _atlas_mod
        from . import drift_sweep
        from . import storage, config
        roots = [r.expanduser().resolve() for r in drift_sweep.AUTHORIZED_ROOTS]

        root = _atlas_mod._root_of(str(target), roots)
        if root is None:
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
            repo.add_alert("info", "atlas_hook", str(e), source="hooks.py",
                           db=config.db_path())
        except Exception:
            pass
    return 0


_EVENTS = {
    "session_start": session_start,
    "session_end": session_end,
    "user_prompt_submit": user_prompt_submit,
    "pretool_use": pretool_use,
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
            repo.add_alert("warn", "hook", f"{args[0]} failed: {e}",
                           source="hooks.py", db=config.db_path())
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
