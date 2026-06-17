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
from .popen_detach import popen_detach, popen_detach_lazy
from .timeutil import utc_iso_to_local_date, utc_iso_to_local_datetime, format_recall_ts

_RECALL_TZ = config.get_tz()
_RECALL_CUTOFF_H = 6  # 6AM local day boundary (matches digest)


# ── recall dedup state (per-session, hook-only) ──────────────────────────────

_TABLE_KINDS = {"milestone", "memes", "entity", "diary", "task"}

# Strip WX-injected `[time: ... | gap: ...]` prefix from event content.
# recall.py strips it for the main-hit content; mirror here for neighbors + log.
_WX_TIME_PREFIX_RE = _re.compile(r"^\[time:[^\]]+\]\s*")


def _strip_wx_time_prefix(s: str) -> str:
    return _WX_TIME_PREFIX_RE.sub("", s or "")


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


def _sticker_nudge_path(sid: str) -> Path:
    return config.DATA_DIR / "state" / "sticker_nudge" / f"{sid}.json"


def _load_sticker_nudge(sid: str) -> dict:
    if not sid:
        return {"turn_count": 0, "last_sticker_turn": 0}
    try:
        return json.loads(_sticker_nudge_path(sid).read_text())
    except Exception:
        return {"turn_count": 0, "last_sticker_turn": 0}


def _save_sticker_nudge(sid: str, state: dict) -> None:
    if not sid:
        return
    p = _sticker_nudge_path(sid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state))
    except Exception:
        pass


def _wipe_sticker_nudge(sid: str) -> None:
    if not sid:
        return
    try:
        _sticker_nudge_path(sid).unlink(missing_ok=True)
    except Exception:
        pass


def _recall_log_dir() -> Path:
    """~/.config/marrow/logs/recall/ — created on first use."""
    d = config.DATA_DIR / "logs" / "recall"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _recall_local_date(utc_now: datetime) -> str:
    """UTC datetime → local recall-day string (YYYY-MM-DD) with 6AM cutoff."""
    local = utc_now.astimezone(_RECALL_TZ) - timedelta(hours=_RECALL_CUTOFF_H)
    return local.date().isoformat()


def _recall_session_log_path(sid: str, utc_now: datetime) -> Path:
    """Per-session recall log: recall/recall-YYYY-MM-DD-<sid8>.md."""
    day = _recall_local_date(utc_now)
    sid8 = (sid or "unknown")[:8]
    return _recall_log_dir() / f"recall-{day}-{sid8}.md"


def _prune_recall_logs() -> None:
    """Delete recall log files older than today-1 (keep today + yesterday).

    Mirrors digest prune: 6AM cutoff for local-day boundary, mtime-based
    safety floor, today/yesterday whitelisted by filename."""
    try:
        now = datetime.now(timezone.utc)
        today = _recall_local_date(now)
        yesterday = _recall_local_date(now - timedelta(days=1))
        cutoff = now.timestamp() - 1.5 * 24 * 3600
        log_dir = _recall_log_dir()
        for f in log_dir.glob("recall-*.md"):
            name = f.stem  # "recall-YYYY-MM-DD-<sid8>"
            parts = name.split("-", 4)  # ["recall", "YYYY", "MM", "DD", "<sid8>"]
            if len(parts) < 5:
                continue
            date_part = "-".join(parts[1:4])
            if date_part in (today, yesterday):
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — prune is best-effort
        pass


def _sweep_empty_async_logs() -> None:
    """Drop 0-byte sessionend_async_*.log left behind when cc SIGKILLs the
    detached child before its atexit cleanup runs. Only matches the exact
    prefix + .log suffix so recall/ logs / unrelated files stay safe."""
    log_dir = config.DATA_DIR / "logs"
    try:
        for p in log_dir.glob("sessionend_async_*.log"):
            try:
                if p.stat().st_size == 0:
                    p.unlink()
            except (FileNotFoundError, OSError):
                pass
    except Exception:  # noqa: BLE001 — never block session_start
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


