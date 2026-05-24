"""SessionEnd async LLM extraction: 1 sonnet call → 4 block writers.

CLI: python -m marrow.sessionend_async --sid <session_id>

One combined SESSIONEND_PROMPT emits AFFECT / TASK_CAND / DIGEST /
HANDOVER blocks. Each writer parses its own marker — one block failing
to parse does not block the others. Audit row per block + final summary
(ok / partial / fail).

Skip rule: sessions with ≤ skip_turn_threshold user turns extract nothing.

ENTITY/MILESTONE/MEMES candidate extraction lives in daily.py (day-
aggregated input is cheaper and dedupes naturally).
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from . import candidates, config, handover_render, storage
from .llm import LLMClient, LLMError
from .sessionend_prompts import SESSIONEND_PROMPT, fence

_LOGS_DIR = Path.home() / ".config" / "marrow" / "logs"
_TZ = ZoneInfo("Australia/Melbourne")
_CUTOFF_H = 6  # 6AM day boundary (per pipeline §6)

_SUMMARY_OK = "ok"
_SUMMARY_SKIP = "skip:short_session"

_SEGMENTS = ("affect", "task_cand", "digest", "handover")


# ── parsing helpers ─────────────────────────────────────────────────────────

def _extract_text_block(text: str, marker: str) -> str:
    """Pull prose between ===<marker>=== and the next ===END==='."""
    open_tag = f"==={marker}==="
    i = text.find(open_tag)
    if i == -1:
        return ""
    tail = text[i + len(open_tag):]
    j = tail.find("===END===")
    return tail[:j].strip() if j != -1 else tail.strip()


def _clamp_importance(x) -> int:
    try:
        return max(1, min(5, int(x)))
    except (TypeError, ValueError):
        return 3


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


# ── DB ops ──────────────────────────────────────────────────────────────────

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


def _session_events_text(conn, sid: str) -> tuple[str, str]:
    """Return (fenced events string, session date). Empty session -> ('', today)."""
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
    return fence("\n".join(lines)), date


def _write_segment_audit(conn, sid: str, segment: str, summary: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, ?, ?)",
            (sid, f"sessionend_extract_{segment}", summary),
        )


def _write_final_audit(conn, sid: str, summary: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'sessionend_extract', ?)",
            (sid, summary),
        )


# ── segment writers ─────────────────────────────────────────────────────────

def _seg_affect(conn, raw: str, sid: str, date: str) -> int:
    """Insert affect rows with importance clamp + unresolved/reconcile linkage."""
    items = candidates.extract_block(raw, "AFFECT")
    if not items:
        return 0
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        ep = int(it.get("ep") or (n + 1))
        valence = float(it.get("valence", 0.5))
        arousal = float(it.get("arousal", 0.3))
        importance = _clamp_importance(it.get("importance", 3))
        label = it.get("label") or None
        desc_raw = it.get("description")
        description = desc_raw.strip() if isinstance(desc_raw, str) else None
        if not description:
            description = label
            print(
                f"[sessionend_async] warn: affect ep={ep} missing description,"
                f" fallback to label={label!r}",
                file=sys.stderr,
            )
        ents = it.get("entities")
        entities = (json.dumps(ents, ensure_ascii=False)
                    if isinstance(ents, list) and ents else None)
        unresolved_raw = it.get("unresolved", 0)
        try:
            unresolved = 1 if int(unresolved_raw) else 0
        except (TypeError, ValueError):
            unresolved = 0
        reconcile_prev = it.get("reconcile_prev")
        if isinstance(reconcile_prev, str):
            rp = reconcile_prev.strip()
            reconcile_prev = None if not rp or rp.upper() == "N/A" else rp
        else:
            reconcile_prev = None

        reconcile_ref = None
        if reconcile_prev:
            prior = conn.execute(
                "SELECT id FROM affect_live"
                " WHERE unresolved=1 AND resolved_at IS NULL"
                " ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if prior:
                reconcile_ref = prior["id"]

        with conn:
            conn.execute(
                "INSERT INTO affect (date, ep, valence, arousal, importance,"
                " label, description, entities, source, unresolved,"
                " reconcile_ref, reconcile_prev_text)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (date, ep, valence, arousal, importance, label, description,
                 entities, "sessionend_async", unresolved, reconcile_ref,
                 reconcile_prev),
            )
            if reconcile_ref:
                ts_now = _dt.datetime.now(_dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    "UPDATE affect SET resolved_at=? WHERE id=?",
                    (ts_now, reconcile_ref),
                )
            n += 1
    return n


_TASK_CATEGORIES = (
    "Appointment", "Assignment", "Study", "Project", "Daily", "Others",
)


def _normalise_category(raw: str | None) -> str:
    if not raw:
        return "Others"
    cleaned = raw.strip().title()
    return cleaned if cleaned in _TASK_CATEGORIES else "Others"


