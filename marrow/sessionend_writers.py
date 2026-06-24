"""Segment writers for sessionend_async — affect / task_cand / digest.

Each writer takes the raw LLM output for its segment and persists rows.
Lifted out of sessionend_async to keep that module under 300 LOC.

seg_digest parses the new structured DIGEST block (KIND/TL/LIFE/VOICE/FACTS)
from the merged sonnet call and writes kind/tl_line/life_lines columns.
VOICE/FACTS remain in the body text. Parse failure → columns NULL + alert.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path
import re as _re

from . import candidates, config as _config
from .sessionend_prompts import parse_task_rows

_TZ = _config.get_tz()
_CUTOFF_H = 6  # 6AM local day boundary


# ── shared helpers ──────────────────────────────────────────────────────────

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


_TASK_CATEGORIES = (
    "Appointment", "Assignment", "Study", "Project", "Daily", "Others",
)


def _normalise_category(raw: str | None) -> str:
    if not raw:
        return "Others"
    cleaned = raw.strip().title()
    return cleaned if cleaned in _TASK_CATEGORIES else "Others"


# ── segment writers ─────────────────────────────────────────────────────────

def _match_event_hint(conn, hint: str | None, sid: str) -> int | None:
    """Match hint phrase against this session's events rows.

    Strategy: FTS phrase match first (trigram tokenizer, ≥3-char phrases),
    then plain LIKE substring. Returns event id only when exactly one row
    matches — ambiguous (multiple equal-confidence rows) or no match → None.
    """
    if not hint or not hint.strip():
        return None
    phrase = hint.strip()
    # FTS phrase match (trigram — works for CN ≥3 chars and EN)
    try:
        fts_q = '"' + phrase.replace('"', '""') + '"'
        rows = conn.execute(
            "SELECT e.id FROM events_fts f"
            " JOIN events e ON e.id = f.rowid"
            " WHERE events_fts MATCH ?"
            " AND e.session_id = ?",
            (fts_q, sid),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        if len(rows) > 1:
            return None  # ambiguous
    except Exception:
        pass
    # Fallback: plain substring LIKE
    try:
        rows = conn.execute(
            "SELECT id FROM events"
            " WHERE session_id = ?"
            " AND content LIKE ?",
            (sid, f"%{phrase}%"),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
    except Exception:
        pass
    return None


def seg_affect(conn, raw: str, sid: str, date: str) -> int:
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
                f"[sessionend_writers] warn: affect ep={ep} missing"
                f" description, fallback to label={label!r}",
                file=sys.stderr,
            )
        ents = it.get("entities")
        entities = (json.dumps(ents, ensure_ascii=False)
                    if isinstance(ents, list) and ents else None)
        # "open" is the merged-prompt field name; "unresolved" is the legacy name.
        # Either is accepted; open takes precedence when present.
        open_raw = it.get("open", it.get("unresolved", 0))
        try:
            unresolved = 1 if int(open_raw) else 0
        except (TypeError, ValueError):
            unresolved = 0
        reconcile_prev = it.get("reconcile_prev")
        if isinstance(reconcile_prev, str):
            rp = reconcile_prev.strip()
            reconcile_prev = None if not rp or rp.upper() == "N/A" else rp
        else:
            reconcile_prev = None

        reconcile_ref = None
        reconcile_skip_summary = None
        if reconcile_prev:
            prior = conn.execute(
                "SELECT id FROM affect_live"
                " WHERE unresolved=1 AND resolved_at IS NULL"
                " AND date = ?"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (date,),
            ).fetchone()
            if prior:
                reconcile_ref = prior["id"]
            else:
                reconcile_skip_summary = (
                    f"reconcile_prev={reconcile_prev!r} skipped:"
                    " no same-day unresolved row"
                )

        # event_hint link: try hint first, fall back to description
        hint_text = it.get("event_hint") or description
        event_id = _match_event_hint(conn, hint_text, sid)

        with conn:
            if reconcile_skip_summary:
                conn.execute(
                    "INSERT INTO audit_log"
                    " (target_table, action, summary)"
                    " VALUES ('affect', 'reconcile_skip', ?)",
                    (reconcile_skip_summary,),
                )
            cur = conn.execute(
                "INSERT INTO affect (date, ep, valence, arousal, importance,"
                " label, description, entities, source, unresolved,"
                " reconcile_ref, reconcile_prev_text, event_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (date, ep, valence, arousal, importance, label, description,
                 entities, "sessionend_async", unresolved, reconcile_ref,
                 reconcile_prev, event_id),
            )
            affect_id = cur.lastrowid
            if reconcile_ref:
                ts_now = _dt.datetime.now(_dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    "UPDATE affect SET resolved_at=? WHERE id=?",
                    (ts_now, reconcile_ref),
                )
            if event_id is not None:
                conn.execute(
                    "INSERT INTO audit_log"
                    " (target_table, target_id, action, summary)"
                    " VALUES ('affect', ?, 'event_link', ?)",
                    (str(affect_id),
                     f"label={label!r} linked to event_id={event_id}"),
                )
            n += 1
    return n


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seg_task_cand(conn, raw: str) -> int:
    """tasks table. Two row shapes from SEGMENT A:
    - tick row {"id": N, "status": "done"} → flip WHERE id=? (id-based tick;
      a reworded title can't miss).
    - new-task row {"title", "category", "status"} → INSERT + cosine dedup.
    """
    items = parse_task_rows(raw)
    if not items:
        return 0
    n = 0
    _24h_ago = (_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for it in items:
        if not isinstance(it, dict):
            continue

        # ── id-based tick (FIRST) ────────────────────────────────────────────
        tid = it.get("id")
        if tid is not None:
            try:
                tid_int = int(tid)
            except (TypeError, ValueError):
                continue
            if (it.get("status") or "").strip() == "done":
                cur = conn.execute(
                    "SELECT status FROM tasks WHERE id=?", (tid_int,)
                ).fetchone()
                if cur and cur["status"] == "active":
                    with conn:
                        conn.execute(
                            "UPDATE tasks SET status='done', updated_at=?"
                            " WHERE id=?", (_now_utc(), tid_int))
                    n += 1
            continue

        # ── new-task add (no id) → INSERT + cosine dedup ─────────────────────
        title = (it.get("title") or "").strip()
        if not title:
            continue
        status = it.get("status") or "active"
        if status not in ("active", "done", "archived"):
            status = "active"
        category = _normalise_category(it.get("category"))
        due = it.get("due") or None
        note = (it.get("note") or "").strip() or None

        # Skip insert: active exists, archived exists (don't revive), or
        # done within last 24h exists (avoid duplicate near same task).
        active_row = conn.execute(
            "SELECT id FROM tasks WHERE title=? AND status='active' LIMIT 1",
            (title,),
        ).fetchone()
        if active_row:
            if status == "done":
                with conn:
                    conn.execute(
                        "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
                        (_dt.datetime.now(_dt.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
                         active_row["id"]),
                    )
                n += 1
            continue  # active: still active, skip

        blocking = conn.execute(
            "SELECT 1 FROM tasks WHERE title=? AND ("
            "  status='archived'"
            "  OR (status='done' AND updated_at>=?)"
            ") LIMIT 1",
            (title, _24h_ago),
        ).fetchone()
        if blocking:
            continue

        # Cosine dedup vs active titles + 24h-window done titles. Mirrors
        # the string-layer scope: archived intentionally excluded so an
        # old archived task can resurface under a new wording.
        from . import semantic_dedup
        cos_targets = [
            r["title"] for r in conn.execute(
                "SELECT title FROM tasks WHERE status='active' OR"
                " (status='done' AND updated_at>=?)", (_24h_ago,),
            ).fetchall()
        ]
        cos = semantic_dedup.cosine_max(conn, title, cos_targets)
        if cos is None:
            with conn:
                semantic_dedup.warn_embedder_missing(
                    conn, "tasks_dedup_no_embedder",
                    "sessionend_writers.seg_task_cand",
                )
        elif cos >= semantic_dedup.threshold_for("tasks"):
            continue

        with conn:
            conn.execute(
                "INSERT INTO tasks (category, title, due, status, next_step)"
                " VALUES (?, ?, ?, ?, ?)",
                (category, title, due, status, note),
            )
        n += 1
    return n


# Fullwidth colon tolerance: "KIND：" and "KIND:" both match.
_DIGEST_LABEL_RE = _re.compile(
    r"^(?P<label>KIND|TL|LIFE|VOICE|FACTS)[：:][ \t]*(?P<value>.*)$",
    _re.IGNORECASE,
)
_VALID_KINDS = frozenset(("casual", "task"))


def _parse_digest_block(raw: str) -> dict:
    """Parse KIND/TL/LIFE lines from the ===DIGEST===/===END=== block.

    Returns dict with keys: kind (str|None), tl_line (str|None),
    life_lines (str|None — newline-joined, None when N/A or task).

    Fullwidth-colon tolerant (TL： == TL:).
    VOICE/FACTS content is left in the returned 'body' key for body storage.
    """
    # Slice the DIGEST block; fall back to whole raw if fences absent.
    i = raw.find("===DIGEST===")
    if i >= 0:
        tail = raw[i + len("===DIGEST==="):]
        j = tail.find("===END===")
        block = tail[:j].strip() if j >= 0 else tail.strip()
    else:
        # No DIGEST fence — strip any trailing ===END=== before parse.
        block = raw.replace("===END===", "").strip()

    kind: str | None = None
    tl_line: str | None = None
    life_parts: list[str] = []
    life_section = False
    voice_section = False
    facts_section = False
    body_lines: list[str] = []

    for line in block.splitlines():
        m = _DIGEST_LABEL_RE.match(line)
        if m:
            label = m.group("label").upper()
            value = m.group("value").strip()
            life_section = voice_section = facts_section = False
            if label == "KIND":
                cand = value.lower()
                kind = cand if cand in _VALID_KINDS else None
            elif label == "TL":
                tl_line = value if value else None
            elif label == "LIFE":
                life_section = True
                # Inline value on the same line (e.g. "LIFE: N/A")
                if value.upper() == "N/A" or not value:
                    life_parts = []
                else:
                    # Rare: first life item on the label line
                    item = value.lstrip("-").strip()
                    if item:
                        life_parts.append(item)
                body_lines.append(line)
            elif label == "VOICE":
                voice_section = True
                body_lines.append(line)
            elif label == "FACTS":
                facts_section = True
                body_lines.append(line)
        else:
            stripped = line.strip()
            if life_section:
                if stripped and stripped.upper() != "N/A":
                    item = stripped.lstrip("-").strip()
                    if item:
                        life_parts.append(item)
                body_lines.append(line)
            elif voice_section or facts_section:
                body_lines.append(line)
            elif stripped:
                body_lines.append(line)

    life_lines: str | None = None
    if life_parts:
        life_lines = "\n".join(life_parts)

    body = "\n".join(body_lines).strip()
    return {
        "kind": kind,
        "tl_line": tl_line,
        "life_lines": life_lines,
        "body": body,
    }


def _digest_log_dir() -> Path:
    """~/.config/marrow/logs/digest/ — created on first use."""
    from . import config
    d = Path(config.DATA_DIR) / "logs" / "digest"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _digest_local_date(utc_now: _dt.datetime) -> str:
    """UTC datetime → local diary day string (YYYY-MM-DD) with 6AM cutoff."""
    local = utc_now.astimezone(_TZ) - _dt.timedelta(hours=_CUTOFF_H)
    return local.date().isoformat()


def _append_digest_log(sid: str, raw_llm: str) -> None:
    """Append raw haiku digest output to today's digest log file."""
    now = _dt.datetime.now(_dt.timezone.utc)
    day = _digest_local_date(now)
    log_path = _digest_log_dir() / f"digest-{day}.log"
    local_iso = now.astimezone(_TZ).strftime("%Y-%m-%dT%H:%M:%S%z")
    entry = f"[{local_iso} sid={sid[:8]}]\n{raw_llm}\n\n"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(entry)


def _prune_digest_logs() -> None:
    """Delete digest log files older than 2.5 days. Never deletes today or yesterday."""
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        today = _digest_local_date(now)
        yesterday = (_digest_local_date(
            now - _dt.timedelta(days=1)))
        cutoff = now.timestamp() - 2.5 * 24 * 3600
        log_dir = _digest_log_dir()
        for f in log_dir.glob("digest-*.log"):
            # Safety guard: never delete today or yesterday
            name = f.stem  # "digest-YYYY-MM-DD"
            date_part = name[len("digest-"):]
            if date_part in (today, yesterday):
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — prune is best-effort
        pass


def seg_digest(conn, raw: str, sid: str, date: str,
               raw_llm: str | None = None, segment_seq: int = 0) -> int:
    """Persist DIGEST text into session_digests. INSERT OR REPLACE on sid.

    Parses KIND/TL/LIFE from the structured DIGEST block and writes the new
    kind/tl_line/life_lines columns. VOICE/FACTS remain in the body text.
    Parse failure → columns NULL + alert; body always kept.

    raw_llm: full LLM output for quality monitoring log.
    """
    parsed = _parse_digest_block(raw)
    body = parsed["body"]
    if not body:
        # Last-resort: strip fences and use whole raw as body.
        body = raw.replace("===DIGEST===", "").replace("===END===", "").strip()
    if not body:
        return 0

    kind = parsed["kind"]
    tl_line = parsed["tl_line"]
    life_lines = parsed["life_lines"]

    # Alert on parse failures for kind/tl_line (critical structure).
    if kind is None or tl_line is None:
        try:
            from . import config, repo
            repo.add_alert(
                "warn", "sessionend", "digest_parse_partial",
                source="sessionend_writers.py", db=config.db_path(),
                message=(
                    f"digest parse: kind={kind!r} tl_line={tl_line!r}"
                    f" for sid={sid[:8]} — columns set NULL, body kept"
                ),
            )
        except Exception:  # noqa: BLE001 — alert is best-effort
            pass

    ts_now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO session_digests"
            " (sid, segment_seq, date, text, ts, kind, tl_line, life_lines)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, segment_seq, date, body, ts_now, kind, tl_line, life_lines),
        )
    if raw_llm is not None:
        try:
            _append_digest_log(sid, raw_llm)
        except Exception:  # noqa: BLE001 — log is best-effort
            pass
        _prune_digest_logs()
    return 1