def _was_worktree_session_at_start(conn: sqlite3.Connection, sid: str) -> bool:
    """True iff this sid's SessionStart wrote a worktree=1 marker.

    Trust SessionStart's judgement over a live re-check at SessionEnd time:
    cc reports inp.cwd as the launch cwd, which may have been a worktree
    that has since been torn down (or `cd`'d out of) — re-running
    _is_worktree_session against that stale cwd falsely returns False and
    drops the session into the main archive path, where empty rows silently
    suppresses lifecycle:end. Pin the verdict at start instead.
    """
    if not sid:
        return False
    row = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='session_lifecycle:start' AND target_id=?"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return bool(row and "worktree=1" in (row["summary"] or ""))


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


def _maybe_set_session_model(sid: str | None) -> None:
    """Sticky model upsert — backfill `sessions.model` from cc's launch argv
    when it's still empty.

    Session_start already runs `_cli_model_from_ppid`, but cli sessions that
    die before cc emits its first system/init never get the model written
    anywhere — jsonl is empty too, so the wx /resume picker shows `?`. Doing
    the same lookup at every `user_prompt_submit` cheaply repairs that gap
    for any session that survives long enough to take a prompt.
    """
    if not sid:
        return
    try:
        cur = repo.get_session(sid)
        if cur and (cur.get("model") or "").strip():
            return  # already set
        channel = (cur or {}).get("channel") or os.environ.get("MARROW_CHANNEL") or "cli"
        if channel != "cli":
            return  # wx writes its own model via swap_provider
        model = _cli_model_from_ppid(os.getppid())
        if not model:
            return
        repo.upsert_session(sid, model, channel)
    except Exception:  # noqa: BLE001 — never block user prompt
        pass


def _maybe_set_session_title(sid: str | None, prompt_text: str) -> None:
    """Two-stage session title for the wx /resume picker.

    Stage 1 (sync) — first prompt: write the prompt's head line (≤40 chars)
    as a placeholder so the picker is never blank.
    Stage 2 (async) — every prompt after that: fire a detached
    ``marrow.title`` subprocess that LLM-summarises the conversation into
    a ≤8-unit title (cn chars OR en words), follows the user's dominant
    language, and writes it back to ``sessions.title``. The audit_log
    dedup inside ``title.summarize`` makes the LLM call run exactly once
    per session.
    """
    if not sid:
        return
    try:
        cur = repo.get_session(sid)
        if (not cur or not (cur.get("title") or "").strip()) and prompt_text:
            head = prompt_text.splitlines()[0].strip()
            head = _re.sub(r"\s+", " ", head)[:40]
            if head:
                channel = (cur or {}).get("channel") or os.environ.get("MARROW_CHANNEL") or "cli"
                repo.upsert_session(sid, None, channel, title=head)
        _maybe_fire_title_summarize(sid)
    except Exception:  # noqa: BLE001 — never block user prompt
        pass


def _maybe_fire_title_summarize(sid: str) -> None:
    """Detached `python -m marrow.title --sid <sid>` for the LLM summariser.

    Pre-checks ``audit_log`` inline (cheap SELECT) so an already-titled
    session does not even fork — only sessions still eligible for
    summarisation pay the popen cost.
    """
    if not sid:
        return
    try:
        conn = storage.connect(config.db_path())
        try:
            row = conn.execute(
                "SELECT 1 FROM audit_log "
                "WHERE action='title_summarize' AND target_table='sessions' AND target_id=? "
                "LIMIT 1",
                (sid,),
            ).fetchone()
            if row:
                return  # sticky — already summarised
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return
    try:
        log_path = Path(config.DATA_DIR) / "logs" / "title_summarize.log"
        popen_detach(
            [sys.executable, "-m", "marrow.title", "--sid", sid],
            log_path,
        )
    except Exception:  # noqa: BLE001 — fire-and-forget
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




