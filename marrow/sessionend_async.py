"""SessionEnd async LLM extraction: single sonnet call for all segments.

CLI: python -m marrow.sessionend_async --sid <session_id> [--cwd <path>]

One sonnet call (TASK_AFFECT_DIGEST_PROMPT) → seg_task_cand + seg_affect +
seg_digest (KIND/TL/LIFE/VOICE/FACTS). Single transcript read; all segments
from one output.

`--cwd` lets _load_git_log locate the repo for CLOSE evidence; absent → ""
(study / ny chats have no commits).

Skip rule: sessions with ≤ skip_turn_threshold user turns extract nothing.
Stale-skip recovery: if a prior skip:short_session row exists but the
session has since grown past threshold (cc mid-flush partial archive),
drop the skip and process.

ENTITY/MILESTONE/MEMES candidate extraction lives in daily.py.
"""
from __future__ import annotations

import atexit as _atexit
import datetime as _dt
import fcntl
import subprocess as _sp
import sys
from pathlib import Path as _Path
# Lazy stdio redirect — MUST run before any heavyweight import so
# import-time tracebacks land in --log-path when they fire. popen_detach
# itself is stdlib-only, safe to import first.
from .popen_detach import _redirect_stdio_from_argv as _redirect_stdio
_redirect_stdio()

from . import config, repo, storage
from .hooks import _FORCE_SESSIONEND_ACTION, _is_manual_skip
from .llm import LLMClient, LLMError
from .sessionend_prompts import TASK_AFFECT_DIGEST_PROMPT
from .sessionend_writers import _seg_digest_ts, seg_affect, seg_digest, seg_task_cand

_TZ = config.get_tz()
_CUTOFF_H = 6  # 6AM day boundary (per pipeline §6)

_OK_PREFIX = "ok,user_count="
_SUMMARY_OK = "ok"  # legacy; kept for backward-compat checks
_SUMMARY_SKIP = "skip:short_session"
_SUMMARY_START = "start"

_WRITERS = ("affect", "task_cand", "digest")
_LOCK_FD = None


