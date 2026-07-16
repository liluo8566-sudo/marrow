"""Thin CC hook entrypoints. `python -m marrow.hooks <event>`.

Code-only, no LLM. Parallel-safe with the legacy ny-memm hooks —
marrow registers ALONGSIDE them, never replaces. Logic lives in the marrow
package; this only does hook I/O (stdin JSON in, stdout JSON for
SessionStart additionalContext, side effects for SessionEnd).

  session_start      -> inject open tasks + alerts + affect backdrop; clear skip on resume
  session_end        -> clean transcript, archive events, regen dashboard top
  user_prompt_submit -> mm controls + recall fallback

PreToolUse is the global prompt-guard.py (scope already covers
~/CC-Lab/marrow/), not duplicated here.

mm- prefix: writes audit_log manual_skip row; sessionend_async skips LLM pipeline.
mm+ prefix: clears manual skip and flags the sid for sessionend.
resume detection: session_start fires on cc resume with same sid; if skip row exists,
  write skip_cleared row so sessionend_async runs normally.
"""
from __future__ import annotations

import hashlib
import json
import os
import re as _re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import config, cortex_bridge, outbox, repo, storage, top_sections, transcript
from .popen_detach import popen_detach, popen_detach_lazy
from .timeutil import (
    utc_iso_to_local_date,
    utc_iso_to_local_datetime,
    format_recall_ts,
    reltime_short,
)

_RECALL_TZ = config.get_tz()
_RECALL_CUTOFF_H = 6  # 6AM local day boundary (matches digest)

_SESSION_CLAIMS_PATH = Path("~/.config/marrow/session_claims.json").expanduser()