def session_start() -> int:
    try:
        log = config.DATA_DIR / "logs" / "sessionstart_catchup.log"
        popen_detach([sys.executable, "-m", "marrow.sessionstart_catchup"], log_path=log)
    except Exception as e:
        try:
            repo.add_alert("warn", "catchup",
                           "catchup_spawn_failed:hooks",
                           source="hooks.py", db=config.db_path(),
                           message=f"session_start catchup spawn failed: {e}")
        except Exception:
            pass
    # Recall housekeeping — prune day-2+ logs from recall/ dir + wipe per-session
    # dedup state so every fresh window starts with a clean recall slate.
    _prune_recall_logs()
    # Sweep 0-byte sessionend_async_*.log residues (SIGKILL fallback).
    _sweep_empty_async_logs()
    inp = _read_input()
    db = config.db_path()
    conn = storage.connect(db)
    try:
        # Write lifecycle:start marker so catchup can detect live vs dead sessions.
        sid = inp.get("session_id") if isinstance(inp, dict) else None
        cwd = inp.get("cwd") if isinstance(inp, dict) else None
        tpath = inp.get("transcript_path") if isinstance(inp, dict) else None
        is_worktree = _is_worktree_session(cwd or "")
        # Subagent (Task tool dispatch) — task-isolated like worktree;
        # no personal memory / no /resume tracking.
        is_subagent = bool(tpath and "/tasks/" in tpath)
        if sid:
            # Fresh window or resume — drop prior recall dedup state either way
            # (cheap; resume re-shows seen rows once, acceptable).
            _wipe_recall_seen(sid)
            _wipe_sticker_nudge(sid)
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
            # No-op for worktree / subagent sessions to keep /resume focused
            # on real work.
            if not is_worktree and not is_subagent:
                try:
                    channel = os.environ.get("MARROW_CHANNEL") or "cli"
                    # cli: peek ppid argv for --model claude-opus-X[1m] so the
                    # picker can display the [1M] tag (cc jsonl drops it).
                    cli_model = (
                        _cli_model_from_ppid(os.getppid())
                        if channel == "cli" else None
                    )
                    repo.upsert_session(sid, cli_model, channel, cwd=cwd, db=db)
                except Exception:  # noqa: BLE001 — never block session_start
                    pass

        if is_worktree or is_subagent:
            # Task-isolated (git worktree or Task-tool subagent): no
            # personal memory injection.
            ctx = ""
        else:
            parts: list[str] = []

            # Heartbeat block goes first so it is never buried.
            heartbeat = _affect_heartbeat(conn)
            if heartbeat:
                parts.append(heartbeat)

            alert_rows = conn.execute(
                "SELECT id, severity, type, message FROM alerts WHERE resolved = 0 ORDER BY id"
            ).fetchall()
            alert_block = ""
            if alert_rows:
                header = f"Alerts: {len(alert_rows)} unresolved"
                alert_lines = [header]
                budget = 500 - len(header)
                for ar in alert_rows:
                    line = f"  #{ar['id']} [{ar['severity']}] {ar['type']}: {ar['message']}"
                    if len(line) > 80:
                        line = line[:79] + "…"
                    if budget - len(line) - 1 < 0:
                        alert_lines.append(f"  … +{len(alert_rows) - len(alert_lines) + 1} more")
                        break
                    budget -= len(line) + 1
                    alert_lines.append(line)
                alert_block = "\n".join(alert_lines)
                parts.append(alert_block)

            from . import timeline as _timeline_mod
            backdrop = _timeline_mod.render_timeline(conn)
            if backdrop:
                parts.append(backdrop)

            ctx = "\n\n".join(p for p in parts if p)

            try:
                conn.execute(
                    "INSERT INTO audit_log (target_table, action, summary) VALUES (?, ?, ?)",
                    (
                        "sessions",
                        "session_start:zones",
                        f"hb={len(heartbeat or '')} alerts={len(alert_block)}"
                        f" tl={len(backdrop or '')} total={len(ctx)}",
                    ),
                )
                conn.commit()
            except Exception:
                pass
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

    cwd = inp.get("cwd") or ""
    early_sid = (inp.get("session_id") or "").strip()
    db = config.db_path()
    conn = storage.connect(db)
    try:
        # Worktree-session gate: cc instances launched inside a NON-primary git
        # worktree are task-isolated runs; their dialogue must not enter marrow.
        # Skip archive_events + LLM spawn entirely. Still write lifecycle:end so
        # catchup doesn't tag this sid as silent_death.
        #
        # Pin verdict on SessionStart marker first: cwd at SessionEnd time can
        # be stale (worktree torn down, cd'd out) which would silently drop the
        # session into the main path. Live cwd check kept as a fallback for
        # sessions whose SessionStart hook never ran.
        is_worktree = (
            _was_worktree_session_at_start(conn, early_sid)
            or _is_worktree_session(cwd)
        )
        if is_worktree:
            if early_sid:
                try:
                    with conn:
                        conn.execute(
                            "INSERT INTO audit_log"
                            " (target_table, target_id, action, summary)"
                            " VALUES ('events', ?, 'session_lifecycle:end', 'worktree=1')",
                            (early_sid,),
                        )
                except Exception:  # noqa: BLE001 — never block session_end
                    pass
            return 0

        # mm- block gate: if the user typed mm- at any point during this session,
        # _handle_mm_prefix wrote a session_block=archive flag. Skip the entire
        # archive path so events table receives ZERO rows for this sid. Still
        # write lifecycle:end so catchup doesn't flag this as silent_death.
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
            _wipe_sticker_nudge(early_sid)
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
        #
        # sid fallback: cc always sends session_id in the hook payload, but
        # rows can be empty (transcript not yet flushed, all messages
        # filtered out, etc). Don't rely on rows[0] — that path silently
        # dropped lifecycle:end for thousands of sessions and bred 760
        # silent_death alerts in one hour on 2026-06-05.
        sid = (
            (rows[0]["session_id"] if rows else None)
            or early_sid
            or None
        )
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
            _wipe_sticker_nudge(sid)

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
                    # _lazy variant: child opens log on first write only,
                    # so silent paths (skip / already_done) leave no file.
                    popen_detach_lazy(
                        [sys.executable, "-m", "marrow.sessionend_async",
                         "--sid", sid, "--cwd", cwd, "--log-path", str(log)],
                        log_path=log,
                    )
                except Exception as e:  # noqa: BLE001
                    try:
                        repo.add_alert(
                            "warn", "sessionend_async",
                            "sessionend_spawn_failed",
                            message=f"spawn failed: {e}",
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
    """Return True if arg matches a full UUID or a short hex-prefix the user might type."""
    return bool(_SID_RE.match(arg.strip())) if arg and " " not in arg else False


def _inject_silent_ack(prefix: str) -> None:
    """Tell the LLM this prompt is a control signal — reply minimally, no chatter."""
    user_name = config.persona()["user_name"]
    ctx = (
        f"## {prefix} control signal\n"
        f"{user_name}发的 `{prefix}` 是 marrow skip/rerun 控制信号，不是对话。\n"
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
    user_name = config.persona()["user_name"]
    ctx = (
        f"## {prefix} 定位请求\n"
        f"{user_name}发了 `{prefix} <clue>`，clue 不是有效 sid 格式。请帮她定位目标 session：\n"
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
                    popen_detach_lazy(
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


# ── pure recall-render helpers (extracted for testability) ───────────────────

def _apply_rel_cutoff(hits: list[dict], rel_cutoff: float) -> list[dict]:
    """Drop hits whose score < top_score * rel_cutoff. Returns filtered list."""
    if not hits:
        return []
    top_score = hits[0].get("score", 0.0)
    cutoff = top_score * rel_cutoff
    return [h for h in hits if (h.get("score") or 0.0) >= cutoff]


def _render_hit_block(rank: int, h: dict, rank_caps: list[int]) -> list[str]:
    """Return the markdown lines for one recall hit at the given rank.

    rank_caps[rank] (falling back to rank_caps[-1]) controls max content chars.
    Context turns (h['_context']) are only rendered for rank-0 event hits.
    Pure function — no I/O, no DB access.
    """
    cap = rank_caps[rank] if rank < len(rank_caps) else rank_caps[-1]
    block: list[str] = []
    ts = format_recall_ts(h.get("timestamp") or "")
    kind = h.get("kind") or "event"
    content_full = (h.get("content") or "").replace("\n", " ")
    if kind in _TABLE_KINDS:
        block.append(f"- {ts} {content_full[:cap]}")
    else:
        ctxs = h.get("_context") or [] if rank == 0 else []
        main_cap = max(40, cap - 60) if ctxs else cap
        main = content_full[:main_cap]
        block.append(f"- {ts} {main}")
        remaining = max(0, cap - len(main))
        if ctxs and remaining > 0:
            per_ctx = max(0, remaining // len(ctxs))
            for c in ctxs:
                if per_ctx <= 0:
                    break
                cts = utc_iso_to_local_datetime(c.get("timestamp") or "")
                csnip = _strip_wx_time_prefix(
                    (c.get("content") or "").replace("\n", " ")
                )[:per_ctx]
                if not csnip:
                    continue
                arrow = "↑" if c.get("rel") == "prev" else "↓"
                block.append(f"    {arrow} [{cts}] ({c.get('role')}) {csnip}")
    return block


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

    # Worktree / subagent gate: cc instances in a NON-primary git worktree
    # OR dispatched via Task tool (transcript_path under /tasks/) are
    # task-isolated runs. They take direction from the user prompt + main
    # session only; no personal recall context.
    cwd = inp.get("cwd") if isinstance(inp, dict) else None
    tpath = inp.get("transcript_path") if isinstance(inp, dict) else None
    is_subagent = bool(tpath and "/tasks/" in tpath)
    if _is_worktree_session(cwd or "") or is_subagent:
        return 0

    # cwd exclude gate — opt-out per-dir via config.toml [recall].exclude_cwds.
    _ex_cwds = config.load().get("recall", {}).get("exclude_cwds", []) or []
    if cwd and any(cwd.startswith(p) for p in _ex_cwds):
        return 0

    prompt_text = (inp.get("prompt") or "").strip() if isinstance(inp, dict) else ""
    sid = inp.get("session_id") if isinstance(inp, dict) else None

    # Pipeline-prompt gate: a hand-run digest/eval claude (spawned without
    # llm.py's --setting-sources isolation) still loads this hook. Its prompt
    # opens with the transcript fence from sessionend_prompts._TRANSCRIPT_BLOCK
    # — never inject, log, or backfill title/model for it.
    if prompt_text.startswith("===== BEGIN ORIGINAL TRANSCRIPT"):
        return 0

    # Sticker nudge: increment turn counter; flag nudge if 10 turns since last sticker.
    _nudge_line: str | None = None
    if sid and os.environ.get("MARROW_BRIDGE") == "1":
        try:
            _sn = _load_sticker_nudge(sid)
            _sn["turn_count"] = _sn.get("turn_count", 0) + 1
            if _sn["turn_count"] - _sn.get("last_sticker_turn", 0) >= 10:
                user_name = config.persona()["user_name"]
                _nudge_line = f"你怎么还不发表情包，{user_name}都等急了——翻翻 sticker_search 找个应景的发一下。"
                _sn["last_sticker_turn"] = _sn["turn_count"]
            _save_sticker_nudge(sid, _sn)
        except Exception:
            pass

    # Sticky title + model backfill for wx /resume picker — run regardless
    # of recall config so short-lived cli sessions still get a model written.
    _maybe_set_session_model(sid)
    _maybe_set_session_title(sid, prompt_text)
    try:
        repo.touch_session_active(sid, db=config.db_path())
    except Exception:  # noqa: BLE001 — best-effort timestamp bump
        pass

    cfg = config.load()
    if not cfg.get("recall", {}).get("vector", False):
        return 0

    if not prompt_text:
        return 0

    # Strip synapse-wx bridge boilerplate before recall so media Read
    # instructions / merge notes / dot sentinels never become query needles.
    # Emptiness is judged with the [time: ...] anchor ALSO removed (recall.py
    # strips it internally anyway) so a pure-media bubble skips recall.
    from .transcript import strip_wx_boilerplate as _strip_wx, strip_harness_markers as _strip_harness
    recall_query = _strip_harness(_strip_wx(prompt_text))
    if not recall_query or not _WX_TIME_PREFIX_RE.sub("", recall_query).strip():
        return 0

    rcfg = cfg.get("recall", {})
    ctx_n = int(rcfg.get("event_context_window", 1))
    budget_chars = int(rcfg.get("budget_chars", 800))
    timelane_budget = int(rcfg.get("timelane_budget", 400))
    _default_rank_caps = [300, 120, 120, 40, 40]
    rank_caps: list[int] = rcfg.get("rank_caps", _default_rank_caps) or _default_rank_caps
    rel_cutoff: float = float(rcfg.get("rel_cutoff", 0.6))

    # ── time-lane: detect cue, run windowed recall first ─────────────────────
    windowed_hits: list[dict] = []
    cue = None
    try:
        from .timecue import parse_time_cue
        cue = parse_time_cue(recall_query)
    except Exception:
        cue = None

    seen = _load_recall_seen(sid)

    if cue is not None:
        try:
            from . import recall as recall_mod
            conn = storage.connect(config.db_path())
            try:
                _stripped = cue.stripped.strip()
                # Check if stripped text has substantive content
                _has_content = bool(
                    len([c for c in _stripped if "一" <= c <= "鿿"]) >= 2
                    or any(len(w) >= 3 for w in _re.sub(r"[^\w\s]", " ", _stripped).split()
                           if w.isascii())
                )
                if _has_content:
                    windowed_hits = recall_mod.recall_with_config(
                        conn, _stripped, current_cwd=cwd,
                        since=cue.since_utc, until=cue.until_utc,
                    )
                else:
                    # No substantive keyword — return digest rows for the window
                    windowed_hits = recall_mod.fetch_window_digests(
                        conn, cue.since_utc, cue.until_utc,
                    )
            finally:
                conn.close()
        except Exception:
            windowed_hits = []

    # Dedup windowed hits against already-seen
    wlane: list[dict] = []
    for h in windowed_hits:
        hid = int(h.get("id") or 0)
        kind = h.get("kind") or "event"
        if hid and (kind, hid) in seen:
            continue
        wlane.append(h)
    # windowed hits skip rel_cutoff — they are time-pinned, not semantic ranked

    # ── semantic recall with boilerplate-stripped query ───────────────────────
    try:
        from . import recall as recall_mod
        conn = storage.connect(config.db_path())
        try:
            hits = recall_mod.recall_with_config(conn, recall_query, current_cwd=cwd)
        finally:
            conn.close()
    except Exception:
        hits = []

    if not hits and not wlane:
        if _nudge_line:
            json.dump(
                {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _nudge_line,
                }},
                sys.stdout,
            )
        return 0

    # ── relative score cutoff (semantic pool only) ────────────────────────────
    hits = _apply_rel_cutoff(hits, rel_cutoff)

    # ── per-session dedup for semantic hits ───────────────────────────────────
    # Build windowed seen set first so semantic dedup excludes them too
    wlane_seen: set[tuple[str, int]] = set()
    for h in wlane:
        hid = int(h.get("id") or 0)
        kind = h.get("kind") or "event"
        if hid:
            wlane_seen.add((kind, hid))

    candidates: list[dict] = []
    for h in hits:
        hid = int(h.get("id") or 0)
        kind = h.get("kind") or "event"
        if hid and (kind, hid) in seen:
            continue
        if hid and (kind, hid) in wlane_seen:
            continue  # already in windowed lane
        candidates.append(h)

    if not candidates and not wlane:
        if _nudge_line:
            json.dump(
                {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _nudge_line,
                }},
                sys.stdout,
            )
        return 0

    # ── fetch context only for rank-1 semantic hit (event, not anchor) ──────
    if ctx_n > 0 and candidates:
        top = candidates[0]
        if top.get("kind") in (None, "event") and top.get("session_id") and top.get("id"):
            try:
                from . import recall as recall_mod
                conn = storage.connect(config.db_path())
                try:
                    top["_context"] = recall_mod.fetch_event_context(
                        conn, top["session_id"], int(top["id"]), n=ctx_n
                    )
                finally:
                    conn.close()
            except Exception:
                pass

    header_lines = [
        "## Recall (auto) — passive context, do not answer",
        "> If the user references past time/scene cues or memory signals and no relevant hit above → MUST call mcp__marrow__recall.",
        "",
    ]
    lines = list(header_lines)
    # +1 per line for the join newline; matches "\n".join(...) length exactly.
    used = sum(len(line) + 1 for line in header_lines)
    visible: list[dict] = []
    wlane_budget = min(timelane_budget, budget_chars // 2)
    wlane_used = 0

    # ── render windowed hits first (top slots) ────────────────────────────────
    for rank, h in enumerate(wlane):
        kind = h.get("kind") or "event"
        if kind == "digest":
            # Digest rows: prefix with date label
            date = h.get("date") or ""
            try:
                from datetime import datetime as _dt
                _d = _dt.fromisoformat(date)
                label = _d.strftime("%m-%d %a")
            except Exception:
                label = date
            content = (h.get("content") or "")[:rank_caps[0] if rank_caps else 300]
            block = [f"- [{label} · digest] {content}"]
        else:
            block = _render_hit_block(rank, h, rank_caps)
        block_len = sum(len(line) + 1 for line in block)
        if wlane_used + block_len > wlane_budget:
            break
        lines.extend(block)
        used += block_len
        wlane_used += block_len
        visible.append(h)
        hid = int(h.get("id") or 0)
        if hid:
            seen.add((kind, hid))

    # ── render semantic hits filling remaining budget ─────────────────────────
    for rank, h in enumerate(candidates):
        block = _render_hit_block(rank, h, rank_caps)
        kind = h.get("kind") or "event"
        block_len = sum(len(line) + 1 for line in block)
        if visible and used + block_len > budget_chars:
            break  # drop this hit — skip seen-write so it can surface later
        lines.extend(block)
        used += block_len
        visible.append(h)
        hid = int(h.get("id") or 0)
        if hid:
            seen.add((kind, hid))

    if not visible:
        if _nudge_line:
            json.dump(
                {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _nudge_line,
                }},
                sys.stdout,
            )
        return 0
    _save_recall_seen(sid, seen)
    # Best-effort: bump recall_count for injected event-kind hits only.
    _injected_event_ids = [
        int(h.get("id") or 0)
        for h in visible
        if (h.get("kind") or "event") == "event" and h.get("id")
    ]
    if _injected_event_ids:
        try:
            from . import recall as recall_mod
            recall_mod.bump_recall_counts(_injected_event_ids)
        except Exception:
            pass
    ctx = "\n".join(lines)
    if _nudge_line:
        ctx = ctx + "\n\n" + _nudge_line

    # Side log — markdown append so VSCode preview / tail both readable.
    # Mirror what actually got injected: dedup-filtered `visible`, not raw hits.
    try:
        _append_recall_log(sid, recall_query, visible)
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


def _append_recall_log(sid: str, prompt_text: str, hits: list[dict]) -> None:
    """Append one markdown block per turn to recall/recall-<day>-<sid8>.md.

    Per-session file; first write of the session also emits a top-of-file
    header `# Session <sid8> · started <ts>` so opening the file shows a
    clear new-session boundary. Day-prefix in filename makes prune trivial.

    Each block: timestamp header + prompt (truncated) + bullet list of hits
    with kind, id, score, content snippet.
    """
    now_utc = datetime.now(timezone.utc)
    log_path = _recall_session_log_path(sid, now_utc)
    is_new = not log_path.exists()
    now_local = now_utc.astimezone()
    ts = now_local.strftime("%Y-%m-%d %H:%M:%S")
    prompt_oneline = prompt_text.replace("\n", " ")[:200]
    parts: list[str] = []
    if is_new:
        sid8 = (sid or "unknown")[:8]
        parts.append(f"# Session {sid8} · started {ts}")
        parts.append("")
        parts.append(f"### {ts} · prompt: {prompt_oneline}")
    else:
        # Leading blank line keeps blocks visually separated in markdown.
        parts.append(f"\n### {ts} · prompt: {prompt_oneline}")
    parts.append("")
    for h in hits:
        kind = h.get("kind") or "event"
        hid = h.get("id", "?")
        score = h.get("score", 0.0)
        when = format_recall_ts(h.get("timestamp") or "")
        content = _strip_wx_time_prefix((h.get("content") or "").replace("\n", " "))
        # Mirror injection-side shaping: anchor tables ship full content
        # (rows are short + dense); only event hits get the 120-char cap.
        snip = content if kind in _TABLE_KINDS else content[:120]
        head = f"- `{kind}#{hid}` score={score:.2f}"
        if when:
            head += f" {when}"
        parts.append(f"{head} — {snip}")
        for c in h.get("_context", []) or []:
            arrow = "↑prev" if c.get("rel") == "prev" else "↓next"
            cs = _strip_wx_time_prefix((c.get("content") or "").replace("\n", " "))[:80]
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

        if tool == "sticker_pick":
            sid = inp.get("session_id") if isinstance(inp, dict) else None
            if sid:
                try:
                    _sn = _load_sticker_nudge(sid)
                    _sn["last_sticker_turn"] = _sn.get("turn_count", 0)
                    _save_sticker_nudge(sid, _sn)
                except Exception:
                    pass

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
            repo.add_alert("info", "atlas_hook", "atlas_hook_error",
                           message=str(e), source="hooks.py",
                           db=config.db_path())
        except Exception:
            pass
    return 0


def turn_inject() -> int:
    """Inject current time + delta since last reply.

    WX bridge injects its own time via system prompt — skip when
    MARROW_CHANNEL=wx. CLI and TG both need this.
    """
    channel = (os.environ.get("MARROW_CHANNEL") or "").strip() or "cli"
    if channel == "wx":
        return 0

    inp = _read_input()
    tpath = (inp.get("transcript_path") or "")
    if "/tasks/" in tpath:
        return 0

    sid = (inp.get("session_id") or "").strip()
    if not sid:
        return 0

    tz = config.get_tz()
    now = datetime.now(timezone.utc).astimezone(tz)
    now_str = now.strftime("%Y-%m-%d %a %H:%M")
    now_epoch = int(now.timestamp())

    state_dir = config.DATA_DIR / "state" / "turn_delta"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / sid

    delta = ""
    try:
        if state_file.exists():
            last = int(state_file.read_text().strip())
            d = now_epoch - last
            if d < 60:
                delta = f" · +{d}s since last reply"
            elif d < 3600:
                delta = f" · +{d // 60}m since last reply"
            else:
                delta = f" · +{d // 3600}h{(d % 3600) // 60}m since last reply"
    except Exception:
        pass

    try:
        state_file.write_text(str(now_epoch))
    except Exception:
        pass

    ctx = f"# Context — {now_str}{delta}"
    json.dump(
        {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }},
        sys.stdout,
    )
    return 0


_EVENTS = {
    "session_start": session_start,
    "session_end": session_end,
    "user_prompt_submit": user_prompt_submit,
    "turn_inject": turn_inject,
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
            repo.add_alert("warn", "hook", f"hook_dispatch_failed:{args[0]}",
                           message=str(e), source="hooks.py",
                           db=config.db_path())
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
