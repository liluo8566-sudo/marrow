"""Segment writers for sessionend_async — affect / task_cand / digest / handover.

Each writer takes the raw LLM output for its segment and persists rows /
re-renders handover.md. Lifted out of sessionend_async to keep that module
under 300 LOC.
"""
from __future__ import annotations

import datetime as _dt
import errno
import fcntl
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from . import candidates, handover_render
from .sessionend_prompts import parse_handover_output

# Repo root PROGRESS.md (one level up from the package dir).
_PROGRESS_DEFAULT = Path(__file__).resolve().parents[1] / "PROGRESS.md"
_PROGRESS_LOCK_RETRIES = 3
_PROGRESS_LOCK_BACKOFF = 0.05


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


def seg_task_cand(conn, raw: str) -> int:
    """tasks table: dedup on title across active/done/archived; category from \
LLM (whitelist fallback Others)."""
    items = candidates.extract_block(raw, "TASK_CAND")
    if not items:
        return 0
    n = 0
    _24h_ago = (_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
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

        with conn:
            conn.execute(
                "INSERT INTO tasks (category, title, due, status, next_step)"
                " VALUES (?, ?, ?, ?, ?)",
                (category, title, due, status, note),
            )
        n += 1
    return n


def seg_digest(conn, raw: str, sid: str, date: str) -> int:
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


def seg_handover(conn, raw: str, sid: str) -> int:
    """Build full handover.md (skeleton + 4 state-axis sections + ready stamp)
    in ONE atomic write. Single-writer rule (Bug #1) — SessionStart hooks are
    read-only and never invoke this path.

    Tombstone-aware: bullets the user removed from prior handover.md since
    the last auto-write are tombstoned in handover_render.write_handover_full
    so sonnet's re-emission cannot revive them."""
    done, open_, plan, reference = parse_handover_output(raw)
    if not (done or open_ or plan or reference):
        return 0
    handover_render.write_handover_full(
        conn, sid, done=done, open_=open_, plan=plan, reference=reference)
    return 1


# ── PROGRESS.md append (per-session DONE block) ─────────────────────────────

def _progress_is_empty(done_block: str) -> bool:
    """Skip markers — empty, whitespace-only, `(none)`, or `- N/A`."""
    body = (done_block or "").strip()
    if not body:
        return True
    flat = " ".join(line.strip("- \t") for line in body.splitlines()).strip()
    return flat.lower() in ("", "none", "(none)", "n/a")


def _acquire_progress_lock(path: Path):
    """LOCK_EX with 3x 50ms backoff — same pattern as handover_render."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    for attempt in range(_PROGRESS_LOCK_RETRIES):
        fd = open(path, "r+", encoding="utf-8")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError) as e:
            fd.close()
            if isinstance(e, OSError) and not isinstance(e, BlockingIOError):
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    return None
            if attempt == _PROGRESS_LOCK_RETRIES - 1:
                return None
            time.sleep(_PROGRESS_LOCK_BACKOFF)
    return None


def append_progress(done_block: str, sid: str, date: str, *,
                    progress_path: Path | None = None) -> int:
    """Append this session's raw DONE block to PROGRESS.md (atomic + flock).
    Skip on empty / (none) / N/A. Degrade-open on failure (alert + raise)."""
    if _progress_is_empty(done_block):
        return 0
    path = Path(progress_path) if progress_path else _PROGRESS_DEFAULT
    short_sid = (sid or "")[:8] or "unknown"
    block = f"\n[{date} sid:{short_sid}]\n{done_block.strip()}\n"
    fd = _acquire_progress_lock(path)
    if fd is None:
        print(f"[sessionend_writers] alert: PROGRESS.md lock-failed sid={sid}",
              file=sys.stderr)
        raise RuntimeError(f"progress_lock_failed sid={sid}")
    try:
        try:
            prior = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            prior = ""
        new_text = prior + block if prior.endswith("\n") or not prior \
            else prior + "\n" + block
        d = str(path.parent) or "."
        tfd, tmp = tempfile.mkstemp(dir=d, prefix=".progress.")
        try:
            with os.fdopen(tfd, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp, str(path))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception as e:
        print(f"[sessionend_writers] alert: PROGRESS.md write-failed sid={sid}"
              f" err={type(e).__name__}", file=sys.stderr)
        raise
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()
    return 1
