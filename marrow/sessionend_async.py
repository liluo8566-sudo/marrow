"""SessionEnd async LLM extraction: 7 sonnet segments + DB writes.

CLI: python -m marrow.sessionend_async --sid <session_id>

Per pipeline §2.3: AFFECT / ENTITY_CAND / TASK_CAND / MILESTONE_CAND /
VOCAB_CAND / DIGEST / HANDOVER. Each segment is independent — failure of
one does not block others; overall audit summary reports ok / partial / fail.

Skip rule: sessions with ≤ skip_turn_threshold user turns extract nothing.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, storage
from .llm import LLMClient, LLMError
from .sessionend_prompts import (
    AFFECT_PROMPT, ENTITY_CAND_PROMPT, TASK_CAND_PROMPT,
    MILESTONE_CAND_PROMPT, VOCAB_CAND_PROMPT, DIGEST_PROMPT,
    HANDOVER_PROMPT, fence,
)

_LOGS_DIR = Path.home() / ".config" / "marrow" / "logs"
_TZ = ZoneInfo("Australia/Melbourne")
_CUTOFF_H = 6  # 6AM day boundary (per pipeline §6)

_SUMMARY_OK = "ok"
_SUMMARY_SKIP = "skip:short_session"

_SEGMENTS = (
    "affect", "entity_cand", "task_cand",
    "milestone_cand", "vocab_cand", "digest", "handover",
)

_ENTITY_KINDS = {"person", "pref", "place"}
_MILESTONE_SCOPES = {"me", "us"}


# ── parsing helpers ─────────────────────────────────────────────────────────

def _extract_block(text: str, marker: str) -> list | None:
    """Pull JSON list between ===<marker>=== and ===END===. None on miss."""
    open_tag = f"==={marker}==="
    i = text.find(open_tag)
    if i == -1:
        return None
    tail = text[i + len(open_tag):]
    j = tail.find("===END===")
    body = tail[:j].strip() if j != -1 else tail.strip()
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


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
    items = _extract_block(raw, "AFFECT")
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
            cur = conn.execute(
                "INSERT INTO affect (date, ep, valence, arousal, importance,"
                " label, entities, source, unresolved, reconcile_ref,"
                " reconcile_prev_text)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (date, ep, valence, arousal, importance, label, entities,
                 "sessionend_async", unresolved, reconcile_ref,
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
            _ = cur  # suppress unused
    return n


def _seg_entity_cand(conn, raw: str) -> int:
    items = _extract_block(raw, "ENTITY_CAND")
    if not items:
        return 0
    n = 0
    seen: set[tuple[str, str]] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            conf = float(it.get("conf", 0))
        except (TypeError, ValueError):
            conf = 0
        if conf < 0.8:
            continue
        kind = (it.get("kind") or "").strip()
        name = (it.get("name") or "").strip()
        if kind not in _ENTITY_KINDS or not name:
            continue
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)
        exists = conn.execute(
            "SELECT 1 FROM entities WHERE kind=? AND name=?"
            " AND superseded_by IS NULL LIMIT 1", (kind, name),
        ).fetchone()
        if exists:
            continue
        fact = (it.get("note") or "").strip() or None
        with conn:
            conn.execute(
                "INSERT INTO entities (kind, name, fact, source)"
                " VALUES (?, ?, ?, ?)",
                (kind, name, fact, "sessionend_async"),
            )
        n += 1
    return n


def _seg_task_cand(conn, raw: str) -> int:
    """tasks table: id/category/title/due/status/next_step/...
    Map extracted fields to tasks schema (category='task' default).
    """
    items = _extract_block(raw, "TASK_CAND")
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
        due = it.get("due") or None
        note = (it.get("note") or "").strip() or None
        # Dedup on (title, status='active')
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
                ("task", title, due, status, note),
            )
        n += 1
    return n


def _seg_milestone_cand(conn, raw: str, date: str) -> int:
    items = _extract_block(raw, "MILESTONE_CAND")
    if not items:
        return 0
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            conf = float(it.get("conf", 0))
        except (TypeError, ValueError):
            conf = 0
        if conf < 0.85:
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        scope = it.get("scope") or "me"
        if scope not in _MILESTONE_SCOPES:
            scope = "me"
        m_date = it.get("date") or date
        desc = (it.get("description") or "").strip() or None
        with conn:
            conn.execute(
                "INSERT INTO milestones (scope, date, title, description, source)"
                " VALUES (?, ?, ?, ?, ?)",
                (scope, m_date, title, desc, "sessionend_async"),
            )
        n += 1
    return n


def _seg_vocab_cand(conn, raw: str) -> int:
    items = _extract_block(raw, "VOCAB_CAND")
    if not items:
        return 0
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            conf = float(it.get("conf", 0))
        except (TypeError, ValueError):
            conf = 0
        if conf < 0.7:
            continue
        key = (it.get("key") or "").strip()
        if not key:
            continue
        vtype = it.get("type") or "phrase"
        value = (it.get("value") or "").strip() or None
        context = (it.get("context") or "").strip() or None
        # Existing key + same type -> bump use_count
        row = conn.execute(
            "SELECT id, use_count FROM vocab WHERE type=? AND key=? LIMIT 1",
            (vtype, key),
        ).fetchone()
        ts_now = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        with conn:
            if row:
                conn.execute(
                    "UPDATE vocab SET use_count=use_count+1, last_seen=?"
                    " WHERE id=?", (ts_now, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO vocab (type, key, value, context,"
                    " use_count, last_seen, source_hash)"
                    " VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (vtype, key, value, context, ts_now, "sessionend_async"),
                )
        n += 1
    return n


def _seg_digest(conn, raw: str, sid: str, date: str) -> int:
    """Persist digest text into session_digests (INSERT OR REPLACE on sid)."""
    body = raw.strip()
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


_HANDOVER_PATH = Path.home() / ".config" / "marrow" / "handover.md"


def _parse_handover_blocks(raw: str) -> tuple[str, str]:
    """Pull THIS_SESSION and NEXT_SESSION bullet blocks out of LLM output."""
    def _slice(open_tag: str, close_tag: str) -> str:
        i = raw.find(open_tag)
        if i < 0:
            return ""
        j = raw.find(close_tag, i + len(open_tag))
        if j < 0:
            return ""
        return raw[i + len(open_tag):j].strip()
    this_s = _slice("===THIS_SESSION===", "===NEXT_SESSION===")
    next_s = _slice("===NEXT_SESSION===", "===END===")
    return this_s, next_s


def _inject_section(text: str, header: str, body: str) -> str:
    """Replace content under `## <header>` up to the next `## ` or HTML
    comment with body. Stops before `<!-- ...` to preserve handover stamps.
    """
    pat = re.compile(
        rf"(^## {re.escape(header)}[ \t]*\n)(.*?)(?=^## |^<!--|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    return pat.sub(lambda m: f"{m.group(1)}{body}\n\n", text, count=1)


def _seg_handover(raw: str, sid: str) -> int:
    """Inject LLM bullets into `## This Session` / `## Next Session` of
    ~/.config/marrow/handover.md. Also stamps the pending handover slot
    as ready so SessionStart hook can detect sid alignment.
    """
    this_s, next_s = _parse_handover_blocks(raw)
    if not this_s and not next_s:
        return 0
    if not _HANDOVER_PATH.exists():
        return 0
    text = _HANDOVER_PATH.read_text(encoding="utf-8")
    if this_s:
        text = _inject_section(text, "This Session", this_s)
    if next_s:
        text = _inject_section(text, "Next Session", next_s)
    # Stamp pending → ready (race-avoidance per DECISIONS:46)
    pending_re = re.compile(r"<!--\s*handover:\s*pending\s+sid:(\S+)\s*-->")
    m = pending_re.search(text)
    ts_unix = int(time.time())
    if m:
        cur_sid = m.group(1)
        lag = ("" if cur_sid == sid
               else f" (handover sid={sid}, skeleton sid={cur_sid})")
        text = pending_re.sub(
            f"<!-- handover: ready sid:{sid} ts:{ts_unix} -->{lag}",
            text, count=1,
        )
    else:
        text = text.rstrip() + (
            f"\n<!-- handover: ready sid:{sid} ts:{ts_unix} -->\n"
        )
    tmp = _HANDOVER_PATH.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(_HANDOVER_PATH)
    return 1


# ── main loop ───────────────────────────────────────────────────────────────

def _run_segment(client, prompt_tpl, sid, events_text, tier="mid"):
    body = prompt_tpl.format(sid=sid, events=events_text)
    return client.call(role=f"sessionend_{prompt_tpl[:20]}",
                       body=body, tier=tier)


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
        failures: list[str] = []
        seg_specs = (
            ("affect", AFFECT_PROMPT,
             lambda r: _seg_affect(conn, r, sid, date)),
            ("entity_cand", ENTITY_CAND_PROMPT,
             lambda r: _seg_entity_cand(conn, r)),
            ("task_cand", TASK_CAND_PROMPT,
             lambda r: _seg_task_cand(conn, r)),
            ("milestone_cand", MILESTONE_CAND_PROMPT,
             lambda r: _seg_milestone_cand(conn, r, date)),
            ("vocab_cand", VOCAB_CAND_PROMPT,
             lambda r: _seg_vocab_cand(conn, r)),
            ("digest", DIGEST_PROMPT,
             lambda r: _seg_digest(conn, r, sid, date)),
            ("handover", HANDOVER_PROMPT,
             lambda r: _seg_handover(r, sid)),
        )

        for name, tpl, writer in seg_specs:
            try:
                raw = client.call(
                    role=f"sessionend_{name}",
                    body=tpl.format(sid=sid, events=events_text),
                    tier="mid",
                )
                writer(raw)
                _write_segment_audit(conn, sid, name, "ok")
            except (LLMError, ValueError, RuntimeError) as e:
                failures.append(name)
                try:
                    _write_segment_audit(
                        conn, sid, name, f"fail:{type(e).__name__}")
                except Exception:
                    pass

        if not failures:
            _write_final_audit(conn, sid, _SUMMARY_OK)
            return 0
        if len(failures) == len(seg_specs):
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
