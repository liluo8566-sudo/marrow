"""Thin CC hook entrypoints. `python -m marrow.hooks <event>`.

Code-only, no LLM. Parallel-safe with the legacy ny-memm hooks —
marrow registers ALONGSIDE them, never replaces. Logic lives in the marrow
package; this only does hook I/O (stdin JSON in, stdout JSON for
SessionStart additionalContext, side effects for SessionEnd).

  session_start      -> inject open tasks + alerts + affect backdrop
  session_end        -> clean transcript, archive events, regen dashboard top
  user_prompt_submit -> deterministic vector recall fallback (scaffold; default off)

PreToolUse is the global prompt-guard.py (scope already covers
~/cc-lab/marrow/), not duplicated here.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from . import config, dashboard, repo, storage, top_sections, transcript
from .popen_detach import popen_detach

SESSION_START_HARD_CAP = 6000


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
    inp = _read_input()
    db = config.db_path()
    conn = storage.connect(db)
    try:
        # Write lifecycle:start marker so catchup can detect live vs dead sessions.
        sid = inp.get("session_id") if isinstance(inp, dict) else None
        if sid:
            try:
                ppid = os.getppid()
                started_at = _started_at_for(ppid)
                with conn:
                    conn.execute(
                        "INSERT INTO audit_log"
                        " (target_table, target_id, action, summary)"
                        " VALUES ('events', ?, 'session_lifecycle:start', ?)",
                        (sid, f"ppid={ppid},source=cc,started_at={started_at}"),
                    )
            except Exception:  # noqa: BLE001 — never block session_start
                pass

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
    db = config.db_path()
    conn = storage.connect(db)
    try:
        rows = transcript.clean(tpath)
        if rows:
            repo.archive_events(conn, rows)
        state = str(config.DATA_DIR / "state")
        dash = inp.get("marrow_dashboard") or config.dashboard_path()
        try:
            dashboard.write_dashboard(dash, conn, state_dir=state, db=db)
        except PermissionError as e:
            # TCC-protected Desktop / unauthorized context: skip the full
            # re-render (lossless — next authorized session_end rewrites it).
            # Alert so the operator sees the TCC block instead of a silent
            # stale dashboard (DESIGN L33: every step writes alert on fail).
            repo.add_alert(
                "warn", "dashboard",
                f"session_end skipped dashboard write: {e}",
                source="hooks.py", db=db,
            )
        # Sub-pages are owned by daily.py (07:00 routine + 19:00 catchup).
        # session_end used to call write_all_subpages here unconditionally,
        # which (a) re-rendered milestone.md every session even though no
        # session-scoped data feeds it, (b) ran reconcile_milestones N times
        # per day for no reason, (c) coupled with the old pinned=0 leak made
        # the dashboard `Milestone candidate` block re-grow after manual
        # deletes. session_end now only owns dashboard top (alerts/tasks/
        # affect) + sessionend_async (handover + LLM extraction).
        # Bug #1 fix: handover.md is written ONLY by sessionend_async
        # (single-writer rule). Sync skeleton write removed — it raced the
        # async LLM injector and clobbered ThisSession/NextSession content.
        # SessionStart stays read-only against handover.md.

        # Auto-embed events freshly archived this session so recall stays
        # current without a manual MCP call. Fail-soft: embedder absence or
        # any runtime error must never block session_end.
        try:
            from . import recall as recall_mod
            recall_mod.embed_pending(conn, batch=200)
        except Exception as e:
            repo.add_alert(
                "warn", "embed",
                f"session_end embed_pending failed: {e}",
                source="hooks.py", db=db,
            )
        # Fire async LLM extraction (SessionEnd async). Lifecycle markers and
        # idempotent gate live here; sessionend_async owns its own skip gate too.
        try:
            sid = rows[0]["session_id"] if rows else None
            if sid:
                # 1. Write lifecycle:end marker (always, best-effort).
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

                # 2. Idempotent gate: skip popen if events haven't grown since last ok.
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
                    popen_detach(
                        [sys.executable, "-m", "marrow.sessionend_async", "--sid", sid],
                        log_path=log,
                    )
        except Exception as e:
            try:
                repo.add_alert(
                    "warn", "sessionend_async",
                    f"session_end async spawn failed: {e}",
                    source="hooks.py", db=db,
                )
            except Exception:
                pass
    finally:
        conn.close()
    return 0


def user_prompt_submit() -> int:
    """Inject top-K recall hits as UserPromptSubmit additionalContext.

    Config flag: [recall] vector = true (default on). Set false to disable.
    Fusion weights come from [recall] in config; recall.recall_fusion blends
    vec + bm25 + recency + affect. Fail-soft: any error falls through to a
    no-op so the user prompt always reaches the model.
    """
    inp = _read_input()
    cfg = config.load()
    if not cfg.get("recall", {}).get("vector", False):
        return 0

    prompt_text = (inp.get("prompt") or "").strip() if isinstance(inp, dict) else ""
    if not prompt_text:
        return 0

    try:
        from . import recall as recall_mod
        conn = storage.connect(config.db_path())
        try:
            hits = recall_mod.recall_with_config(conn, prompt_text)
        finally:
            conn.close()
    except Exception:
        return 0  # fail-soft: never break the user turn

    if not hits:
        return 0

    lines = ["## Recall (auto)"]
    for h in hits:
        ts = (h.get("timestamp") or "")[:10]
        snippet = (h.get("content") or "").replace("\n", " ")[:300]
        lines.append(f"- [{ts}] {snippet}")
    ctx = "\n".join(lines)

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
