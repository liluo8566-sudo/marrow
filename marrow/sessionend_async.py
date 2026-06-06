"""SessionEnd async LLM extraction: 2 sonnet calls → 4 segment writers.

CLI: python -m marrow.sessionend_async --sid <session_id> [--cwd <path>]

STATE call emits TASK board + DOING diff + NOTE_DONE. NARRATIVE call emits
AFFECT + DIGEST. Both prompts share a byte-identical transcript-fence prefix
so the second call's cache_read > 0 (audit_log.llm_call_cost). One call
failing does not block the other. `--cwd` lets _load_git_log locate the repo
for CLOSE evidence; absent → "" (study / ny chats have no commits).

Skip rule: sessions with ≤ skip_turn_threshold user turns extract nothing.
Stale-skip recovery: if a prior skip:short_session row exists but the
session has since grown past threshold (cc mid-flush partial archive),
drop the skip and process.

ENTITY/MILESTONE/MEMES candidate extraction lives in daily.py.
"""
from __future__ import annotations

import atexit as _atexit
import datetime as _dt
import re as _re
import subprocess as _sp
import sys
from pathlib import Path as _Path
from zoneinfo import ZoneInfo

from . import config, handover_diff, handover_render, repo, storage
from .hooks import _is_manual_skip
from .llm import LLMClient, LLMError
from .paths import paths
from .sessionend_prompts import NARRATIVE_PROMPT, STATE_PROMPT
from .sessionend_writers import (seg_affect, seg_digest, seg_handover,
                                 seg_task_cand)

_LOGS_DIR = paths.logs_dir
_TZ = ZoneInfo("Australia/Melbourne")
_CUTOFF_H = 6  # 6AM day boundary (per pipeline §6)

_OK_PREFIX = "ok,user_count="
_SUMMARY_OK = "ok"  # legacy; kept for backward-compat checks
_SUMMARY_SKIP = "skip:short_session"
_SUMMARY_START = "start"

_SEGMENTS = ("affect", "task_cand", "digest", "handover")


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


def _user_event_count(conn, sid: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE session_id = ? AND role = 'user'",
        (sid,),
    ).fetchone()
    return row["c"] if row else 0


def _already_done(conn, sid: str) -> bool:
    """True iff this sid has already been fully covered.

    New semantics: look for the most recent `ok,user_count=N` row; if current
    user_count > N, return False so incremental runs trigger. Backward compat:
    a legacy `summary='ok'` row (no user_count) is treated as fully covered
    to avoid needless re-runs on historical data.

    Reset rows (reset:mm_plus, reset:stale_skip) posted after the last ok row
    act as force-rerun signals — return False so the pipeline runs again.
    """
    # If the most recent sessionend_extract row is a reset:*, treat as not done.
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
        " AND summary LIKE 'ok,user_count=%'"
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
    """True iff latest non-start sessionend_extract row for sid is reset:mm_plus."""
    # Skip the 'start' row stamped at line 296; the marker is consumed once
    # _run_extraction writes a fresh ok,user_count=N row over it.
    row = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=?"
        " AND summary != ?"
        " ORDER BY id DESC LIMIT 1",
        (sid, _SUMMARY_START),
    ).fetchone()
    return bool(row and row["summary"] == "reset:mm_plus")


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


def _session_events_text(conn, sid: str) -> tuple[str, str]:
    """Return (raw events block, session date). Empty session -> ('', today).
    Transcript fence lives inside the prompt body (sessionend_prompts), so
    we only emit the role-tagged content here."""
    rows = conn.execute(
        "SELECT timestamp, role, content FROM events"
        " WHERE session_id=? ORDER BY timestamp, id",
        (sid,),
    ).fetchall()
    if not rows:
        return "", _dt.date.today().isoformat()
    label = {"user": "念念", "assistant": "屿忱"}
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
            repo.add_alert(
                sev, "sessionend_async",
                f"sid={sid[:8]} {summary} (catchup retry also failed)",
                source="sessionend_async.py", db=config.db_path(),
            )
        except Exception:  # noqa: BLE001 — alert is best-effort
            pass