def _cleanup_empty_log(log_path: _Path) -> None:
    """Unlink the spawn log if the child wrote nothing (normal success path).
    Stderr/traceback paths leave the file intact for postmortem."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        if log_path.exists() and log_path.stat().st_size == 0:
            log_path.unlink()
    except Exception:  # noqa: BLE001
        pass


# ── helpers ─────────────────────────────────────────────────────────────────

def _to_local_date(utc_iso: str) -> str:
    """UTC ISO -> local diary day by 6AM cutoff."""
    s = utc_iso.strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return _dt.date.today().isoformat()
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    local = d.astimezone(_TZ) - _dt.timedelta(hours=_CUTOFF_H)
    return local.date().isoformat()


def _user_event_count(conn, sid: str, after_event_id: int | None = None) -> int:
    if after_event_id is not None:
        row = conn.execute(
            "SELECT COUNT(*) c FROM events"
            " WHERE session_id = ? AND role = 'user' AND id > ?",
            (sid, after_event_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE session_id = ? AND role = 'user'",
            (sid,),
        ).fetchone()
    return row["c"] if row else 0


def _has_force_sessionend(conn, sid: str) -> bool:
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


def _already_done(conn, sid: str) -> bool:
    """True iff this sid has already been fully covered.

    New semantics: look for the most recent `ok,user_count=N` row; if current
    user_count > N, return False so incremental runs trigger. Backward compat:
    a legacy `summary='ok'` row (no user_count) is treated as fully covered
    to avoid needless re-runs on historical data.

    Force rows posted after the last ok row act as rerun signals.
    """
    if _has_force_sessionend(conn, sid):
        return False

    # Keep legacy CLI reset rows working for mw sessionend rerun.
    latest_row = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=?"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if latest_row and latest_row["summary"].startswith("reset:"):
        return False

    # Check for new-style ok row with user_count.
    ok_row = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=?"
        " AND (summary LIKE 'ok,user_count=%' OR summary LIKE 'skip:short_session%')"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if ok_row:
        try:
            n = int(ok_row["summary"].split("=", 1)[1])
        except (ValueError, IndexError):
            return True  # malformed but row exists → treat as done
        return _user_event_count(conn, sid) <= n

    # Backward compat: legacy plain 'ok' row.
    legacy_row = conn.execute(
        "SELECT 1 FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=? AND summary=?",
        (sid, _SUMMARY_OK),
    ).fetchone()
    return legacy_row is not None


def _has_mm_plus_reset(conn, sid: str) -> bool:
    """Legacy reset:mm_plus support for mw sessionend rerun."""
    row = conn.execute(
        "SELECT 1 FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=?"
        " AND summary='reset:mm_plus'"
        " AND id > COALESCE("
        "   (SELECT MAX(id) FROM audit_log"
        "    WHERE action='sessionend_extract' AND target_id=?"
        "    AND summary LIKE 'ok,%'), 0)"
        " LIMIT 1",
        (sid, sid),
    ).fetchone()
    return row is not None


def _drop_stale_skip(conn, sid: str, threshold: int) -> bool:
    """Silent-death fix: cc fires session_end mid-flush — first hook archives
    a partial 7-8 events, sessionend_async skips (below threshold), then the
    real session ends with 41 events. Old code kept the skip row forever
    (skip counted as terminal in _should_skip), the sid never re-processed.

    Now: if a skip row exists but the current user event count is past the
    threshold, drop the skip row + leave an audit trail and let the main
    extraction path run. Returns True if a stale skip was cleared."""
    skip_row = conn.execute(
        "SELECT id FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=?"
        " AND (summary=? OR summary LIKE ?)"
        " ORDER BY id DESC LIMIT 1",
        (sid, _SUMMARY_SKIP, f"{_SUMMARY_SKIP},%"),
    ).fetchone()
    if not skip_row:
        return False
    if _user_event_count(conn, sid) <= threshold:
        return False  # still genuinely short — keep the skip
    with conn:
        conn.execute("DELETE FROM audit_log WHERE id=?", (skip_row["id"],))
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', 'reset:stale_skip')",
            (sid,),
        )
    return True


def _session_events_text(conn, sid: str,
                         after_event_id: int | None = None) -> tuple[str, str]:
    """Return (raw events block, session date). Empty session -> ('', today).
    Transcript fence lives inside the prompt body (sessionend_prompts), so
    we only emit the role-tagged content here."""
    if after_event_id is not None:
        rows = conn.execute(
            "SELECT timestamp, role, content FROM events"
            " WHERE session_id=? AND id > ? ORDER BY timestamp, id",
            (sid, after_event_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT timestamp, role, content FROM events"
            " WHERE session_id=? ORDER BY timestamp, id",
            (sid,),
        ).fetchall()
    if not rows:
        return "", _dt.date.today().isoformat()
    _p = config.persona()
    label = {"user": _p["user_name"], "assistant": _p["assistant_name"]}
    lines = [
        f"[{_local_hhmm(r['timestamp'])}] [{label.get(r['role'], r['role'])}]"
        f" {r['content']}"
        for r in rows
    ]
    date = _to_local_date(rows[0]["timestamp"])
    return "\n".join(lines), date


def _local_hhmm(utc_iso: str) -> str:
    """UTC ISO timestamp -> local Australia/Melbourne HH:MM. '??:??' on parse
    error so a malformed row never breaks the transcript."""
    s = (utc_iso or "").strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return "??:??"
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_TZ).strftime("%H:%M")


def _write_segment_audit(conn, sid: str, segment: str, summary: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, ?, ?)",
            (sid, f"sessionend_extract_{segment}", summary),
        )


def _write_final_audit(conn, sid: str, summary: str) -> None:
    """Insert final summary row + alert when effective failures cross 2.
    Effective failures = fail/partial rows + silent deaths (start rows
    without a matching terminal). Silent-death count excludes the current
    attempt's own 'start' row."""
    prior_fails = 0
    if summary.startswith(("fail:", "partial:")):
        row = conn.execute(
            "SELECT"
            " SUM(CASE WHEN summary LIKE 'fail:%' OR summary LIKE 'partial:%' THEN 1 ELSE 0 END) AS fails,"
            " SUM(CASE WHEN summary='ok' OR summary LIKE 'ok,user_count=%'"
            "          OR summary LIKE 'skip:short_session%' THEN 1 ELSE 0 END) AS done,"
            " SUM(CASE WHEN summary='start' THEN 1 ELSE 0 END) AS starts"
            " FROM audit_log"
            " WHERE action='sessionend_extract' AND target_id=?",
            (sid,),
        ).fetchone()
        if row:
            fails = row["fails"] or 0
            done = row["done"] or 0
            starts = row["starts"] or 0
            silent_deaths = max(0, starts - 1 - (done + fails))
            prior_fails = fails + silent_deaths
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', ?)",
            (sid, summary),
        )
    if summary.startswith(("fail:", "partial:")) and prior_fails >= 1:
        try:
            sev = "critical" if summary.startswith("fail:") else "warn"
            # Type-level fingerprint: all sids whose retry also failed collapse
            # into a single dashboard row; repo.add_alert bumps hit_count and
            # refreshes the message to the most-recent sid+summary so the row
            # always shows the latest failure rather than flooding.
            repo.add_alert(
                sev, "sessionend_async",
                "sessionend_async_retry_failed",
                source="sessionend_async.py", db=config.db_path(),
                message=(
                    f"latest sid={sid[:8]} {summary} "
                    f"(catchup retry also failed; prior_fails={prior_fails})"
                ),
            )
        except Exception:  # noqa: BLE001 — alert is best-effort
            pass


