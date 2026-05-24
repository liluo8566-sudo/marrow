"""SessionStart catchup: detect pending sids and fire sessionend_async.

CLI: python -m marrow.sessionstart_catchup

Single data source = cc-side jsonls under ~/.claude/projects/*/*.jsonl.
A jsonl is `pending` iff:
  - mtime within [now-WINDOW_HOURS, now-IDLE_SECONDS]
    (older = stale; newer = session likely still alive, skip this turn)
  - is_headless == False (real manual session, not a worker spawn)
  - audit_log has NO ok / skip:short_session row for the sid AND fewer
    than RETRY_LIMIT fail/partial rows (catchup gives one retry; second
    failure raises a critical alert via sessionend_async itself).

For each pending sid we archive_events on the fly (covers the case where
hooks.session_end never ran because cc dropped SessionEnd), then spawn
sessionend_async. Newest-mtime first; per-run spawn cap = MAX_FIRE; rest
rolls over to next SessionStart.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from . import config, repo, storage, transcript
from .popen_detach import popen_detach

_LOGS_DIR = Path.home() / ".config" / "marrow" / "logs"
_CC_PROJECTS = Path.home() / ".claude" / "projects"

WINDOW_HOURS = 24
IDLE_SECONDS = 600  # jsonl untouched ≥10min → treat as closed; alive sessions still flushing skip this turn
MAX_FIRE = 2
RETRY_LIMIT = 2  # max fail/partial extractions before catchup gives up


def _should_skip(conn, sid: str) -> bool:
    """Skip iff already succeeded (ok / skip:short_session) or already
    hit RETRY_LIMIT failures. Otherwise eligible for catchup retry."""
    row = conn.execute(
        "SELECT"
        " SUM(CASE WHEN summary IN ('ok','skip:short_session') THEN 1 ELSE 0 END) AS done,"
        " SUM(CASE WHEN summary LIKE 'fail:%' OR summary LIKE 'partial:%' THEN 1 ELSE 0 END) AS fails"
        " FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=?",
        (sid,),
    ).fetchone()
    if not row:
        return False
    done = row["done"] or 0
    fails = row["fails"] or 0
    return done > 0 or fails >= RETRY_LIMIT


def _jsonl_orphans(conn) -> list[str]:
    """Real manual jsonls touched in [now-WINDOW_HOURS, now-IDLE_SECONDS] with
    no sessionend_extract audit. Archive on the fly. Ordered newest-mtime first
    so the most recently closed session wins the MAX_FIRE cap."""
    now = time.time()
    floor = now - WINDOW_HOURS * 3600
    ceil = now - IDLE_SECONDS
    candidates: list[tuple[float, str, Path]] = []
    if not _CC_PROJECTS.exists():
        return []
    for jsonl in _CC_PROJECTS.glob("*/*.jsonl"):
        try:
            m = jsonl.stat().st_mtime
        except OSError:
            continue
        if m < floor or m > ceil:
            continue
        try:
            if transcript.is_headless(str(jsonl)):
                continue
        except OSError:
            continue
        sid = jsonl.stem
        if _should_skip(conn, sid):
            continue
        candidates.append((m, sid, jsonl))
    candidates.sort(key=lambda t: t[0], reverse=True)
    out: list[str] = []
    for _, sid, jsonl in candidates:
        try:
            rows = transcript.clean(str(jsonl))
        except OSError:
            continue
        if not rows:
            continue
        already = conn.execute(
            "SELECT 1 FROM events WHERE session_id=? LIMIT 1", (sid,),
        ).fetchone()
        if not already:
            try:
                repo.archive_events(conn, rows)
            except Exception:  # noqa: BLE001 — never break catchup
                continue
        out.append(sid)
    return out


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    db = config.db_path()
    conn = storage.connect(db)
    try:
        pending = _jsonl_orphans(conn)
    finally:
        conn.close()

    spawned = 0
    failures: list[str] = []
    for sid in pending[:MAX_FIRE]:
        log_path = _LOGS_DIR / f"sessionend_async_{sid}.log"
        try:
            popen_detach(
                [sys.executable, "-m", "marrow.sessionend_async", "--sid", sid],
                log_path=log_path,
            )
            spawned += 1
        except Exception as e:  # noqa: BLE001
            failures.append(f"{sid[:8]}:{type(e).__name__}")

    if failures:
        try:
            repo.add_alert(
                "warn", "catchup",
                f"catchup spawn failed: {', '.join(failures)}",
                source="sessionstart_catchup.py", db=db,
            )
        except Exception:  # noqa: BLE001
            pass

    print(
        f"catchup: spawned {spawned} of {len(pending)} pending workers"
        f" (cap={MAX_FIRE}, window={WINDOW_HOURS}h, idle={IDLE_SECONDS}s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