# ── sonnet input loaders ────────────────────────────────────────────────────

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


def _read_handover_text() -> str:
    try:
        return handover_render._RENDERED_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _load_doing_for_sonnet() -> str:
    """Current `## Doing` threads, each prefixed `[#id]`, for the DOING_DIFF
    audit. Empty/missing → placeholder."""
    text = _read_handover_text()
    if not text:
        return "(no prior handover)"
    body = handover_diff._section_body(text, handover_diff._DOING_HEADER)
    doing, _ = handover_diff.parse_doing(body)
    if not doing:
        return "(no open threads)"
    parts: list[str] = []
    for ident in sorted(doing):
        block = doing[ident]
        lines = block.splitlines()
        head = f"[#{ident}] {lines[0]}" if lines else f"[#{ident}]"
        parts.append("\n".join([head] + lines[1:]))
    return "\n".join(parts)


def _load_note() -> str:
    """Current `## Lumi's Note` body verbatim, for the {note} prompt input.
    Empty/missing → 'N/A'."""
    text = _read_handover_text()
    if not text:
        return "N/A"
    if not handover_diff._section_present(text, handover_diff._NOTE_HEADER):
        return "N/A"
    body = handover_diff._section_body(text, handover_diff._NOTE_HEADER).strip()
    return body or "N/A"


_READY_TS_RE = _re.compile(r"<!--\s*handover:\s*ready[^>]*ts:(\d+)")


def _since_ts_from_handover() -> int:
    """Epoch of the last HO write (ts: in the ready stamp). No stamp → 24h ago.
    git_log uses this as `--since`; double-counting is harmless (CLOSE is
    idempotent by id)."""
    text = _read_handover_text()
    m = _READY_TS_RE.search(text) if text else None
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp()) - 24 * 3600


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
        threshold = cfg.get("sessionend", {}).get("skip_turn_threshold", 3)
        db = config.db_path()
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

            count = _user_event_count(conn, sid)
            if count <= threshold and not _has_mm_plus_reset(conn, sid):
                _write_final_audit(conn, sid, f"{_SUMMARY_SKIP},user_count={count}")
                return 0

            events_text, date = _session_events_text(conn, sid)
            if not events_text:
                _write_final_audit(conn, sid, "fail:no_events")
                return 1

            return _run_extraction(conn, sid, date, events_text, cfg, count, cwd)
        except Exception as e:  # noqa: BLE001
            try:
                _write_final_audit(conn, sid, f"fail:{type(e).__name__}")
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


def _run_writer(conn, sid: str, name: str, writer) -> bool:
    """Run one writer; log audit row. Returns True on success.

    Catches every Exception (including sqlite3.OperationalError and OSError)
    so one writer failing never escapes to the outer session-level try and
    flips the whole session to fail. The audit row carries the writer name
    explicitly, so downstream final-audit sees this as a per-writer partial,
    not a session-wide blowup.
    """
    try:
        writer()
        _write_segment_audit(conn, sid, name, "ok")
        return True
    except Exception as e:  # noqa: BLE001
        try:
            _write_segment_audit(conn, sid, name, f"fail:{type(e).__name__}")
        except Exception:  # noqa: BLE001
            pass
        return False