def _seg_task_cand(conn, raw: str) -> int:
    """tasks table: dedup on (title, status='active'); category from LLM \
(whitelist fallback Others)."""
    items = candidates.extract_block(raw, "TASK_CAND")
    if not items:
        return 0
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        status = it.get("status") or "active"
        if status not in ("active", "done", "archived"):
            status = "active"
        category = _normalise_category(it.get("category"))
        due = it.get("due") or None
        note = (it.get("note") or "").strip() or None
        exists = conn.execute(
            "SELECT 1 FROM tasks WHERE title=? AND status='active' LIMIT 1",
            (title,),
        ).fetchone()
        if exists and status == "active":
            continue
        with conn:
            conn.execute(
                "INSERT INTO tasks (category, title, due, status, next_step)"
                " VALUES (?, ?, ?, ?, ?)",
                (category, title, due, status, note),
            )
        n += 1
    return n


def _seg_digest(conn, raw: str, sid: str, date: str) -> int:
    """Persist DIGEST text into session_digests. INSERT OR REPLACE on sid."""
    body = _extract_text_block(raw, "DIGEST")
    if not body:
        return 0
    ts_now = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO session_digests (sid, date, text, ts)"
            " VALUES (?, ?, ?, ?)",
            (sid, date, body, ts_now),
        )
    return 1


def _load_prior_handover_for_sonnet() -> str:
    """Read prior handover.md and extract the narrative sections (Previous /
    This / Next / Reference) for sonnet carry-over judgement. Alerts / Tasks /
    Affect / Milestone candidate are code-owned and not included."""
    try:
        text = handover_render._RENDERED_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return "(no prior handover)"
    parts: list[str] = []
    for header in ("Previous Sessions", "This Session",
                   "Next Session", "Reference"):
        body = handover_render._split_section_body(text, header).strip()
        parts.append(f"## {header}\n{body or '- N/A'}")
    return "\n\n".join(parts)


def _parse_handover_blocks(raw: str) -> tuple[str, str, str]:
    """Pull THIS_SESSION, NEXT_SESSION, REFERENCE bullet blocks out of LLM \
output. Each defaults to empty if its marker is missing."""
    def _slice(open_tag: str, close_tag: str) -> str:
        i = raw.find(open_tag)
        if i < 0:
            return ""
        j = raw.find(close_tag, i + len(open_tag))
        if j < 0:
            return ""
        return raw[i + len(open_tag):j].strip()
    this_s = _slice("===THIS_SESSION===", "===NEXT_SESSION===")
    next_s = _slice("===NEXT_SESSION===", "===REFERENCE===")
    if not next_s:
        next_s = _slice("===NEXT_SESSION===", "===END===")
    ref_s = _slice("===REFERENCE===", "===END===")
    return this_s, next_s, ref_s


def _seg_handover(conn, raw: str, sid: str) -> int:
    """Build full handover.md (skeleton + LLM bullets + ready stamp) in ONE
    atomic write — single-writer rule per Bug #1 fix. SessionStart hooks are
    read-only and never invoke this path.
    """
    this_s, next_s, ref_s = _parse_handover_blocks(raw)
    if not this_s and not next_s and not ref_s:
        return 0
    handover_render.write_handover_full(
        conn, sid, this_s, next_s, reference=ref_s)
    return 1


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

        count = _user_event_count(conn, sid)
        if count <= threshold:
            _write_final_audit(conn, sid, _SUMMARY_SKIP)
            return 0

        events_text, date = _session_events_text(conn, sid)
        if not events_text:
            _write_final_audit(conn, sid, "fail:no_events")
            return 1

        client = LLMClient(cfg=cfg)
        # One sonnet call emits all 4 segment blocks.
        try:
            prior_handover = _load_prior_handover_for_sonnet()
            raw = client.call(
                role="sessionend",
                body=SESSIONEND_PROMPT.format(
                    sid=sid, events=events_text,
                    prior_handover=prior_handover),
                tier="mid",
            )
        except (LLMError, ValueError, RuntimeError) as e:
            _write_final_audit(conn, sid, f"fail:{type(e).__name__}")
            return 1

        writers = (
            ("affect", lambda r: _seg_affect(conn, r, sid, date)),
            ("task_cand", lambda r: _seg_task_cand(conn, r)),
            ("digest", lambda r: _seg_digest(conn, r, sid, date)),
            ("handover", lambda r: _seg_handover(conn, r, sid)),
        )
        failures: list[str] = []
        for name, writer in writers:
            try:
                writer(raw)
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

    except Exception as e:  # noqa: BLE001
        try:
            _write_final_audit(conn, sid, f"fail:{type(e).__name__}")
        except Exception:
            pass
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
