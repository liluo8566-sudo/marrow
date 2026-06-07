"""SessionStart catchup: detect pending sids via lifecycle markers and fire sessionend_async.

CLI: python -m marrow.sessionstart_catchup

Decision source = audit_log lifecycle markers + events table (last 24h).
No jsonl mtime scanning — mtime was always an unreliable signal (idle
thinking / context switch silences the file without killing the session).

For each candidate sid, _classify returns spawn|skip per a 7-state table:
  1. ppid live (start marker ppid in live_cc_ppids) -> skip (active session)
  2. lifecycle:end + ok,user_count=N + events.user_count > N -> spawn (resumed, grew)
  3. lifecycle:end + ok,user_count=N + events.user_count <= N -> skip (done)
  4. lifecycle:end + no ok + (now - end_ts) < 5min -> skip (async still running)
  5. lifecycle:end + no ok + (now - end_ts) >= 5min -> spawn (async died)
  6. no lifecycle:end + start ppid dead -> spawn (endhook didn't fire)
  7. no marker rows at all + sid in 24h events -> spawn (cc died before hooks)

Alert: lifecycle:start >= 30min ago, ppid dead, no lifecycle:end -> silent_death alert.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from . import config, repo, storage
from .paths import paths
from .popen_detach import popen_detach, popen_detach_lazy

_LOGS_DIR = paths.logs_dir

MAX_FIRE = 2
RETRY_LIMIT = 2

_WINDOW_HOURS = 24
_END_GRACE_SECONDS = 300   # 5min grace after lifecycle:end before spawning
_SILENT_DEATH_MIN = 30     # alert if start >= 30min ago, ppid dead, no end


def _parse_ppid_started_at(summary: str) -> tuple[int | None, int | None]:
    """Parse ppid and started_at from summary `ppid=X,source=cc,started_at=Y`."""
    ppid: int | None = None
    started_at: int | None = None
    for part in summary.split(","):
        if part.startswith("ppid="):
            try:
                ppid = int(part[5:])
            except ValueError:
                pass
        elif part.startswith("started_at="):
            try:
                started_at = int(part[11:])
            except ValueError:
                pass
    return ppid, started_at


def _ps_started_at(ppid: int) -> int | None:
    """Return process start epoch via `ps -o lstart= -p <ppid>`, or None.

    LC_ALL=C forces POSIX time format ('Mon May 25 22:07:42 2026') so the
    strptime mask works under any user locale (en_AU prints day-before-month
    by default, which silently broke ppid liveness checks)."""
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
    return None


def _live_cc_ppids(conn) -> set[int]:
    """Return set of ppids whose cc process is confirmed alive.

    Primary signal = `os.kill(pid, 0)` — process exists. started_at is a soft
    secondary signal: when present and the recorded value matches the live
    process's actual start time within 60s tolerance, we have stronger
    confidence the pid was not recycled. When markers were written with a
    fallback started_at (legacy locale bug), the os.kill signal alone is
    still authoritative; we'd rather over-attribute liveness than mark a
    real active session dead and clobber its handover."""
    now = int(time.time())
    cutoff_ts = datetime.fromtimestamp(
        now - _WINDOW_HOURS * 3600, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT DISTINCT summary FROM audit_log"
        " WHERE action='session_lifecycle:start' AND occurred_at >= ?",
        (cutoff_ts,),
    ).fetchall()
    live: set[int] = set()
    for row in rows:
        ppid, _started_at = _parse_ppid_started_at(row["summary"] or "")
        if ppid is None:
            continue
        try:
            os.kill(ppid, 0)
        except OSError:
            continue  # process dead
        live.add(ppid)
    return live


def _list_candidate_sids(conn, window_hours: int = 24) -> list[str]:
    """Union of sids from audit_log lifecycle:start rows and events table within window."""
    now = int(time.time())
    cutoff_ts = datetime.fromtimestamp(
        now - window_hours * 3600, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    sids: set[str] = set()

    # From audit_log lifecycle:start rows.
    rows = conn.execute(
        "SELECT DISTINCT target_id FROM audit_log"
        " WHERE action='session_lifecycle:start' AND occurred_at >= ?"
        " AND target_id IS NOT NULL",
        (cutoff_ts,),
    ).fetchall()
    for row in rows:
        sids.add(row["target_id"])

    # From events table.
    rows = conn.execute(
        "SELECT DISTINCT session_id FROM events"
        " WHERE timestamp >= ? AND session_id IS NOT NULL",
        (cutoff_ts,),
    ).fetchall()
    for row in rows:
        sids.add(row["session_id"])

    return list(sids)


def _ts_to_epoch(ts: str) -> float:
    """Convert ISO timestamp string (audit_log occurred_at) to epoch float."""
    try:
        s = ts.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


_BRIDGE_OWNS_TTL_SECONDS = 12 * 3600


def _bridge_owns_active(conn, sid: str) -> bool:
    """True iff sid's latest manual_skip row is 'bridge_owns', it's younger
    than the TTL, AND no newer sessionend_extract row has appeared. The bridge
    writes the marker on SessionEnd; once it manually fires sessionend_async
    and that run writes an ok/fail/partial row (newer audit_log id), the
    marker is superseded. TTL guards the bridge-crash-forever scenario —
    after 12h with no manual fire, fall through to state 5 spawn so the sid
    isn't orphaned indefinitely."""
    row = conn.execute(
        "SELECT id, summary, occurred_at FROM audit_log"
        " WHERE action='manual_skip' AND target_id=?"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if not row or row["summary"] != "bridge_owns":
        return False
    marker_epoch = _ts_to_epoch(row["occurred_at"])
    if marker_epoch and (time.time() - marker_epoch) > _BRIDGE_OWNS_TTL_SECONDS:
        return False
    newer = conn.execute(
        "SELECT 1 FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=? AND id > ?"
        " LIMIT 1",
        (sid, row["id"]),
    ).fetchone()
    return newer is None


def _classify(conn, sid: str, live_ppids: set[int]) -> Literal["spawn", "skip"]:
    """7-state decision table. Returns 'spawn' or 'skip'.

    # TODO: WeClaude bridge integration — when source=wechat, ppid field carries
    # the wechat bridge process pid. _live_cc_ppids already handles this correctly
    # since it only checks os.kill + started_at; no special-casing needed here.
    # Next step: weclaude bridge writes lifecycle:start/end markers into marrow.db
    # on rotate_session / idle_fire_loop. See Task 5 in wt-lifecycle plan.
    """
    # Precondition: bridge owns sessionend timing for this sid.
    if _bridge_owns_active(conn, sid):
        return "skip"

    now = time.time()

    # Fetch start marker rows for this sid.
    start_rows = conn.execute(
        "SELECT summary, occurred_at FROM audit_log"
        " WHERE action='session_lifecycle:start' AND target_id=?"
        " ORDER BY id DESC",
        (sid,),
    ).fetchall()

    # Fetch end marker for this sid.
    end_row = conn.execute(
        "SELECT occurred_at FROM audit_log"
        " WHERE action='session_lifecycle:end' AND target_id=?"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    # Fetch most recent terminal row for this sid. `skip:short_session[,user_count=N]`
    # counts as terminal: short sessions need no LLM digest, and the embedded
    # user_count lets State 2 detect resume-and-grow without an extra DB hit.
    ok_row = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='sessionend_extract' AND target_id=?"
        " AND (summary='ok' OR summary LIKE 'ok,user_count=%'"
        "      OR summary LIKE 'skip:short_session%')"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()

    # Count current user events.
    user_count = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE session_id=? AND role='user'",
        (sid,),
    ).fetchone()["c"]

    # State 1: ppid is live.
    if start_rows:
        ppid, _ = _parse_ppid_started_at(start_rows[0]["summary"] or "")
        if ppid is not None and ppid in live_ppids:
            return "skip"

    # States 2-5: lifecycle:end exists.
    if end_row:
        if ok_row:
            ok_summary = ok_row["summary"]
            if ok_summary == "ok":
                return "skip"  # legacy plain ok -> fully covered
            try:
                n = int(ok_summary.split("=", 1)[1])
            except (ValueError, IndexError):
                return "skip"
            # State 2: grew past last ok.
            if user_count > n:
                return "spawn"
            # State 3: covered.
            return "skip"
        else:
            # No ok row yet.
            end_epoch = _ts_to_epoch(end_row["occurred_at"])
            elapsed = now - end_epoch
            # State 4: grace period.
            if elapsed < _END_GRACE_SECONDS:
                return "skip"
            # State 5: async died.
            return "spawn"

    # No lifecycle:end.
    # Legacy/cc-killed-hook path: ok row exists but no lifecycle:end. Could be
    # (a) sid processed before lifecycle plan deployment, or (b) cc reaped the
    # hook between archive_events and the end-marker write but sessionend_async
    # still ran. Either way, the ok row is authoritative.
    if ok_row:
        ok_summary = ok_row["summary"]
        if ok_summary == "ok":
            return "skip"  # legacy bare ok, no incremental signal
        try:
            n = int(ok_summary.split("=", 1)[1])
        except (ValueError, IndexError):
            return "skip"
        if user_count > n:
            return "spawn"  # resumed and grew past last ok
        return "skip"

    # State 6: start marker exists + ppid dead.
    if start_rows:
        ppid, _ = _parse_ppid_started_at(start_rows[0]["summary"] or "")
        if ppid is not None and ppid not in live_ppids:
            return "spawn"
        # ppid unknown (couldn't parse) -> spawn to be safe.
        if ppid is None:
            return "spawn"

    # State 7: no marker rows but sid appears in events within window.
    return "spawn"


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    db = config.db_path()
    conn = storage.connect(db)
    now = time.time()

    try:
        live_ppids = _live_cc_ppids(conn)
        candidates = _list_candidate_sids(conn)

        pending: list[str] = []
        for sid in candidates:
            if _classify(conn, sid, live_ppids) == "spawn":
                pending.append(sid)

        # Alert on silent deaths: start >= 30min ago, ppid dead, no lifecycle:end,
        # AND no sessionend_extract row (extract row = sessionend_async actually
        # ran; lifecycle:end marker may have been racekilled by cc SIGKILL but
        # the session is NOT silently dead). Fingerprint is type-level so the
        # whole class collapses to a single dashboard row regardless of how
        # many sids qualify in a given window.
        silent_sids: list[str] = []
        for sid in candidates:
            start_rows = conn.execute(
                "SELECT summary, occurred_at FROM audit_log"
                " WHERE action='session_lifecycle:start' AND target_id=?"
                " ORDER BY id DESC LIMIT 1",
                (sid,),
            ).fetchall()
            if not start_rows:
                continue
            start_epoch = _ts_to_epoch(start_rows[0]["occurred_at"])
            if (now - start_epoch) < _SILENT_DEATH_MIN * 60:
                continue
            ppid, _ = _parse_ppid_started_at(start_rows[0]["summary"] or "")
            if ppid is None or ppid in live_ppids:
                continue
            end_exists = conn.execute(
                "SELECT 1 FROM audit_log"
                " WHERE action='session_lifecycle:end' AND target_id=? LIMIT 1",
                (sid,),
            ).fetchone()
            if end_exists:
                continue
            # sessionend_async wrote an extract row -> session finished work,
            # only the lifecycle:end marker is missing. Not a silent death.
            extract_done = conn.execute(
                "SELECT 1 FROM audit_log"
                " WHERE action='sessionend_extract' AND target_id=? LIMIT 1",
                (sid,),
            ).fetchone()
            if extract_done:
                continue
            # Per-sid audit_log marker prevents the same sid being counted in
            # every catchup pass; the aggregated alert fires only on the
            # first-seen batch.
            already_alerted = conn.execute(
                "SELECT 1 FROM audit_log"
                " WHERE action='alert' AND target_id=?"
                " AND summary LIKE 'silent_death_no_end:sid=%' LIMIT 1",
                (sid,),
            ).fetchone()
            if already_alerted:
                continue
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO audit_log (target_table, target_id, action, summary)"
                        " VALUES ('events', ?, 'alert', ?)",
                        (sid, f"silent_death_no_end:sid={sid}"),
                    )
            except Exception:  # noqa: BLE001
                pass
            silent_sids.append(sid)

        if silent_sids:
            sample = ", ".join(s[:8] for s in silent_sids[:5])
            extra = f" (+{len(silent_sids) - 5} more)" if len(silent_sids) > 5 else ""
            try:
                repo.add_alert(
                    "warn", "silent_death",
                    "silent_death",
                    source="sessionstart_catchup.py", db=db,
                    message=(
                        f"{len(silent_sids)} sid(s) no lifecycle:end + no extract "
                        f"(>= {_SILENT_DEATH_MIN}min, ppid dead): {sample}{extra}"
                    ),
                )
            except Exception:  # noqa: BLE001
                pass

        spawned = 0
        failures: list[str] = []
        for sid in pending[:MAX_FIRE]:
            log_path = _LOGS_DIR / f"sessionend_async_{sid}.log"
            try:
                # _lazy: child redirects its own stdio to log_path on first
                # write, so silent catchup retries leave no file behind.
                popen_detach_lazy(
                    [sys.executable, "-m", "marrow.sessionend_async",
                     "--sid", sid, "--log-path", str(log_path)],
                    log_path=log_path,
                )
                spawned += 1
            except Exception as e:  # noqa: BLE001
                failures.append(f"{sid[:8]}:{type(e).__name__}")

        if failures:
            try:
                repo.add_alert(
                    "warn", "catchup",
                    "catchup_spawn_failed",
                    source="sessionstart_catchup.py", db=db,
                    message=f"catchup spawn failed: {', '.join(failures)}",
                )
            except Exception:  # noqa: BLE001
                pass

        print(
            f"catchup: spawned {spawned} of {len(pending)} pending"
            f" (cap={MAX_FIRE}, window={_WINDOW_HOURS}h)",
            file=sys.stderr,
        )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
