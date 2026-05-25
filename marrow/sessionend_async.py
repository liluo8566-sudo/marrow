"""SessionEnd async LLM extraction: 2 sonnet calls → 4 segment writers.

CLI: python -m marrow.sessionend_async --sid <session_id>

STATE call emits TASK_CAND + HANDOVER. NARRATIVE call emits AFFECT + DIGEST.
Both prompts share a byte-identical transcript-fence prefix so the second
call's cache_read > 0 (audit_log.llm_call_cost). One call failing does not
block the other.

Skip rule: sessions with ≤ skip_turn_threshold user turns extract nothing.
Stale-skip recovery: if a prior skip:short_session row exists but the
session has since grown past threshold (cc mid-flush partial archive),
drop the skip and process.

ENTITY/MILESTONE/MEMES candidate extraction lives in daily.py.
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, handover_render, repo, storage
from .llm import LLMClient, LLMError
from .sessionend_prompts import NARRATIVE_PROMPT, STATE_PROMPT
from .sessionend_writers import (seg_affect, seg_digest, seg_handover,
                                 seg_task_cand)

_LOGS_DIR = Path.home() / ".config" / "marrow" / "logs"
_TZ = ZoneInfo("Australia/Melbourne")
_CUTOFF_H = 6  # 6AM day boundary (per pipeline §6)

_SUMMARY_OK = "ok"
_SUMMARY_SKIP = "skip:short_session"
_SUMMARY_START = "start"

_SEGMENTS = ("affect", "task_cand", "digest", "handover")


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
    row = conn.execute(
        "SELECT 1 FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=? AND summary=?",
        (sid, _SUMMARY_OK),
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
        " WHERE action='sessionend_extract' AND target_id=? AND summary=?"
        " ORDER BY id DESC LIMIT 1",
        (sid, _SUMMARY_SKIP),
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
    lines = [f"[{label.get(r['role'], r['role'])}] {r['content']}" for r in rows]
    date = _to_local_date(rows[0]["timestamp"])
    return "\n".join(lines), date


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
            " SUM(CASE WHEN summary IN ('ok','skip:short_session') THEN 1 ELSE 0 END) AS done,"
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
    """db active task snapshot for sonnet's tick decisions. Title is the
    match key — sonnet must copy verbatim so seg_task_cand flips by title."""
    rows = conn.execute(
        "SELECT title, category FROM tasks WHERE status='active'"
        " ORDER BY id"
    ).fetchall()
    if not rows:
        return "_none_"
    return "\n".join(f"- {r['title']} ({r['category']})" for r in rows)


def _load_prior_handover_for_sonnet() -> str:
    """Read prior handover.md and extract the 4 state-axis sections (Done /
    Open / Plan / Reference) for sonnet's audit step. Top-section (Alerts /
    Tasks / Affect / Milestone candidate) is code-owned and not included."""
    try:
        text = handover_render._RENDERED_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return "(no prior handover)"
    parts: list[str] = []
    for header in ("Done", "Open", "Plan", "Reference"):
        body = handover_render._split_section_body(text, header).strip()
        parts.append(f"## {header}\n{body or '- N/A'}")
    return "\n\n".join(parts)


# ── main loop ───────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    sid: str | None = None
    i = 0
    while i < len(args):
        if args[i] == "--sid" and i + 1 < len(args):
            sid = args[i + 1]
            i += 2
        else:
            i += 1

    if not sid:
        print("usage: python -m marrow.sessionend_async --sid <session_id>",
              file=sys.stderr)
        return 2

    cfg = config.load()
    threshold = cfg.get("sessionend", {}).get("skip_turn_threshold", 5)
    db = config.db_path()
    conn = storage.connect(db)
    try:
        if _already_done(conn, sid):
            return 0
        # Silent-death root cause: cc fires session_end mid-flush. The first
        # hook can write skip:short_session while only a partial slice of
        # events is on disk. _drop_stale_skip clears that row when event
        # count grew past threshold so the real run isn't blocked.
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
        if count <= threshold:
            _write_final_audit(conn, sid, _SUMMARY_SKIP)
            return 0

        events_text, date = _session_events_text(conn, sid)
        if not events_text:
            _write_final_audit(conn, sid, "fail:no_events")
            return 1

        return _run_extraction(conn, sid, date, events_text, cfg)
    except Exception as e:  # noqa: BLE001
        try:
            _write_final_audit(conn, sid, f"fail:{type(e).__name__}")
        except Exception:
            pass
        return 1
    finally:
        conn.close()


def _run_extraction(conn, sid: str, date: str,
                    events_text: str, cfg: dict) -> int:
    """Two-call flow: STATE + NARRATIVE → 4 segment writers + final audit."""
    client = LLMClient(cfg=cfg)
    prior_handover = _load_prior_handover_for_sonnet()
    active_tasks = _load_active_tasks_for_sonnet(conn)

    state_raw, state_err = "", None
    try:
        state_raw = client.call(
            role="sessionend_state",
            body=STATE_PROMPT.format(
                sid=sid, events=events_text,
                prior_handover=prior_handover,
                active_tasks=active_tasks),
            tier="mid",
        )
    except (LLMError, ValueError, RuntimeError) as e:
        state_err = type(e).__name__

    narrative_raw, narrative_err = "", None
    try:
        narrative_raw = client.call(
            role="sessionend_narrative",
            body=NARRATIVE_PROMPT.format(sid=sid, events=events_text),
            tier="mid",
        )
    except (LLMError, ValueError, RuntimeError) as e:
        narrative_err = type(e).__name__

    if state_err and narrative_err:
        _write_final_audit(
            conn, sid, f"fail:state={state_err},narrative={narrative_err}")
        return 1
    if state_err:
        _write_segment_audit(conn, sid, "state_call", f"fail:{state_err}")
    if narrative_err:
        _write_segment_audit(conn, sid, "narrative_call",
                             f"fail:{narrative_err}")

    writers = (
        ("affect",    "narrative", lambda: seg_affect(
            conn, narrative_raw, sid, date)),
        ("task_cand", "state",     lambda: seg_task_cand(conn, state_raw)),
        ("digest",    "narrative", lambda: seg_digest(
            conn, narrative_raw, sid, date)),
        ("handover",  "state",     lambda: seg_handover(
            conn, state_raw, sid)),
    )
    failures: list[str] = []
    for name, src, writer in writers:
        if src == "state" and state_err:
            _write_segment_audit(
                conn, sid, name, f"skip:state_failed_{state_err}")
            failures.append(name)
            continue
        if src == "narrative" and narrative_err:
            _write_segment_audit(
                conn, sid, name, f"skip:narrative_failed_{narrative_err}")
            failures.append(name)
            continue
        try:
            writer()
            _write_segment_audit(conn, sid, name, "ok")
        except (ValueError, RuntimeError, TypeError, KeyError) as e:
            failures.append(name)
            try:
                _write_segment_audit(
                    conn, sid, name, f"fail:{type(e).__name__}")
            except Exception:
                pass

    if not failures:
        _write_final_audit(conn, sid, _SUMMARY_OK)
        return 0
    if len(failures) == len(writers):
        _write_final_audit(conn, sid, "fail:all")
        return 1
    _write_final_audit(conn, sid, f"partial:{','.join(failures)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