def _run_extraction(conn, sid: str, date: str,
                    events_text: str, cfg: dict, count: int,
                    cwd: str = "") -> int:
    """Two-call flow: STATE writers run after call1; NARRATIVE writers after
    call2; dashboard + embed_pending run at tail (fail-soft)."""
    from . import dashboard as _dash_mod
    from . import recall as _recall_mod

    client = LLMClient(cfg=cfg)
    doing = _load_doing_for_sonnet()
    active_tasks = _load_active_tasks_for_sonnet(conn)
    note = _load_note()
    git_log = _load_git_log(cwd, _since_ts_from_handover())

    # ── call 1: STATE (~30s) ──────────────────────────────────────────────────
    state_raw, state_err = "", None
    try:
        state_raw = client.call(
            role="sessionend_state",
            body=STATE_PROMPT.format(
                sid=sid, events=events_text,
                active_tasks=active_tasks, doing=doing,
                git_log=git_log, note=note),
            tier="mid",
        )
    except (LLMError, ValueError, RuntimeError) as e:
        state_err = type(e).__name__

    if state_err:
        _write_segment_audit(conn, sid, "state_call", f"fail:{state_err}")
    else:
        # State-based writers: handover.md surfaces ~30s after popen.
        _run_writer(conn, sid, "handover", lambda: seg_handover(conn, state_raw, sid))
        _run_writer(conn, sid, "task_cand", lambda: seg_task_cand(conn, state_raw))

    # ── call 2: NARRATIVE (~30s; cache_read on events_text fence) ─────────────
    narrative_raw, narrative_err = "", None
    try:
        narrative_raw = client.call(
            role="sessionend_narrative",
            body=NARRATIVE_PROMPT.format(sid=sid, events=events_text),
            tier="mid",
        )
    except (LLMError, ValueError, RuntimeError) as e:
        narrative_err = type(e).__name__

    if narrative_err:
        _write_segment_audit(conn, sid, "narrative_call",
                             f"fail:{narrative_err}")
    else:
        _run_writer(conn, sid, "affect",
                    lambda: seg_affect(conn, narrative_raw, sid, date))
        _run_writer(conn, sid, "digest",
                    lambda: seg_digest(conn, narrative_raw, sid, date))

    # ── tail: slow side-effects (fail-soft; cc can't kill us here) ───────────
    db = config.db_path()
    try:
        state_dir = str(config.DATA_DIR / "state")
        _dash_mod.write_dashboard(
            config.dashboard_path(), conn, state_dir=state_dir, db=db)
    except Exception as e:  # noqa: BLE001
        try:
            repo.add_alert("warn", "dashboard",
                           f"sessionend_async dashboard write failed: {e}",
                           source="sessionend_async.py", db=db)
        except Exception:  # noqa: BLE001
            pass

    try:
        _recall_mod.embed_pending(conn, batch=200)
    except Exception as e:  # noqa: BLE001
        try:
            repo.add_alert("warn", "embed",
                           f"sessionend_async embed_pending failed: {e}",
                           source="sessionend_async.py", db=db)
        except Exception:  # noqa: BLE001
            pass

    # ── final audit ───────────────────────────────────────────────────────────
    if state_err and narrative_err:
        _write_final_audit(
            conn, sid, f"fail:state={state_err},narrative={narrative_err}")
        return 1

    # Collect failures recorded by _run_writer above.
    seg_rows = conn.execute(
        "SELECT action, summary FROM audit_log"
        " WHERE target_id=? AND action LIKE 'sessionend_extract_%'"
        " AND action NOT IN ('sessionend_extract_state_call',"
        "                    'sessionend_extract_narrative_call')",
        (sid,),
    ).fetchall()
    failures: list[str] = [
        r["action"].removeprefix("sessionend_extract_")
        for r in seg_rows
        if not r["summary"].startswith("ok")
    ]
    # Implicit skips for the failed call's writers.
    if state_err:
        for w in ("handover", "task_cand"):
            if w not in failures:
                _write_segment_audit(
                    conn, sid, w, f"skip:state_failed_{state_err}")
                failures.append(w)
    if narrative_err:
        for w in ("affect", "digest"):
            if w not in failures:
                _write_segment_audit(
                    conn, sid, w, f"skip:narrative_failed_{narrative_err}")
                failures.append(w)

    all_writers = ("handover", "task_cand", "affect", "digest")
    if not failures:
        _write_final_audit(conn, sid, f"{_OK_PREFIX}{count}")
        return 0
    if len(failures) >= len(all_writers):
        _write_final_audit(conn, sid, "fail:all")
        return 1
    _write_final_audit(conn, sid, f"partial:{','.join(sorted(set(failures)))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
