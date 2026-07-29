"""Session lifecycle hooks: session_start, session_end, stop."""
from __future__ import annotations

import json
import os
import re as _re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from .. import config, cortex_bridge, replay, repo, storage, transcript
from ..popen_detach import popen_detach
from ._shared import _read_input
from .housekeep import _claude_json_snapshot_block, _git_housekeep_block
from .state import (
    _load_ct_cursor,
    _prune_recall_logs,
    _save_ct_cursor,
    _wipe_recall_seen,
    _wipe_sticker_nudge,
    _write_ct_activity,
)

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


def _drain_fallback_sink() -> None:
    """Replay any lines queued in alerts-fallback.jsonl into the alerts table.

    Truncates the file first so a replay that itself fails re-appends via the
    fallback sink, keeping the file bounded. Malformed lines are dropped with
    a stderr note. Fully fail-soft — never blocks session start."""
    try:
        db = config.db_path()
        sink = config.DATA_DIR / "alerts-fallback.jsonl"
        if not sink.exists() or sink.stat().st_size == 0:
            return
        try:
            raw = sink.read_text(encoding="utf-8")
            sink.write_text("", encoding="utf-8")  # truncate before replay
        except OSError as e:
            sys.stderr.write(f"[session_start] fallback drain read error: {e}\n")
            return
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                repo.add_alert(
                    rec["severity"], rec["type"], rec["fingerprint"],
                    source=rec.get("source"),
                    message=rec.get("message"),
                    db=db,
                )
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(
                    f"[session_start] fallback drain dropped malformed line: {e}\n"
                )
    except Exception:  # noqa: BLE001 — never block session_start
        pass


# ── lifecycle helpers ─────────────────────────────────────────────────────────


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
    of the started_at stamp on the lifecycle:start marker)."""
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


# ── session-start payload ────────────────────────────────────────────────────


def session_start() -> int:
    # Drain alerts-fallback sink: replay any alerts that were written to the
    # jsonl fallback (add_alert path when the DB was unwritable) into the
    # alerts table. Fail-soft — never blocks session start.
    _drain_fallback_sink()
    # Recall housekeeping — prune day-2+ logs from recall/ dir + wipe per-session
    # dedup state so every fresh window starts with a clean recall slate.
    _prune_recall_logs()
    inp = _read_input()
    db = config.db_path()
    conn = storage.connect(db)
    try:
        # Write lifecycle:start marker (resume detection + latest-session query).
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
                # Resume detection: a sid with a prior lifecycle:start row is a
                # cc resume (used below for the cortex handoff page-turn gate).
                is_resume = _has_prior_lifecycle_start(conn, sid)
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

            from .. import timeline as _timeline_mod
            backdrop = _timeline_mod.render_timeline(conn, inject_cap=_timeline_mod._INJECT_CAP)
            if backdrop:
                parts.append(backdrop)

            # Cross-session replay: same core call as turn_inject. No marker yet
            # -> the latest window renders here, so opening context is not empty.
            if sid:
                replay_seed = replay.context(
                    sid, os.environ.get("MARROW_CHANNEL") or "cli",
                    transcript_path=tpath)
                if replay_seed:
                    parts.append(replay_seed)

            # Usage block (all sessions self-aware) — off the collector kv.
            try:
                from .. import usage as _usage
                ulines = _usage.sessionstart_lines()
                if ulines:
                    parts.append("\n".join(ulines))
            except Exception:
                pass

            # Cortex handoff: fresh window only (new process = fresh;
            # a resume skips). Nothing is injected here — the user's cortex
            # CLAUDE.md `@handoff.md` imports the content directly, and wake
            # delivery is typed straight into the window by the cortex daemon.
            # Page-turn (stale-date archive + fresh template) is the only effect.
            if (cortex_bridge.enabled() and cortex_bridge._shell_enabled()
                    and not is_resume):
                cortex_bridge._cortex_handoff_page_turn_if_stale()

            try:
                from .. import schedule as _sched
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
                        f"git={len(git_hk or '')} alerts={len(alert_block)}"
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
        # Terminal lifecycle:end marker only. Events are archived per-turn by
        # the Stop hook; SessionEnd no longer cleans/archives the transcript or
        # spawns any LLM extraction. The marker is still written for every
        # session type so timeline._query_current_sid can tell live from ended
        # windows.
        if os.environ.get("MARROW_PIPELINE") == "1":
            summary = "pipeline=1"
        elif tpath and "/tasks/" in tpath:
            summary = "subagent=1"
        elif transcript.is_headless(tpath):
            summary = "headless=1"
        elif (
            _was_worktree_session_at_start(conn, early_sid)
            or _is_worktree_session(cwd)
        ):
            summary = "worktree=1"
        else:
            summary = ""

        if early_sid:
            try:
                _write_lifecycle_end(early_sid, summary)
            except Exception:  # noqa: BLE001 — never block session_end
                pass
            # Drop per-session recall dedup state — next window starts clean.
            _wipe_recall_seen(early_sid)
            _wipe_sticker_nudge(early_sid)
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