# ── input loaders ────────────────────────────────────────────────────────────

def _load_active_tasks_for_sonnet(conn) -> str:
    """db active task snapshot WITH id for sonnet's tick-by-id decisions.
    Sonnet emits {"id": N, "status": "done"}; code flips WHERE id=?."""
    rows = conn.execute(
        "SELECT id, title, category FROM tasks WHERE status='active'"
        " ORDER BY id"
    ).fetchall()
    if not rows:
        return "_none_"
    return "\n".join(
        f"- [#{r['id']}] {r['title']} ({r['category']})" for r in rows)


def _load_git_log(cwd: str | None, since_ts: int) -> str:
    """Commit subjects since `since_ts` from the repo at cwd. Off-repo / any
    error / no cwd → '' (study & ny chats have no commits → sonnet falls back
    to the transcript)."""
    if not cwd:
        return ""
    try:
        proc = _sp.run(
            ["git", "-C", cwd, "log", f"--since=@{since_ts}", "--format=%s"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 — never let git crash extraction
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


# ── main loop ───────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    sid: str | None = None
    cwd: str = ""
    log_path: str = ""
    after_event_id: int | None = None
    segment_seq = 0
    i = 0
    while i < len(args):
        if args[i] == "--sid" and i + 1 < len(args):
            sid = args[i + 1]
            i += 2
        elif args[i] == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
            i += 2
        elif args[i] == "--log-path" and i + 1 < len(args):
            log_path = args[i + 1]
            i += 2
        elif args[i] == "--after-event-id" and i + 1 < len(args):
            after_event_id = int(args[i + 1])
            i += 2
        elif args[i] == "--segment-seq" and i + 1 < len(args):
            segment_seq = int(args[i + 1])
            i += 2
        else:
            i += 1

    log_obj = _Path(log_path) if log_path else None
    if log_obj:
        _atexit.register(_cleanup_empty_log, log_obj)  # SIGKILL backstop

    try:
        if not sid:
            print("usage: python -m marrow.sessionend_async --sid <session_id>"
                  " [--cwd <path>] [--log-path <path>]", file=sys.stderr)
            return 2

        cfg = config.load()
        db = config.db_path()
        global _LOCK_FD
        lock_path = config.DATA_DIR / "sessionend.lock"
        _LOCK_FD = lock_path.open("a+")
        try:
            fcntl.flock(_LOCK_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                _conn = storage.connect(db)
                try:
                    with _conn:
                        _conn.execute(
                            "INSERT INTO audit_log"
                            " (target_table, target_id, action, summary)"
                            " VALUES ('events', ?, 'sessionend_extract', 'skip:locked')",
                            (sid,),
                        )
                finally:
                    _conn.close()
            except Exception:
                pass
            return 0

        threshold = cfg.get("sessionend", {}).get("skip_turn_threshold", 3)
        conn = storage.connect(db)
        try:
            if _already_done(conn, sid):
                return 0
            # Manual skip: mm- prefix wrote a manual_skip/skip row; latest row wins.
            if _is_manual_skip(conn, sid):
                _write_final_audit(conn, sid, "skip:manual")
                return 0
            # Silent-death root cause: cc fires session_end mid-flush while only
            # a partial slice of events is on disk. The original skip:short_session
            # row then blocked every later re-run. _drop_stale_skip clears the row
            # only when event count grew past threshold since the skip was written.
            _drop_stale_skip(conn, sid, threshold)

            # Stamp "start" the moment we own the work. Any silent death below
            # leaves this row behind so catchup counts it as one failed attempt.
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO audit_log (target_table, target_id, action, summary)"
                        " VALUES ('events', ?, 'sessionend_extract', ?)",
                        (sid, _SUMMARY_START),
                    )
            except Exception:  # noqa: BLE001 — never block extraction on audit
                pass

            count = _user_event_count(conn, sid, after_event_id)
            if count == 0:
                _write_final_audit(conn, sid, f"{_SUMMARY_SKIP},user_count=0")
                return 0
            force_run = _has_force_sessionend(conn, sid) or _has_mm_plus_reset(conn, sid)
            if count <= threshold and not force_run:
                _write_final_audit(conn, sid, f"{_SUMMARY_SKIP},user_count={count}")
                return 0

            events_text, date = _session_events_text(conn, sid, after_event_id)
            if not events_text:
                _write_final_audit(conn, sid, "fail:no_events")
                return 1

            return _run_extraction(conn, sid, date, events_text, cfg, count,
                                   cwd, segment_seq, after_event_id)
        except Exception as e:  # noqa: BLE001
            try:
                _write_final_audit(conn, sid, f"fail:{type(e).__name__}: {e}"[:220])
            except Exception:
                pass
            return 1
        finally:
            conn.close()
    finally:
        # Active cleanup — atexit doesn't fire under SIGKILL, but this finally
        # always runs on normal return paths (already_done / skip:* / fail:*).
        if log_obj is not None:
            _cleanup_empty_log(log_obj)


def _run_writer(conn, sid: str, name: str, writer, *, zero_is_fail: bool = False):
    """Run one writer; log audit row. Returns the writer's row count on
    success (audited ok, or ok:0 on zero rows), None on exception.

    When zero_is_fail=True, a zero-row result is audited as fail:zero_rows
    instead of ok:0 so it joins the failures list for retry/two-strike logic.

    Catches every Exception (including sqlite3.OperationalError and OSError)
    so one writer failing never escapes to the outer session-level try and
    flips the whole session to fail. The audit row carries the writer name
    explicitly, so downstream final-audit sees this as a per-writer partial,
    not a session-wide blowup.
    """
    try:
        n = writer()
        if n == 0 and zero_is_fail:
            _write_segment_audit(conn, sid, name, "fail:zero_rows")
        else:
            _write_segment_audit(conn, sid, name, "ok:0" if n == 0 else "ok")
        return n
    except Exception as e:  # noqa: BLE001
        try:
            _write_segment_audit(conn, sid, name, f"fail:{type(e).__name__}: {e}"[:200])
        except Exception:  # noqa: BLE001
            pass
        return None


def _collect_run_failures(conn, sid: str) -> list[str]:
    """Failed segment names from THIS run only — rows after the latest
    'start' stamp. Stale fail rows from a prior attempt must not flip a
    fully-successful retry back to partial (which would false-fire the
    two-strike alert)."""
    seg_rows = conn.execute(
        "SELECT action, summary FROM audit_log"
        " WHERE target_id=? AND action LIKE 'sessionend_extract_%'"
        " AND action != 'sessionend_extract_llm_call'"
        " AND id > COALESCE((SELECT MAX(id) FROM audit_log"
        "   WHERE action='sessionend_extract' AND target_id=?"
        "   AND summary='start'), 0)",
        (sid, sid),
    ).fetchall()
    return [
        r["action"].removeprefix("sessionend_extract_")
        for r in seg_rows
        if not r["summary"].startswith("ok")
    ]


def _run_extraction(conn, sid: str, date: str,
                    events_text: str, cfg: dict, count: int,
                    cwd: str = "", segment_seq: int = 0,
                    after_event_id: int | None = None) -> int:
    """Single sonnet call: TASK_AFFECT_DIGEST_PROMPT emits all segments.
    dashboard + embed_pending run at tail (fail-soft).
    """
    from . import dashboard as _dash_mod

    client = LLMClient(cfg=cfg)
    active_tasks = _load_active_tasks_for_sonnet(conn)
    since_ts = int(_dt.datetime.now(_dt.timezone.utc).timestamp()) - 24 * 3600
    git_log = _load_git_log(cwd, since_ts)
    tl_rows = conn.execute(
        "SELECT life_lines FROM session_digests"
        " WHERE life_lines IS NOT NULL AND tl_hidden = 0"
        " ORDER BY ts DESC, segment_seq DESC LIMIT 3",
    ).fetchall()
    timeline_context = (
        "\n".join(r["life_lines"] for r in reversed(tl_rows))
        if tl_rows else "(none)"
    )

    # ── single call: all segments (sonnet mid) ────────────────────────────────
    mid_hhmm = _local_hhmm(_seg_digest_ts(conn, sid, after_event_id))
    raw, call_err = "", None
    persona = config.persona()
    user_terms = " / ".join(config.all_user_terms())
    assistant_terms = " / ".join(config.all_assistant_terms())
    try:
        raw = client.call(
            role="sessionend_task_affect",
            body=TASK_AFFECT_DIGEST_PROMPT.format(
                sid=sid, events=events_text,
                active_tasks=active_tasks, git_log=git_log,
                timeline_context=timeline_context,
                user_name=persona["user_name"],
                assistant_name=persona["assistant_name"],
                user_terms=user_terms,
                assistant_terms=assistant_terms,
                mid_time=mid_hhmm),
            tier="mid",
        )
    except (LLMError, ValueError, RuntimeError) as e:
        call_err = f"{type(e).__name__}: {e}"[:200]

    if call_err:
        _write_segment_audit(conn, sid, "llm_call", f"fail:{call_err}")
        _write_final_audit(conn, sid, f"fail:llm={call_err}")
        return 1

    _run_writer(conn, sid, "task_cand",
                lambda: seg_task_cand(conn, raw))
    _run_writer(conn, sid, "affect",
                lambda: seg_affect(conn, raw, sid, date))
    _run_writer(conn, sid, "digest",
                lambda: seg_digest(conn, raw, sid, date, raw_llm=raw,
                                   segment_seq=segment_seq,
                                   after_event_id=after_event_id),
                zero_is_fail=True)

    # ── final audit ───────────────────────────────────────────────────────────
    failures = _collect_run_failures(conn, sid)

    all_writers = ("task_cand", "affect", "digest")
    if not failures:
        _write_final_audit(conn, sid, f"{_OK_PREFIX}{count}")
        rc = 0
    elif len(failures) >= len(all_writers):
        _write_final_audit(conn, sid, "fail:all")
        rc = 1
    else:
        _write_final_audit(conn, sid, f"partial:{','.join(sorted(set(failures)))}")
        rc = 0

    # Write watermark when digest succeeded — prevents re-extraction on partial failure
    if segment_seq > 0 and "digest" not in failures:
        max_ev = conn.execute(
            "SELECT MAX(id) FROM events WHERE session_id=?", (sid,)
        ).fetchone()[0]
        if max_ev is not None:
            storage.insert_watermark(conn, sid, segment_seq, max_ev, count)

    # ── tail: slow side-effects (fail-soft; cc can't kill us here) ───────────
    db = config.db_path()
    try:
        state_dir = str(config.DATA_DIR / "state")
        _dash_mod.write_dashboard(
            config.dashboard_path(), conn, state_dir=state_dir, db=db)
    except Exception as e:  # noqa: BLE001
        try:
            repo.add_alert("warn", "dashboard",
                           "sessionend_async_dashboard_failed",
                           source="sessionend_async.py", db=db,
                           message=f"sessionend_async dashboard write failed: {e}")
        except Exception:  # noqa: BLE001
            pass

    embed_batch = cfg.get("sessionend", {}).get("embed_batch", 200)
    if embed_batch:
        try:
            from . import recall as _recall_mod
            _recall_mod.embed_pending(conn, batch=embed_batch)
        except Exception as e:  # noqa: BLE001
            try:
                repo.add_alert("warn", "embed",
                               "sessionend_async_embed_failed",
                               source="sessionend_async.py", db=db,
                               message=f"sessionend_async embed_pending failed: {e}")
            except Exception:  # noqa: BLE001
                pass

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