def _claim_session_lock(sid: str, channel: str) -> None:
    """Write cross-channel session claim so bridges detect handoff."""
    import json as _json, tempfile as _tf
    p = _SESSION_CLAIMS_PATH
    try:
        data = _json.loads(p.read_text("utf-8")) if p.exists() else {}
    except Exception:
        data = {}
    data[sid] = channel
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _tf.mkstemp(dir=str(p.parent), prefix=".slock.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(data, f)
        os.replace(tmp, str(p))
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    try:
        conn = storage.connect()
        with conn:
            conn.execute("UPDATE sessions SET channel=? WHERE sid=?", (channel, sid))
    except Exception:
        pass


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


# ── per-turn ingest cursor (Stop hook) ───────────────────────────────────────
# Mirrors the recall_seen storage pattern: one small json per sid holding the
# last-ingested tail uuid + byte offset, so a long session tail-reads instead
# of re-parsing the whole transcript each turn.

def _ct_cursor_path(sid: str) -> Path:
    return config.DATA_DIR / "state" / "ct_cursor" / f"{sid}.json"


def _load_ct_cursor(sid: str) -> dict | None:
    if not sid:
        return None
    try:
        d = json.loads(_ct_cursor_path(sid).read_text())
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _save_ct_cursor(sid: str, last_uuid: str | None, offset: int) -> None:
    if not sid:
        return
    p = _ct_cursor_path(sid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last_uuid": last_uuid, "offset": offset}))
    except Exception:
        pass


# ── cross-session replay cursor (turn_inject) ────────────────────────────────
# One small file per sid holding the last-seen events.id, same dir/pattern as
# recall_seen. Absent = first sight of this sid → seed to MAX(id), future-only.

def _replay_cursor_path(sid: str) -> Path:
    return config.DATA_DIR / "state" / "replay" / f"{sid}"


def _load_replay_cursor(sid: str) -> int | None:
    if not sid:
        return None
    try:
        return int(_replay_cursor_path(sid).read_text().strip())
    except Exception:
        return None


def _save_replay_cursor(sid: str, last_id: int) -> None:
    if not sid:
        return
    p = _replay_cursor_path(sid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(int(last_id)))
    except Exception:
        pass


def _replay_local_hm(ts: str, tz) -> str:
    try:
        dt = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%H:%M")
    except Exception:
        return "??:??"


def _replay_truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def _replay_context(sid: str, channel: str) -> str:
    """Cross-session replay inject. Returns '' when disabled, when this session's
    channel is an excluded target, on first sight of the sid (seed only), or when
    no new events from OTHER sessions exist. Cursor advances after a render
    decision (ambient — advances even when overflow turns are folded)."""
    if not sid:
        return ""
    cfg = (config.load().get("replay", {}) or {})
    if not cfg.get("enabled", True):
        return ""
    if channel in (cfg.get("exclude_target_channels", ["ct"]) or []):
        return ""

    max_turns = int(cfg.get("max_turns", 2))
    per_chars = int(cfg.get("per_msg_chars", 150))
    header = cfg.get("header", "## Recent replay from other sessions")

    conn = storage.connect(config.db_path())
    try:
        cursor = _load_replay_cursor(sid)
        if cursor is None:
            row = conn.execute("SELECT MAX(id) AS m FROM events").fetchone()
            seed = int(row["m"]) if row and row["m"] is not None else 0
            _save_replay_cursor(sid, seed)
            return ""  # first sight — future-only, never backfill

        rows = conn.execute(
            "SELECT id, session_id, role, content, timestamp, channel FROM events "
            "WHERE id > ? AND session_id != ? AND role IN ('user','assistant') "
            "AND COALESCE(channel,'') != 'ct' ORDER BY id ASC",
            (cursor, sid),
        ).fetchall()
    except sqlite3.OperationalError:
        return ""
    finally:
        conn.close()

    if not rows:
        return ""

    max_id = rows[-1]["id"]
    tz = config.get_tz()

    # Group into turns: a user event opens a turn; consecutive user msgs stay in
    # the same turn; the following assistant msgs close it. Next user reopens.
    turns: list[list[dict]] = []
    for r in rows:
        content = transcript.strip_media_markers(r["content"])
        if not content:
            continue
        item = {
            "channel": r["channel"] or "?",
            "sid4": (r["session_id"] or "")[:4],
            "hm": _replay_local_hm(r["timestamp"], tz),
            "role": "N" if r["role"] == "user" else "Y",
            "content": _replay_truncate(content, per_chars),
        }
        if r["role"] == "user" and (not turns or turns[-1][-1]["role"] != "N"):
            turns.append([item])
        elif not turns:
            # assistant with no opening user turn — start a bare turn
            turns.append([item])
        else:
            turns[-1].append(item)

    # Advance cursor on a render decision regardless of fold (ambient replay).
    _save_replay_cursor(sid, max_id)

    if not turns:
        return ""

    kept = turns[:max_turns]
    folded = len(turns) - len(kept)
    lines = [header]
    for turn in kept:
        for it in turn:
            lines.append(
                f"[{it['channel']}·{it['sid4']} {it['hm']}] {it['role']}: {it['content']}"
            )
    if folded > 0:
        lines.append(f"+{folded} more turns")
    return "\n".join(lines)


def _ensure_ct_activity(conn: sqlite3.Connection) -> None:
    """Create ct_activity if absent. Cortex C1 collector reads (ts, sid, channel)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ct_activity ("
        " id INTEGER PRIMARY KEY,"
        " ts TEXT NOT NULL,"
        " sid TEXT,"
        " channel TEXT)"
    )


def _write_ct_activity(conn: sqlite3.Connection, sid: str, channel: str) -> None:
    _ensure_ct_activity(conn)
    with conn:
        conn.execute(
            "INSERT INTO ct_activity (ts, sid, channel) VALUES (?, ?, ?)",
            (_now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), sid, channel),
        )


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
_FORCE_SESSIONEND_ACTION = "force_sessionend"
_STATUS_MM_PLUS_FLAG = "mm_plus_flag"
_STATUS_MM_IMMEDIATE = "mm_immediate"
_STATUS_MM_IMMEDIATE_CURRENT = "mm_immediate_current"


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


def _write_force_sessionend_flag(conn: sqlite3.Connection, sid: str, status: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, ?, ?)",
            (sid, _FORCE_SESSIONEND_ACTION, status),
        )


def _has_force_sessionend(conn: sqlite3.Connection, sid: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM audit_log"
        " WHERE action=? AND target_id=?"
        " AND id > COALESCE("
        "   (SELECT MAX(id) FROM audit_log"
        "    WHERE action='sessionend_extract' AND target_id=?"
        "    AND (summary='ok' OR summary LIKE 'ok,user_count=%')), 0)"
        " LIMIT 1",
        (_FORCE_SESSIONEND_ACTION, sid, sid),
    ).fetchone()
    return row is not None


def _is_session_blocked(conn: sqlite3.Connection, sid: str) -> bool:
    """Latest session_block row wins. archive -> True, cleared/absent -> False.
    mm+ does not write this flag; archive blocks remain explicit."""
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
        popen_detach(
            [sys.executable, "-m", "marrow.title", "--sid", sid],
            log_path=Path(os.devnull),
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


# ── git housekeep ────────────────────────────────────────────────────────────

# Files whose deletion must never be auto-committed away silently (Part A only).
_HOUSEKEEP_PROTECTED_DEFAULT = [
    "CLAUDE.md", "settings.json", "keybindings.json", "statusline.py",
    "output-styles/ny.md",
]


def _housekeep_protected_files() -> list[str]:
    try:
        return config.load().get("hooks", {}).get(
            "housekeep_protected_files", _HOUSEKEEP_PROTECTED_DEFAULT
        )
    except Exception:
        return _HOUSEKEEP_PROTECTED_DEFAULT


def _categorize_porcelain(lines: list[str]) -> dict[str, list[str]]:
    """Bucket `git status --porcelain` lines into deleted/renamed/added/modified.

    XY status codes: 'R'/'C' = rename/copy (own bucket, path keeps the
    'old -> new' arrow); 'D' = deleted; '?' or 'A' = added; everything else
    (M, T, U, ...) = modified.
    """
    cats: dict[str, list[str]] = {
        "deleted": [], "renamed": [], "added": [], "modified": [],
    }
    for line in lines:
        if not line.strip():
            continue
        xy, path = line[:2], line[3:].strip()
        if "R" in xy or "C" in xy:
            cats["renamed"].append(path)
        elif "D" in xy:
            cats["deleted"].append(path)
        elif "?" in xy or "A" in xy:
            cats["added"].append(path)
        else:
            cats["modified"].append(path)
    return cats


def _build_housekeep_commit_msg(cats: dict[str, list[str]], total: int) -> str:
    """Subject line stays `auto: session-start housekeep (N files)`; body
    lists files by category, deleted first and never truncated. Body caps
    at ~2000 chars overall (added/modified may truncate, deleted never does).
    """
    subject = f"auto: session-start housekeep ({total} files)"
    body_lines: list[str] = []
    running = 0
    if cats["deleted"]:
        line = "deleted: " + ", ".join(cats["deleted"])
        body_lines.append(line)
        running += len(line)
    for key in ("renamed", "added", "modified"):
        items = cats[key]
        if not items:
            continue
        line = f"{key}: " + ", ".join(items)
        if running + len(line) > 2000:
            remaining = 2000 - running
            if remaining <= len(key) + 2:
                continue
            line = line[:remaining - 1] + "…"
        body_lines.append(line)
        running += len(line)
    if not body_lines:
        return subject
    return subject + "\n\n" + "\n".join(body_lines)


def _build_housekeep_report_line(label: str, cats: dict[str, list[str]], total: int) -> str:
    line = f"{label}: committed {total} files"
    deleted = cats["deleted"]
    if not deleted:
        return line
    joined = ", ".join(deleted)
    if len(joined) > 120:
        joined = joined[:117] + "…"
        return f"{line} ⚠️ {len(deleted)} deleted: {joined}"
    return f"{line} ⚠️ deleted: {joined}"


def _git_housekeep_block(
    cwd: str | None, current_sid: str | None, conn: sqlite3.Connection
) -> str | None:
    """Auto-commit leftover diffs from prior sessions at session start.

    Three parts joined with ' · '. Returns None if nothing to report.
    Entire function is fail-soft — never blocks session_start.
    """
    try:
        lines: list[str] = []

        # Part A: ~/.claude auto-commit
        try:
            claude_dir = Path("~/.claude").expanduser()
            if (claude_dir / ".git").is_dir():
                r = subprocess.run(
                    ["git", "-C", str(claude_dir), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                dirty = [l for l in r.stdout.splitlines() if l.strip()]
                if dirty:
                    cats = _categorize_porcelain(dirty)
                    protected = _housekeep_protected_files()
                    blocked = [p for p in cats["deleted"] if p in protected]
                    if blocked:
                        repo.add_alert(
                            "warn", "git_housekeep_protected_delete",
                            f"claude:{','.join(sorted(blocked))}",
                            source="hooks.py",
                            message=(
                                "~/.claude session-start housekeep would delete "
                                f"protected file(s): {', '.join(blocked)} — "
                                "commit skipped, working tree left dirty"
                            ),
                            db=config.db_path(),
                        )
                        lines.append(
                            f"~/.claude: ⚠️ SKIPPED — protected file(s) "
                            f"deleted: {', '.join(blocked)} (resolve manually)"
                        )
                    else:
                        subprocess.run(
                            ["git", "-C", str(claude_dir), "add", "-A"],
                            capture_output=True, text=True, timeout=5, check=False,
                        )
                        subprocess.run(
                            ["git", "-C", str(claude_dir), "commit",
                             "-m", _build_housekeep_commit_msg(cats, len(dirty))],
                            capture_output=True, text=True, timeout=5, check=False,
                        )
                        lines.append(_build_housekeep_report_line("~/.claude", cats, len(dirty)))
        except Exception:
            pass

        # Part B: project cwd — commit submodules first, then top-level
        try:
            if cwd and Path(cwd).is_dir():
                # B1: recurse into nested git repos and commit dirty ones
                cwd_p = Path(cwd)
                nested = [d for d in cwd_p.iterdir()
                          if d.is_dir() and (d / ".git").exists()]
                for sm_abs_p in nested:
                    sm_path = sm_abs_p.name
                    sm_abs = str(sm_abs_p)
                    sr = subprocess.run(
                        ["git", "-C", sm_abs, "status", "--porcelain"],
                        capture_output=True, text=True, timeout=5, check=False,
                    )
                    sm_dirty = [l for l in sr.stdout.splitlines() if l.strip()]
                    if sm_dirty:
                        sm_cats = _categorize_porcelain(sm_dirty)
                        subprocess.run(
                            ["git", "-C", sm_abs, "add", "-A"],
                            capture_output=True, text=True, timeout=5, check=False,
                        )
                        subprocess.run(
                            ["git", "-C", sm_abs, "commit",
                             "-m", _build_housekeep_commit_msg(sm_cats, len(sm_dirty))],
                            capture_output=True, text=True, timeout=5, check=False,
                        )
                        lines.append(_build_housekeep_report_line(sm_path, sm_cats, len(sm_dirty)))

                # B2: top-level commit (picks up updated submodule pointers + own files)
                r = subprocess.run(
                    ["git", "-C", cwd, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                dirty = [l for l in r.stdout.splitlines() if l.strip()]
                if dirty:
                    cats = _categorize_porcelain(dirty)
                    subprocess.run(
                        ["git", "-C", cwd, "add", "-A"],
                        capture_output=True, text=True, timeout=5, check=False,
                    )
                    subprocess.run(
                        ["git", "-C", cwd, "commit",
                         "-m", _build_housekeep_commit_msg(cats, len(dirty))],
                        capture_output=True, text=True, timeout=5, check=False,
                    )
                    lines.append(_build_housekeep_report_line("cwd", cats, len(dirty)))
        except Exception:
            pass

        # Part C: stale worktree detection + cleanup
        try:
            if cwd and Path(cwd).is_dir():
                r = subprocess.run(
                    ["git", "-C", cwd, "worktree", "list", "--porcelain"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                wt_paths = [
                    l.split(" ", 1)[1].strip()
                    for l in r.stdout.splitlines()
                    if l.startswith("worktree ")
                ]
                secondary = wt_paths[1:]
                if secondary:
                    now = time.time()
                    stale, fresh = [], []
                    for p in secondary:
                        pp = Path(p)
                        if not pp.is_dir():
                            continue
                        age_h = (now - pp.stat().st_mtime) / 3600
                        name = pp.name
                        if age_h >= 24:
                            has_changes = bool(subprocess.run(
                                ["git", "-C", p, "status", "--porcelain"],
                                capture_output=True, text=True, timeout=5, check=False,
                            ).stdout.strip())
                            if has_changes:
                                stale.append(f"{name} ({age_h:.0f}h, has uncommitted changes)")
                            else:
                                branch = subprocess.run(
                                    ["git", "-C", p, "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True, timeout=5, check=False,
                                ).stdout.strip()
                                subprocess.run(
                                    ["git", "-C", cwd, "worktree", "remove", p],
                                    capture_output=True, text=True, timeout=10, check=False,
                                )
                                if branch and branch != "HEAD":
                                    subprocess.run(
                                        ["git", "-C", cwd, "branch", "-d", branch],
                                        capture_output=True, text=True, timeout=5, check=False,
                                    )
                                stale.append(f"{name} ({age_h:.0f}h, clean — removed)")
                        else:
                            fresh.append(name)
                    parts = []
                    if stale:
                        parts.append("stale wt: " + "; ".join(stale))
                    if fresh:
                        parts.append(f"{len(fresh)} active wt: " + ", ".join(fresh))
                    if parts:
                        lines.append(" · ".join(parts))
        except Exception:
            pass

        return " · ".join(lines) if lines else None
    except Exception:
        return None


# ── ~/.claude.json mcpServers snapshot ───────────────────────────────────────

_CLAUDE_JSON_SNAPSHOT_KEEP_DEFAULT = 10


def _claude_json_snapshot_keep() -> int:
    try:
        return int(config.load().get("hooks", {}).get(
            "claude_json_snapshot_keep", _CLAUDE_JSON_SNAPSHOT_KEEP_DEFAULT
        ))
    except Exception:
        return _CLAUDE_JSON_SNAPSHOT_KEEP_DEFAULT


def _claude_json_snapshot_block() -> str | None:
    """Fail-soft rolling backup of ~/.claude.json's mcpServers block. Never
    blocks session_start; on parse failure raises an alert instead of
    snapshotting the corrupt file.
    """
    try:
        src = Path.home() / ".claude.json"
        if not src.exists():
            return None

        try:
            raw = src.read_text()
        except Exception:
            return None

        try:
            data = json.loads(raw)
        except Exception:
            repo.add_alert(
                "warn", "claude_json_corrupt", "claude_json_corrupt",
                source="hooks.py",
                message="~/.claude.json failed to parse as JSON — snapshot skipped",
                db=config.db_path(),
            )
            return "claude.json: ⚠️ corrupt JSON, snapshot skipped"

        mcp_hash = hashlib.sha256(
            json.dumps(data.get("mcpServers", {}), sort_keys=True).encode()
        ).hexdigest()

        snap_dir = Path(config.DATA_DIR) / "backup" / "claude-json"
        snap_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(snap_dir.glob("claude-json-*.json"))

        if existing:
            newest = existing[-1]
            try:
                newest_data = json.loads(newest.read_text())
                newest_hash = hashlib.sha256(
                    json.dumps(newest_data.get("mcpServers", {}), sort_keys=True).encode()
                ).hexdigest()
            except Exception:
                newest_hash = None
            if newest_hash == mcp_hash:
                return None

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = snap_dir / f"claude-json-{stamp}.json"
        shutil.copy2(src, dest)

        keep = _claude_json_snapshot_keep()
        all_snaps = sorted(snap_dir.glob("claude-json-*.json"))
        if keep > 0:
            for stale in all_snaps[:-keep]:
                try:
                    stale.unlink()
                except Exception:
                    pass

        return "claude.json: snapshot saved (mcpServers changed)"
    except Exception:
        return None


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
        from datetime import date as _date
        catchup_dir = config.DATA_DIR / "logs" / "catchup"
        catchup_dir.mkdir(parents=True, exist_ok=True)
        log = catchup_dir / f"catchup.{_date.today():%Y-%m-%d}.log"
        for old in sorted(catchup_dir.glob("catchup.*.log"))[:-14]:
            old.unlink(missing_ok=True)
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
        is_resume = False
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
                    _claim_session_lock(sid, channel)
                except Exception:  # noqa: BLE001 — never block session_start
                    pass

        if is_worktree or is_subagent:
            # Task-isolated (git worktree / Task-tool subagent): no personal memory.
            ctx = ""
        else:
            parts: list[str] = []

            git_hk = _git_housekeep_block(cwd, sid, conn)
            if git_hk:
                parts.append(git_hk)

            cj_snap = _claude_json_snapshot_block()
            if cj_snap:
                parts.append(cj_snap)

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
            backdrop = _timeline_mod.render_timeline(conn, inject_cap=_timeline_mod._INJECT_CAP)
            if backdrop:
                parts.append(backdrop)

            # Usage block (all sessions self-aware) — off the collector kv.
            try:
                from . import usage as _usage
                ulines = _usage.sessionstart_lines()
                if ulines:
                    parts.append("\n".join(ulines))
            except Exception:
                pass

            # Cortex handoff: fresh window only (new process = fresh;
            # a resume skips). Content is no longer injected here — the user's
            # cortex CLAUDE.md `@handoff.md` imports it directly. Page-turn
            # (stale-date archive + fresh template) still runs as a side effect.
            if cortex_bridge.enabled() and os.environ.get("MARROW_CORTEX"):
                if not is_resume:
                    cortex_bridge._cortex_handoff_page_turn_if_stale()
                    # Arm the ear on a fresh cortex window: one-shot reminder to
                    # start the signal-log tail. Blank config text = no injection.
                    _arm = cortex_bridge.arm_ear_text()
                    if _arm:
                        parts.append(_arm)
                else:
                    # Resumed cortex window. Decide in code (not model judgement)
                    # whether it is still the active resident: compare the
                    # resumed transcript against wake_state.json's `transcript`
                    # pointer.
                    if cortex_bridge.is_resident_session(tpath):
                        # Resident resume: the prior process died with its ear
                        # tail; the harness will surface stale pre-resume task
                        # notifications. Kill orphan tails (none of ours armed
                        # yet — safe only here) and inject re-arm guidance so the
                        # model does not treat leftovers as a wake/rotate.
                        try:
                            cortex_bridge.kill_orphan_ear_tails()
                        except Exception:
                            pass
                        _resume = cortex_bridge.resume_ear_text()
                        if _resume:
                            parts.append(_resume)
                    else:
                        # Retired window: a newer cortex took over (or this was
                        # rotated out and reopened to read history). Arm nothing,
                        # touch no wake_state, never kill the live resident's
                        # tail — inject read-only/archive guidance instead.
                        _retired = cortex_bridge.retired_ear_text()
                        if _retired:
                            parts.append(_retired)

            try:
                from . import schedule as _sched
                if _sched.is_enabled():
                    sched_content, _ = _sched.refresh_daily()
                    if sched_content:
                        parts.append(sched_content)
            except Exception:
                pass

            ctx = "\n\n".join(p for p in parts if p)

            try:
                conn.execute(
                    "INSERT INTO audit_log (target_table, action, summary) VALUES (?, ?, ?)",
                    (
                        "sessions",
                        "session_start:zones",
                        f"git={len(git_hk or '')} hb={len(heartbeat or '')} alerts={len(alert_block)}"
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

    # Cortex window really closing (not /clear, which also fires SessionEnd
    # but leaves the window alive): end the wake now instead of waiting for
    # a 20-min fallback to discover the dead window. Best-effort, never
    # blocks the rest of session_end.
    if cortex_bridge.is_cortex_session():
        reason = inp.get("reason")
        if reason != "clear":
            try:
                cortex_bridge.cortex_window_closed(tpath)
            except Exception:  # noqa: BLE001 — never block session_end
                pass

    cwd = inp.get("cwd") or ""
    early_sid = (inp.get("session_id") or "").strip()
    db = config.db_path()
    conn = storage.connect(db)

    def _write_lifecycle_end(sid: str, summary: str) -> None:
        with conn:
            conn.execute(
                "INSERT INTO audit_log"
                " (target_table, target_id, action, summary)"
                " VALUES ('events', ?, 'session_lifecycle:end', ?)",
                (sid, summary),
            )
            conn.execute(
                "UPDATE sessions"
                " SET ended_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                " WHERE sid = ?",
                (sid,),
            )

    try:
        # Regen/rewind suppress: bridge wrote this flag before closing cc
        # so the intermediate SessionEnd skips archive entirely.
        if early_sid:
            _suppress = config.DATA_DIR / f".regen_suppress_{early_sid}"
            if _suppress.exists():
                try:
                    _suppress.unlink(missing_ok=True)
                except OSError:
                    pass
                return 0

        if os.environ.get("MARROW_PIPELINE") == "1":
            if early_sid:
                try:
                    _write_lifecycle_end(early_sid, "pipeline=1")
                except Exception:  # noqa: BLE001
                    pass
            return 0

        is_subagent = bool(tpath and "/tasks/" in tpath)
        if is_subagent:
            if early_sid:
                try:
                    _write_lifecycle_end(early_sid, "subagent=1")
                except Exception:  # noqa: BLE001 — never block session_end
                    pass
            return 0

        if transcript.is_headless(tpath):
            if early_sid:
                try:
                    _write_lifecycle_end(early_sid, "headless=1")
                except Exception:  # noqa: BLE001 — never block session_end
                    pass
            return 0

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
                    _write_lifecycle_end(early_sid, "worktree=1")
                except Exception:  # noqa: BLE001 — never block session_end
                    pass
            return 0

        # mm- block gate: if the user typed mm- at any point during this session,
        # _handle_mm_prefix wrote a session_block=archive flag. Skip the entire
        # archive path so events table receives ZERO rows for this sid. Still
        # write lifecycle:end so catchup doesn't flag this as silent_death.
        if early_sid and _is_session_blocked(conn, early_sid):
            try:
                _write_lifecycle_end(early_sid, "mm_minus_blocked")
            except Exception:  # noqa: BLE001 — never block session_end
                pass
            _wipe_recall_seen(early_sid)
            _wipe_sticker_nudge(early_sid)
            return 0

        is_bridge = os.environ.get("MARROW_BRIDGE") == "1"
        rows = transcript.clean(tpath, skip_headless_check=is_bridge, channel=os.environ.get("MARROW_CHANNEL") or "cli")
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
                _write_lifecycle_end(sid, "")
            except Exception:  # noqa: BLE001
                pass
            # Drop per-session recall dedup state — next window starts clean.
            _wipe_recall_seen(sid)
            _wipe_sticker_nudge(sid)

            # Bridge gate: when synapse-wx wraps cc, it owns sessionend timing
            # (fires on 6h idle, not on every /model swap). Archive runs, marker
            # written, popen suppressed. Catchup honors the bridge_owns marker
            # until a later fail row (manual fire that failed) supersedes it.
            # Exception: an explicit force flag requests extraction.
            if os.environ.get("MARROW_BRIDGE") == "1":
                if not _has_force_sessionend(conn, sid):
                    try:
                        _write_manual_skip_flag(
                            conn, sid, _STATUS_SKIP_BRIDGE_OWNS,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return 0

            # Idempotent gate: skip popen if events haven't grown since last ok.
            skip_spawn = False
            try:
                last_ok = _last_ok_user_count(conn, sid)
                if last_ok is not None and not _has_force_sessionend(conn, sid):
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
                cwd = inp.get("cwd") or ""
                try:
                    wm = storage.get_latest_watermark(conn, sid)
                    after_eid = wm["last_event_id"] if wm else None
                    _spawn_sessionend_async(
                        sid, after_event_id=after_eid, segment_seq=0, cwd=cwd,
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


# ── Stop hook: per-turn ingest ────────────────────────────────────────────────

def _tail_uuid(records: list[dict]) -> str | None:
    """Last record with a uuid, in file order (matches transcript tail semantics)."""
    t: str | None = None
    for r in records:
        if r.get("uuid"):
            t = r["uuid"]
    return t


def _tail_chain_connects(new_records: list[dict], last_uuid: str | None) -> bool:
    """True iff the newly-appended tail is a linear continuation of last_uuid.

    Walk parentUuid from the new tail; the chain root's parentUuid must equal
    last_uuid. A rewind/branch points the root elsewhere -> False (caller then
    does a full-file live-chain rebuild)."""
    if not last_uuid or not new_records:
        return False
    by_uuid = {r["uuid"]: r for r in new_records if r.get("uuid")}
    tail = _tail_uuid(new_records)
    if tail is None:
        return False
    cur: str | None = tail
    seen: set[str] = set()
    while cur in by_uuid and cur not in seen:
        seen.add(cur)
        cur = by_uuid[cur].get("parentUuid")
    return cur == last_uuid


def stop() -> int:
    """Per-turn ingest fired after each completed assistant turn.

    Archives the newly completed user+assistant pair (idempotent by
    source_hash) and logs a ct_activity row. Tail-reads from the per-sid cursor
    for cheap long-session appends; when the parentUuid walk can't reach the
    last-ingested uuid (rewind / bridge rewrite / stale offset) it falls back to
    a full-file live-chain rebuild via transcript.rows_from_records purely to
    locate + ingest the current pair and reset the cursor. Ghost rows ingested
    before a rewind stay in the DB (no retraction in v1)."""
    # Isolated pipeline spawns don't load hooks; mirror the guard defensively.
    if os.environ.get("MARROW_PIPELINE") == "1":
        return 0

    inp = _read_input()
    tpath = inp.get("transcript_path") if isinstance(inp, dict) else None
    sid = (inp.get("session_id") or "").strip() if isinstance(inp, dict) else ""
    cwd = inp.get("cwd") if isinstance(inp, dict) else None
    if not tpath or not sid:
        return 0

    # Task-isolated sessions (git worktree / Task-tool subagent) never enter
    # personal memory — mirror session_start / session_end.
    if "/tasks/" in tpath or _is_worktree_session(cwd or ""):
        return 0

    is_bridge = os.environ.get("MARROW_BRIDGE") == "1"
    channel = os.environ.get("MARROW_CHANNEL") or "cli"
    if not is_bridge and transcript.is_headless(tpath):
        return 0

    try:
        size = os.path.getsize(tpath)
    except OSError:
        return 0

    cursor = _load_ct_cursor(sid)
    rows: list[dict] = []
    new_last_uuid: str | None = None
    incremental = False

    if (cursor and isinstance(cursor.get("offset"), int)
            and 0 < cursor["offset"] <= size):
        tail_records: list[dict] = []
        try:
            # Binary seek: getsize is bytes; text-mode seek to an arbitrary
            # byte offset is unsafe once the file holds multibyte (CJK) content.
            with open(tpath, "rb") as f:
                f.seek(cursor["offset"])
                for raw in f.read().split(b"\n"):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        tail_records.append(json.loads(raw.decode("utf-8")))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
        except OSError:
            tail_records = []
        if _tail_chain_connects(tail_records, cursor.get("last_uuid")):
            incremental = True
            rows = transcript.rows_from_records(tail_records, channel=channel)
            new_last_uuid = _tail_uuid(tail_records) or cursor.get("last_uuid")

    if not incremental:
        records = transcript.parse_records(tpath)
        rows = transcript.rows_from_records(records, channel=channel)
        new_last_uuid = _tail_uuid(records)

    conn = storage.connect(config.db_path())
    try:
        if rows:
            repo.archive_events(conn, rows)
        _write_ct_activity(conn, sid, channel)
    finally:
        conn.close()
    _save_ct_cursor(sid, new_last_uuid, size)
    return 0


_SID_RE = _re.compile(
    r"^[0-9a-f]{8}(-[0-9a-f]{4}){0,3}(-[0-9a-f]{4,12})?$",
    _re.IGNORECASE,
)


def _looks_like_sid(arg: str) -> bool:
    """Return True if arg matches a full UUID or a short hex-prefix the user might type."""
    return bool(_SID_RE.match(arg.strip())) if arg and " " not in arg else False


_MM_ACK = {
    "mm-": "本窗口跳过DB",
    "mm+": "本窗口加入DB",
    "mm!": "补跑中",
    "mm!!": "补跑中",
}


def _inject_silent_ack(prefix: str) -> None:
    """Tell the LLM this prompt is a control signal."""
    ack = _MM_ACK.get(prefix, prefix)
    ctx = (
        f"## {prefix} control signal\n"
        f"Hook handled. Reply with exactly: {ack}"
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
    action = f"{prefix} <full-sid>"
    user_name = config.persona()["user_name"]
    ctx = (
        f"## {prefix} locate request\n"
        f"{user_name} sent `{prefix} <clue>`, but the clue is not a valid sid.\n"
        f"- clue: {clue}\n"
        "- Search events and audit_log for matching timestamp, content, or role.\n"
        f"- Once found, use `{action}`."
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
    projects_dir = pathlib.Path.home() / ".claude" / "projects"
    matches = list(projects_dir.glob(f"**/{sid}.jsonl"))
    if not matches:
        return None
    return str(max(matches, key=lambda p: p.stat().st_mtime))


def _pre_archive_jsonl(conn: sqlite3.Connection, tpath: str | None, channel: str = "cli") -> None:
    """Archive events from an active-session jsonl. Fail-soft — never raises."""
    if not tpath:
        return
    try:
        if transcript.is_headless(tpath):
            return
        rows = transcript.clean(tpath, channel=channel)
        if rows:
            repo.archive_events(conn, rows)
    except Exception:  # noqa: BLE001
        pass


def _spawn_sessionend_async(
    sid: str,
    *,
    after_event_id: int | None = None,
    segment_seq: int = 0,
    cwd: str = "",
) -> None:
    log = config.DATA_DIR / "logs" / f"sessionend_async_{sid}.log"
    cmd = [
        sys.executable, "-m", "marrow.sessionend_async",
        "--sid", sid, "--log-path", str(log),
    ]
    if cwd:
        cmd.extend(["--cwd", cwd])
    if after_event_id is not None:
        cmd.extend(["--after-event-id", str(after_event_id)])
    if segment_seq != 0:
        cmd.extend(["--segment-seq", str(segment_seq)])
    popen_detach_lazy(cmd, log_path=log)


def _spawn_sessionend_after_watermark(
    conn: sqlite3.Connection, sid: str, *, cwd: str = "",
) -> None:
    wm = storage.get_latest_watermark(conn, sid)
    after_eid = wm["last_event_id"] if wm else None
    _spawn_sessionend_async(sid, after_event_id=after_eid, segment_seq=0, cwd=cwd)


def _classify_skip_reason(conn: sqlite3.Connection, sid: str) -> str:
    """Return skip reason tag for a session without successful sessionend."""
    has_start = conn.execute(
        "SELECT 1 FROM audit_log"
        " WHERE action='session_lifecycle:start' AND target_id=?"
        " LIMIT 1",
        (sid,),
    ).fetchone()
    has_end = conn.execute(
        "SELECT 1 FROM audit_log"
        " WHERE action='session_lifecycle:end' AND target_id=?"
        " LIMIT 1",
        (sid,),
    ).fetchone()
    if has_start and not has_end:
        return "active"
    block = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='session_block' AND target_id=?"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if block and (block["summary"] or "") == "archive":
        return "mm-"
    skip = conn.execute(
        "SELECT 1 FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=?"
        " AND summary LIKE 'skip:short_session%'"
        " LIMIT 1",
        (sid,),
    ).fetchone()
    if skip:
        return "short"
    return "miss"


def _unrun_session_rows(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "WITH known AS ("
        " SELECT sid, title, channel, cwd, last_active FROM sessions"
        " UNION ALL"
        " SELECT target_id AS sid, '' AS title, '' AS channel, '' AS cwd,"
        "        MAX(occurred_at) AS last_active"
        " FROM audit_log"
        " WHERE action='session_lifecycle:start' AND target_id IS NOT NULL"
        " GROUP BY target_id"
        ")"
        " SELECT sid, MAX(title) AS title, MAX(channel) AS channel,"
        "        MAX(cwd) AS cwd, MAX(last_active) AS last_active"
        " FROM known"
        " WHERE sid IS NOT NULL AND sid != ''"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM audit_log a"
        "   WHERE a.action='sessionend_extract' AND a.target_id=known.sid"
        "   AND (a.summary='ok' OR a.summary LIKE 'ok,user_count=%')"
        " )"
        " GROUP BY sid"
        " ORDER BY MAX(last_active) DESC"
        " LIMIT ?",
        (limit,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["reason"] = _classify_skip_reason(conn, d["sid"])
        result.append(d)
    return result


def _inject_unrun_sessions(rows: list[dict]) -> None:
    if rows:
        lines = ["## mm! sessions without successful sessionend"]
        for i, r in enumerate(rows, 1):
            sid = r.get("sid") or ""
            title = (r.get("title") or "").strip() or "(untitled)"
            channel = (r.get("channel") or "-").strip() or "-"
            reason = r.get("reason") or "miss"
            last_active = (r.get("last_active") or "").strip()
            tail = f" {last_active}" if last_active else ""
            lines.append(f"{i}. [{channel}|{reason}] {title} {sid}{tail}")
        lines.append("Reasons: miss=漏跑 mm-=主动跳过 short=三轮以下 active=进行中")
        lines.append("Use `mm! <sid>` to run one immediately.")
    else:
        lines = ["## mm! sessions without successful sessionend", "No sessions found."]
    json.dump(
        {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(lines),
        }},
        sys.stdout,
    )


def _handle_mm_prefix(inp: dict) -> bool:
    """Handle mm control prefixes. Returns True if handled.

    mm-: writes manual_skip audit row for current (or named) sid.
    mm+: clears manual skip and flags current or named sid for sessionend.
    mm!: lists unrun sessions or immediately spawns a named sid.
    mm!!: pre-archives current jsonl and immediately spawns current sid.
    mm- / mm+ / mm!: three-branch on arg after prefix:
      - empty          → current sid (existing behaviour)
      - UUID-like      → named sid
      - natural-lang   → inject additionalContext to help LLM locate sid
    Fail-soft: any error is swallowed — hook must never block the user turn.
    """
    prompt = (inp.get("prompt") or "").strip().replace("！", "!")
    if not prompt.startswith(("mm-", "mm+", "mm!")):
        return False

    sid = (inp.get("session_id") or "").strip()
    if prompt.startswith("mm!!"):
        prefix = "mm!!"
        rest = prompt[4:].strip()
    else:
        prefix = prompt[:3]
        rest = prompt[3:].strip()

    if prefix == "mm!" and not rest:
        try:
            conn = storage.connect(config.db_path())
            try:
                _inject_unrun_sessions(_unrun_session_rows(conn))
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            _inject_unrun_sessions([])
        return True

    if prefix == "mm!!":
        if rest:
            _inject_locate_request(prefix, rest)
            return True
        try:
            if sid:
                conn = storage.connect(config.db_path())
                try:
                    _write_force_sessionend_flag(conn, sid, _STATUS_MM_IMMEDIATE_CURRENT)
                    tpath = inp.get("transcript_path") or _locate_jsonl(sid)
                    _pre_archive_jsonl(conn, tpath, channel=os.environ.get("MARROW_CHANNEL") or "cli")
                    row = conn.execute(
                        "SELECT 1 FROM audit_log"
                        " WHERE target_table='events'"
                        " AND target_id=?"
                        " AND action='session_lifecycle:end'"
                        " LIMIT 1",
                        (sid,),
                    ).fetchone()
                    if not row:
                        with conn:
                            conn.execute(
                                "INSERT INTO audit_log"
                                " (target_table, target_id, action, summary)"
                                " VALUES ('events', ?, 'session_lifecycle:end', 'mm_bang')",
                                (sid,),
                            )
                            conn.execute(
                                "UPDATE sessions"
                                " SET ended_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                                " WHERE sid = ? AND (ended_at IS NULL OR ended_at = '')",
                                (sid,),
                            )
                finally:
                    conn.close()
                conn = storage.connect(config.db_path())
                try:
                    _spawn_sessionend_after_watermark(conn, sid)
                finally:
                    conn.close()
        except Exception:  # noqa: BLE001
            pass
        _inject_silent_ack("mm!!")
        return True

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
            elif prefix == "mm+":
                if target_sid:
                    _write_manual_skip_flag(conn, target_sid, _STATUS_SKIP_CLEARED)
                    _write_force_sessionend_flag(conn, target_sid, _STATUS_MM_PLUS_FLAG)
            elif prefix == "mm!":
                if target_sid:
                    _write_force_sessionend_flag(conn, target_sid, _STATUS_MM_IMMEDIATE)
                    row = conn.execute(
                        "SELECT 1 FROM audit_log"
                        " WHERE target_table='events'"
                        " AND target_id=?"
                        " AND action='session_lifecycle:end'"
                        " LIMIT 1",
                        (target_sid,),
                    ).fetchone()
                    if not row:
                        with conn:
                            conn.execute(
                                "INSERT INTO audit_log"
                                " (target_table, target_id, action, summary)"
                                " VALUES ('events', ?, 'session_lifecycle:end', 'mm_bang')",
                                (target_sid,),
                            )
                            conn.execute(
                                "UPDATE sessions"
                                " SET ended_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                                " WHERE sid = ? AND (ended_at IS NULL OR ended_at = '')",
                                (target_sid,),
                            )
                    _spawn_sessionend_after_watermark(conn, target_sid)
                    conn.close()
                    conn = None
        finally:
            if conn is not None:
                conn.close()
    except Exception:  # noqa: BLE001 — never block prompt
        pass
    if prefix in {"mm-", "mm+", "mm!"}:
        _inject_silent_ack(prefix)
    return True


# ── pure recall-render helpers (extracted for testability) ───────────────────

def _apply_rel_cutoff(hits: list[dict], rel_cutoff: float) -> list[dict]:
    """Drop hits whose score < top_score * rel_cutoff. Returns filtered list."""
    if not hits:
        return []
    top_score = hits[0].get("score", 0.0)
    cutoff = top_score * rel_cutoff
    return [h for h in hits if (h.get("score") or 0.0) >= cutoff]


# Per-kind id prefix for recall heads (event -> ev, memes -> me, etc.).
_KIND_ABBREV = {
    "event": "ev", "memes": "me", "milestone": "ms",
    "entity": "en", "diary": "d", "task": "t",
}


def _meme_date(ts: str) -> str:
    """Meme creation date as 'MM-DD' (configured local timezone), or 'YYYY' if >1y old.
    Empty on missing/unparseable timestamp."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        local = dt.astimezone(config.get_tz())
        if (now - dt).total_seconds() >= 365 * 86400:
            return local.strftime("%Y")
        return local.strftime("%m-%d")
    except Exception:
        return ""


def _milestone_date(ts: str) -> str:
    """Milestone date with the T00:00 junk stripped. Keeps calendar precision:
    'YYYY' / 'YYYY-MM' / 'YYYY-MM-DD' (whatever the stored date carried).
    The date is a calendar value, not an instant — no tz conversion."""
    if not ts:
        return ""
    return ts.split("T", 1)[0]


def _recall_head(h: dict) -> str:
    """Shared recall-row head: '<time-label> <abbrev>#<id>' (content appended
    by the caller as ': <content>'). Kept identical across the injection
    renderer and the recall log so both read the same.
      event     -> [<channel> <reltime>] ev#<id>   (channel fallback: cli)
      memes     -> [<MM-DD|YYYY>] me#<id>
      milestone -> [<YYYY[-MM[-DD]]>] ms#<id>       (never T00:00)
      entity    -> en#<id>                          (no time)
      diary     -> [<format_recall_ts>] d#<id>      (existing time handling)
      task      -> [<format_recall_ts>] t#<id>
    """
    kind = h.get("kind") or "event"
    ref = f"{_KIND_ABBREV.get(kind, kind)}#{h.get('id', '?')}"
    if kind == "event":
        ch = (h.get("channel") or "cli").strip() or "cli"
        rt = reltime_short(h.get("timestamp") or "")
        return f"[{ch} {rt}] {ref}" if rt else f"[{ch}] {ref}"
    if kind == "memes":
        d = _meme_date(h.get("timestamp") or "")
        return f"[{d}] {ref}" if d else ref
    if kind == "milestone":
        d = _milestone_date(h.get("timestamp") or "")
        return f"[{d}] {ref}" if d else ref
    if kind == "entity":
        return ref
    # diary / task — keep existing format_recall_ts handling.
    ts = format_recall_ts(h.get("timestamp") or "")
    return f"{ts} {ref}" if ts else ref


def _render_hit_block(rank: int, h: dict, rank_caps: list[int]) -> list[str]:
    """Return the markdown lines for one recall hit at the given rank.

    rank_caps[rank] (falling back to rank_caps[-1]) controls max content chars.
    Context turns (h['_context']) are only rendered for rank-0 event hits.
    Pure function — no I/O, no DB access.
    """
    cap = rank_caps[rank] if rank < len(rank_caps) else rank_caps[-1]
    block: list[str] = []
    head = _recall_head(h)
    kind = h.get("kind") or "event"
    content_full = (h.get("content") or "").replace("\n", " ")
    if kind in _TABLE_KINDS:
        block.append(f"- {head}: {content_full[:cap]}")
    else:
        ctxs = h.get("_context") or [] if rank == 0 else []
        main_cap = max(40, cap - 60) if ctxs else cap
        main = content_full[:main_cap]
        block.append(f"- {head}: {main}")
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

    Also handles mm controls before recall.
    Config flag: [recall] vector = true (default on). Set false to disable.
    Fusion weights come from [recall] in config; recall.recall_fusion blends
    vec + bm25 + recency + affect. Fail-soft: any error falls through to a
    no-op so the user prompt always reaches the model.
    """
    inp = _read_input()

    # mm control plane — check before recall, independent of recall config.
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

    # Cortex wake-turn / monitor-death injections (cortex window only). The ear
    # Monitor tails the wake-signal log and surfaces a marker line as a user turn
    # (wake), or the harness surfaces a "Monitor stopped" task-notification when
    # the ear dies. Both are handled here and stop before recall; ordinary chat
    # turns fall through untouched. Text + paths are config-routed.
    if cortex_bridge.is_cortex_session():
        _prompt = (inp.get("prompt") or "").strip() if isinstance(inp, dict) else ""
        # Monitor death → rearm the ear. Checked first: the death notification is
        # a distinct harness shape, never a wake marker.
        if cortex_bridge.is_monitor_death(_prompt):
            _re_txt = cortex_bridge.rearm_text()
            if _re_txt:
                json.dump({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _re_txt,
                }}, sys.stdout)
            return 0
        # Free-round tuck-in ([NEW ROUND]) already carries its diff-mode note
        # INLINE (cortex watchdog D6) — the hook must NOT also turn-inject the
        # full note, or the round lands with a duplicate note (07-14 incident).
        # A tuck-in is a machine line but never a wake BELL, so this guard is
        # checked before the wake-marker branch. The tuck-in falls through to
        # the is_machine_line gate below (no user-wake reset either).
        _tuck = cortex_bridge.tuck_in_marker()
        if _tuck and cortex_bridge.line_starts_with_marker(_prompt, _tuck):
            return 0
        # Wake turn → inject the full wakeup note. Marker match only; missing or
        # empty note injects nothing (never crashes). The line may carry a
        # cancellation-epoch token ' {g<gen>:<sid>}': a STALE token (a newer
        # epoch — e.g. a user message — already superseded this alarm) suppresses
        # the wake-note injection (log only, do NOT process as a wake). A
        # token-less line is legacy and processed as before.
        # Line-start shape check (NOT substring): a real user prompt merely
        # quoting the marker mid-sentence ("grep for [CORTEX-WAKE]") must fall
        # through to the user-wake reset + recall, not be swallowed here. The
        # real wake bell always line-starts the marker; the epoch token
        # ' {g<gen>:<sid>}' is a suffix, so the startswith check tolerates it.
        _marker = cortex_bridge.wake_marker()
        if _marker and cortex_bridge.line_starts_with_marker(_prompt, _marker):
            _tok = cortex_bridge.parse_gen_token(_prompt)
            if _tok is not None and not cortex_bridge.wake_token_current(_tok):
                cortex_bridge._wake_audit(
                    "wake_line_stale", f"gen={_tok[0]}",
                    "suppressed (superseded epoch)")
                return 0
            _note = cortex_bridge.wakeup_note_text(tpath)
            # Merge any ct-targeted outbox notes into the wake payload — the
            # normal delivery path below never runs on a wake turn, so ct notes
            # must be consumed here (same atomic claim/consume ordering).
            try:
                _ob = outbox.deliver(
                    inp.get("session_id") if isinstance(inp, dict) else None,
                    "ct", is_cortex=True, db=config.db_path())
            except Exception:
                _ob = None
            _payload = "\n\n".join(p for p in (_note, _ob) if p)
            if _payload:
                json.dump({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _payload,
                }}, sys.stdout)
            return 0
        # Real user message (NOT a machine line down the ear channel) → user-wake
        # reset: flip awake, kill the pending alarm + sentinel, spawn a watchdog.
        # Best-effort; never blocks the prompt or the recall below.
        if not cortex_bridge.is_machine_line(_prompt):
            try:
                cortex_bridge._cortex_user_wake_reset(inp if isinstance(inp, dict) else {})
            except Exception:
                pass

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

    # Outbox delivery (cli/session notes): claim + render notes targeting this
    # session (exact sid, or 'cli' broadcast for cli sessions), consume-once.
    # ct-targeted notes are handled in the wake branch above. Seeds _nudge_line
    # so it lands on every emit path (renders above recall / other nudges).
    _nudge_line: str | None = None
    try:
        _msg_note = outbox.deliver(
            sid, os.environ.get("MARROW_CHANNEL") or "cli",
            db=config.db_path())
        if _msg_note:
            _nudge_line = _msg_note
    except Exception:
        pass

    # Sticker nudge: increment turn counter; flag nudge if 10 turns since last sticker.
    if sid and os.environ.get("MARROW_BRIDGE") == "1":
        try:
            _sn = _load_sticker_nudge(sid)
            _sn["turn_count"] = _sn.get("turn_count", 0) + 1
            if _sn["turn_count"] - _sn.get("last_sticker_turn", 0) >= 10:
                user_name = config.persona()["user_name"]
                _sticker_line = f"你怎么还不发表情包，{user_name}都等急了——翻翻 sticker(action=search) 找个应景的发一下。"
                _nudge_line = f"{_nudge_line}\n{_sticker_line}" if _nudge_line else _sticker_line
                _sn["last_sticker_turn"] = _sn["turn_count"]
            _save_sticker_nudge(sid, _sn)
        except Exception:
            pass

    # tl_add nudge: fire the 10-turn (config) reminder for sids that have gone
    # too long without recording a timeline line. Appends to any sticker nudge.
    if sid:
        try:
            from . import tl_nudge as _tln
            if _tln.enabled():
                conn = storage.connect(config.db_path())
                try:
                    _tl_hint = _tln.maybe_nudge(conn, sid)
                finally:
                    conn.close()
                if _tl_hint:
                    _nudge_line = f"{_nudge_line}\n{_tl_hint}" if _nudge_line else _tl_hint
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
        score = h.get("score", 0.0)
        content = _strip_wx_time_prefix((h.get("content") or "").replace("\n", " "))
        # Mirror injection-side shaping: anchor tables ship full content
        # (rows are short + dense); only event hits get the 120-char cap.
        snip = content if kind in _TABLE_KINDS else content[:120]
        # Same head as the injection renderer; score kept as a debug suffix.
        parts.append(f"- {_recall_head(h)}: {snip} · score={score:.2f}")
        for c in h.get("_context", []) or []:
            arrow = "↑prev" if c.get("rel") == "prev" else "↓next"
            cs = _strip_wx_time_prefix((c.get("content") or "").replace("\n", " "))[:80]
            parts.append(f"    - {arrow} ({c.get('role')}) {cs}")
    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")


_PLACEMENT_BASH_OPS = {"mv", "cp", "rename", "mmv", "touch", "mkdir"}


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


def _bg_is_whitelisted_path(p: str) -> bool:
    """Whitelist scope: /tmp, /private/tmp, or any path containing /scratchpad/
    or /.claude/worktrees/ — destructive ops there are silent."""
    pp = (p or "").strip().strip("'\"")
    if not pp:
        return False
    if pp in ("/tmp", "/private/tmp"):
        return True
    if pp.startswith("/tmp/") or pp.startswith("/private/tmp/"):
        return True
    if "/scratchpad/" in pp or "/.claude/worktrees/" in pp:
        return True
    return False


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
        return hooks_cfg.get("backup_guard_deny_message") or _BG_DENY_MSG
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
        return hooks_cfg.get("backup_guard_message") or _BG_REMIND_MSG
    except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
        return None


# ── git force-push guard (PreToolUse) — hard deny ─────────────────────────────
# Force push rewrites remote history: a hard deny, no escape hatch, no worktree
# exemption. Tokenized per shell segment so a commit -m "...--force..." message
# can never false-positive. Config: [hooks].git_force_push_guard (default true)
# + git_force_push_guard_message override.
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
    (default true) + git_force_push_guard_message. Fail-open: any error → None."""
    try:
        if not isinstance(inp, dict) or inp.get("tool_name") != "Bash":
            return None
        hooks_cfg = config.load().get("hooks", {}) or {}
        if not hooks_cfg.get("git_force_push_guard", True):
            return None
        cmd = (inp.get("tool_input") or {}).get("command", "") or ""
        if not isinstance(cmd, str) or not _git_force_push_matches(cmd):
            return None
        return hooks_cfg.get("git_force_push_guard_message") or _GIT_FORCE_PUSH_MSG
    except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
        return None


# ── git revert-type guard (PreToolUse) — held for authorship check ────────────
# Distinct from the backup guard: revert-type git ops discard work, so the
# risk is WHOSE work. Decision is "ask" (surface to the user), not a silent
# deny — the model must first verify the diff's authorship. Worktree/agent
# cleanup (branch -D teardown, worktree remove) is exempt.
_GIT_REVERT_DEFAULT_PATTERNS = [
    r"\bgit\s+reset\s+--hard\b",
    # git checkout [<flags>|<tree-ish>]* -- <path> — an optional revision
    # token (HEAD, a commit, a branch name — no leading dash) and/or flags
    # may precede `--`. `git checkout <branch>` with no `--` (plain branch
    # switch) is intentionally left unmatched.
    r"\bgit\s+checkout\s+(?:\S+\s+)*?--\s+\S",
    r"\bgit\s+restore\b",                            # worktree discard
    r"\bgit\s+clean\s+-\w*f",                        # -f / -fd
    r"\bgit\s+branch\s+-\w*D\w*\b",
    r"\bgit\s+stash\s+(?:drop|clear)\b",
    r"\bgit\s+revert\b[^\n]*--no-edit\b",
    r"\bgit\s+switch\b[^\n]*--discard-changes\b",
    r"\bgit\s+worktree\s+remove\b",
]

_GIT_REVERT_MSG = "🤡 狗男人又要乱搞你git里的东西了，是否同意？"


# Split on shell control operators (&&, ||, ;, |, &, newline) so pattern
# matching is evaluated PER SEGMENT — a safe segment (`git restore --staged
# a`) can never launder an unsafe one later in the same compound command
# (`git restore --staged a && git restore b`). Not a full shell parser
# (simple split — this guard is fail-open assist), mirrors the backup
# guard's `_BG_SHELL_SEP_RE` with newline added per review.
_GIT_REVERT_SEP_RE = _re.compile(r"&&|\|\||[;&|]|\n")


def _git_revert_segment_matches(seg: str, pats: list) -> bool:
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
        return True
    return False


def _git_revert_matches(cmd: str) -> bool:
    """True if any shell segment of *cmd* contains a git revert-type op per
    config patterns."""
    if not cmd:
        return False
    pats = (config.load().get("hooks", {}) or {}).get(
        "git_revert_patterns"
    ) or _GIT_REVERT_DEFAULT_PATTERNS
    for seg in _GIT_REVERT_SEP_RE.split(cmd):
        seg = seg.strip()
        if seg and _git_revert_segment_matches(seg, pats):
            return True
    return False


def _git_revert_guard(inp: dict) -> str | None:
    """Git revert-type authorship gate.

    Returns the 'ask' reason string to hold the call; "" when a git-revert op
    matched but it's worktree/agent cleanup (allow silently, skip the backup
    deny gate); None when it isn't a git-revert op at all (fall through).
    Config: [hooks].git_revert_guard (default true) + git_revert_patterns.
    Fail-open: any error returns None."""
    try:
        if not isinstance(inp, dict) or inp.get("tool_name") != "Bash":
            return None
        hooks_cfg = config.load().get("hooks", {}) or {}
        if not hooks_cfg.get("git_revert_guard", True):
            return None
        cmd = (inp.get("tool_input") or {}).get("command", "") or ""
        if not isinstance(cmd, str) or not _git_revert_matches(cmd):
            return None
        # Worktree/agent cleanup teardown stays allowed. Use the same
        # `/.claude/worktrees/` path-substring test as the backup guard's
        # whitelist; also honour a live worktree-session detection when the cwd
        # resolves.
        cwd = inp.get("cwd") or ""
        if (
            "/.claude/worktrees/" in cwd
            or ".claude/worktrees/" in cmd
            or _is_worktree_session(cwd)
        ):
            return ""
        return hooks_cfg.get("git_revert_guard_message") or _GIT_REVERT_MSG
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

        # Cortex lie_down handoff gate — deny a rotate/full-window lie_down until
        # the handoff is written this window. Plain lie_down passes.
        lie_down_deny: str | None = None
        try:
            if cortex_bridge.enabled():
                lie_down_deny = cortex_bridge._cortex_lie_down_deny(inp)
        except Exception:  # noqa: BLE001 — fail-open, never blocks the hook
            lie_down_deny = None
        if lie_down_deny:
            _emit_hso({
                "permissionDecision": "deny",
                "permissionDecisionReason": lie_down_deny,
            })
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

        _literal = "[Path] Use paths with /, not bare filenames."

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


def _in_time_window(now_min: int, start: str, end: str) -> bool:
    """Minute-of-day membership; wraps past midnight when end <= start."""
    def _to_min(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    s, e = _to_min(start), _to_min(end)
    if s <= e:
        return s <= now_min < e
    return now_min >= s or now_min < e


def _kickout_context(channel: str, now: datetime) -> str:
    """B8 anti-late-night deterministic nudge, config-first ([kickout] in
    config.toml — see config.default.toml for the live windows/text). Cortex
    is immune (MARROW_CORTEX=1, own bulletin/schedule, not this hook's rules)."""
    if cortex_bridge.is_cortex_session():
        return ""
    kc = config.load().get("kickout", {}) or {}
    if not kc.get("enabled", True):
        return ""
    now_min = now.hour * 60 + now.minute
    if channel == "cli":
        if _in_time_window(now_min, kc.get("cli_wind_down_start", "21:30"),
                            kc.get("cli_wind_down_end", "22:00")):
            return kc.get("cli_wind_down_text", "")
        if _in_time_window(now_min, kc.get("cli_leave_start", "22:00"),
                            kc.get("cli_leave_end", "06:00")):
            return kc.get("cli_leave_text", "")
    elif channel in ("wx", "tg"):
        if _in_time_window(now_min, kc.get("im_quiet_start", "23:00"),
                            kc.get("im_quiet_end", "06:00")):
            return kc.get("im_quiet_text", "")
    return ""


def _window_tokens_from_transcript(tpath: str) -> int:
    """Context-window occupancy = the last assistant message's usage totals
    (input + cache read + cache creation + output) in the session jsonl. Mirrors
    cortex.transcript.window_tokens. 0 on any missing/unreadable transcript."""
    if not tpath:
        return 0
    try:
        lines = open(tpath, encoding="utf-8").read().splitlines()
    except OSError:
        return 0
    total = 0
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        msg = o.get("message")
        u = msg.get("usage") if isinstance(msg, dict) else None
        if u:
            total = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                     + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0))
    return total


def _usage_threshold_context(sid: str, tpath: str) -> str:
    """In-window token threshold line (all sessions). `main` = window occupancy
    (last assistant turn's usage totals — same metric as statusline `total` and
    the rotate/fuse thresholds, NOT cumulative net-spend); `agent` = cumulative
    subagent_tokens. Fires once `main` crosses threshold_start, then
    again every threshold_step above the last-injected watermark. Watermark
    tracked per session under state/. Empty below the first threshold.

    Tier/watermark math uses `main_occ` alone — the threshold is a
    window-rotation signal, and agent tokens don't occupy the main window, so
    they must not drive triggering. `agent_net` appears only in the rendered
    line."""
    if not sid or not tpath:
        return ""
    try:
        from . import usage
        cu = config.load().get("cortex_usage", {}) or {}
        start = int(cu.get("threshold_start", 100_000) or 0)
        step = int(cu.get("threshold_step", 50_000) or 0)
        if start <= 0 or step <= 0:
            return ""
        main_occ = _window_tokens_from_transcript(tpath)
        agent_net = usage.agent_tokens_from_transcript(tpath)
        if main_occ < start:
            return ""
        # Current tier = highest crossed threshold (start + k*step).
        tier = start + ((main_occ - start) // step) * step
        state_dir = config.DATA_DIR / "state" / "usage_watermark"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / sid
        last = 0
        try:
            last = int(state_file.read_text().strip())
        except (OSError, ValueError):
            last = 0
        if tier <= last:
            return ""
        line = usage.threshold_line(main_occ, agent_net)
        if not line:
            return ""
        state_file.write_text(str(tier))
        return line
    except Exception:
        return ""


def turn_inject() -> int:
    """Inject current time + delta since last reply, plus the B8 kickout
    nudge (config [kickout]).

    WX bridge injects its own time via system prompt — skip the time+delta
    stamp when MARROW_CHANNEL=wx, but the kickout nudge still applies there.
    CLI and TG both need the time stamp.
    """
    channel = (os.environ.get("MARROW_CHANNEL") or "").strip() or "cli"

    inp = _read_input()
    tpath = (inp.get("transcript_path") or "")
    if "/tasks/" in tpath:
        return 0

    sid = (inp.get("session_id") or "").strip()
    if not sid:
        return 0

    tz = config.get_tz()
    now = datetime.now(timezone.utc).astimezone(tz)
    kickout_ctx = _kickout_context(channel, now)

    def _sched_fragment() -> str:
        try:
            from . import schedule as _sched
            inj = _sched.check_and_inject(sid)
            return f"\n\n{inj}" if inj else ""
        except Exception:
            return ""

    def _tl_fragment() -> str:
        try:
            from . import tl_sync as _tls
            conn = storage.connect(config.db_path())
            try:
                frag = _tls.render_update(conn, sid)
            finally:
                conn.close()
            return f"\n\n{frag}" if frag else ""
        except Exception:
            return ""

    if channel == "wx":
        # WX bridge injects its own time — skip the time stamp only; the
        # schedule + tl fragments and kickout nudge still apply.
        wx_sched = _sched_fragment()
        wx_tl = _tl_fragment()
        wx_kick = f"\n\n{kickout_ctx}" if kickout_ctx else ""
        wx_replay = _replay_context(sid, channel)
        wx_replay = f"\n\n{wx_replay}" if wx_replay else ""
        wx_ctx = f"{wx_sched}{wx_tl}{wx_kick}{wx_replay}".strip()
        if wx_ctx:
            json.dump(
                {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": wx_ctx,
                }},
                sys.stdout,
            )
        return 0

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

    sched_ctx = _sched_fragment()
    tl_ctx = _tl_fragment()

    # Absorbed global turn-inject: per-turn care directive (config-lives).
    care_ctx = ""
    try:
        care = (config.load().get("turn_inject", {}) or {}).get("care_text", "")
        care = (care or "").strip()
        if care:
            care_ctx = f"\n\n{care}"
    except Exception:
        pass

    kickout_full = f"\n\n{kickout_ctx}" if kickout_ctx else ""
    show_ctx = cortex_bridge._cortex_show_context(tpath) if cortex_bridge.enabled() else ""
    show_full = f"\n\n{show_ctx}" if show_ctx else ""
    usage_ctx = _usage_threshold_context(sid, tpath)
    usage_full = f"\n\n{usage_ctx}" if usage_ctx else ""
    replay_ctx = _replay_context(sid, channel)
    replay_full = f"\n\n{replay_ctx}" if replay_ctx else ""
    ctx = (f"# Context — {now_str}{delta}{sched_ctx}{tl_ctx}{care_ctx}"
           f"{kickout_full}{show_full}{usage_full}{replay_full}")
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


if __name__ == "__main__":
    raise SystemExit(main())
