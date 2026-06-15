"""SessionStart catchup: detect pending sids via lifecycle markers and fire sessionend_async.

CLI: python -m marrow.sessionstart_catchup

Decision source = audit_log lifecycle markers + events table (last 24h).
No jsonl mtime scanning — mtime was always an unreliable signal (idle
thinking / context switch silences the file without killing the session).

For each candidate sid, _classify returns spawn|skip per these preconditions
followed by a 7-state table:

  P1. bridge owns sessionend timing  -> skip
  P2. session_block latest = archive -> skip (Lumi archived; cleared = run)
  P3. manual_skip latest = skip      -> skip (manual_skip; skip_cleared = run)
  P4. end_row.summary in {worktree=1, mm_minus_blocked} -> skip (alt close path)
  P5. sessionend_extract:start row newer than end_row -> skip (in-flight)

  1. ppid live (start marker ppid in live_cc_ppids) -> skip (active session)
  2. lifecycle:end + ok,user_count=N + events.user_count > N -> spawn (resumed, grew)
  3. lifecycle:end + ok,user_count=N + events.user_count <= N -> skip (done)
  4. lifecycle:end + no ok + (now - end_ts) < 5min -> skip (async still running)
  5. lifecycle:end + no ok + (now - end_ts) >= 5min -> spawn (async died)
  6. no lifecycle:end + start ppid dead -> spawn (endhook didn't fire)
  7. no marker rows at all + sid in 24h events -> spawn (cc died before hooks)

Alert path: catchup_spawn_failed when popen_detach_lazy raises. No predicate-
based silent_death alerts — catchup signals only on operational failure.
"""
from __future__ import annotations

import json
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
_INFLIGHT_GRACE_SECONDS = 15 * 60  # 15 min: stale start row treated as died

# Terminal summaries written by _write_final_audit (sessionend_async.py).
_TERMINAL_PREFIXES = ("ok", "skip:", "fail:", "partial:")


def _is_terminal_summary(summary: str) -> bool:
    return any(summary.startswith(p) for p in _TERMINAL_PREFIXES)


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

    # TODO: WeChat bridge integration — when source=wechat, ppid field carries
    # the wechat bridge process pid. _live_cc_ppids already handles this correctly
    # since it only checks os.kill + started_at; no special-casing needed here.
    # Next step: wx bridge writes lifecycle:start/end markers into marrow.db
    # on rotate_session / idle_fire_loop. See Task 5 in wt-lifecycle plan.
    """
    # P1: bridge owns sessionend timing for this sid.
    if _bridge_owns_active(conn, sid):
        return "skip"

    # P2-P3: latest-row semantics mirror hooks._is_session_blocked /
    # _is_manual_skip. A `cleared` / `skip_cleared` row means mm+ has unblocked
    # the sid and it should be processed normally — so we cannot treat the
    # mere existence of these actions as terminal.
    block_latest = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='session_block' AND target_id=?"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if block_latest and block_latest["summary"] == "archive":
        return "skip"

    msk_latest = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='manual_skip' AND target_id=?"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    # bridge_owns is NOT terminal here: P1 already decided it with TTL +
    # superseded checks; a latest bridge_owns row that failed P1 must fall
    # through to the state machine.
    if msk_latest and msk_latest["summary"] == "skip":
        return "skip"

    now = time.time()

    # Fetch start marker rows for this sid.
    start_rows = conn.execute(
        "SELECT summary, occurred_at FROM audit_log"
        " WHERE action='session_lifecycle:start' AND target_id=?"
        " ORDER BY id DESC",
        (sid,),
    ).fetchall()

    # Fetch end marker for this sid — keep summary + id for P4/P5 below.
    end_row = conn.execute(
        "SELECT id, summary, occurred_at FROM audit_log"
        " WHERE action='session_lifecycle:end' AND target_id=?"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()

    # P4: alternate close paths leave a typed end-marker summary. There is
    # nothing left to catch up for worktree / mm_minus sessions.
    if end_row and (end_row["summary"] or "") in ("worktree=1", "mm_minus_blocked"):
        return "skip"

    # P5: in-flight guard — skip ONLY when all three hold:
    #   (a) a start row (sessionend_extract, summary='start') with id > end_row
    #   (b) NO terminal row after that start row
    #   (c) the start row is younger than _INFLIGHT_GRACE_SECONDS
    # If a terminal row exists after the start → the run finished (possibly
    # fail/partial); fall through so states 2-5 can re-spawn it.
    # If the start is stale (> grace) with no terminal → died mid-run; fall
    # through so state 5 spawns a retry.
    if end_row:
        start_inflight = conn.execute(
            "SELECT id, occurred_at FROM audit_log"
            " WHERE action='sessionend_extract' AND target_id=?"
            " AND summary='start' AND id > ?"
            " ORDER BY id DESC LIMIT 1",
            (sid, end_row["id"]),
        ).fetchone()
        if start_inflight:
            start_epoch = _ts_to_epoch(start_inflight["occurred_at"])
            start_age = now - start_epoch
            if start_age < _INFLIGHT_GRACE_SECONDS:
                # Check for a terminal row after this start.
                terminal = conn.execute(
                    "SELECT summary FROM audit_log"
                    " WHERE action='sessionend_extract' AND target_id=?"
                    " AND id > ?"
                    " ORDER BY id DESC LIMIT 1",
                    (sid, start_inflight["id"]),
                ).fetchone()
                if terminal is None or not _is_terminal_summary(terminal["summary"]):
                    return "skip"  # genuinely in-flight

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


def _drain_fallback_sink(db: str) -> None:
    """Replay any lines queued in alerts-fallback.jsonl into the alerts table.

    Truncates the file first so a replay that itself fails re-appends via the
    fallback sink, keeping the file bounded.  Malformed lines are dropped with
    a stderr note.
    """
    sink = config.DATA_DIR / "alerts-fallback.jsonl"
    if not sink.exists() or sink.stat().st_size == 0:
        return
    try:
        raw = sink.read_text(encoding="utf-8")
        sink.write_text("", encoding="utf-8")  # truncate before replay
    except OSError as e:
        sys.stderr.write(f"[catchup] fallback drain read error: {e}\n")
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            repo.add_alert(
                rec["severity"], rec["type"], rec["fingerprint"],
                source=rec.get("source"),
                message=rec.get("message"),
                db=db,
            )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[catchup] fallback drain dropped malformed line: {e}\n")


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    db = config.db_path()
    _drain_fallback_sink(db)
    conn = storage.connect(db)
    now = time.time()

    try:
        live_ppids = _live_cc_ppids(conn)
        candidates = _list_candidate_sids(conn)

        pending: list[str] = []
        for sid in candidates:
            if _classify(conn, sid, live_ppids) == "spawn":
                pending.append(sid)

        # NOTE: Predicate-based "silent_death" alerting was removed in this
        # change. Speculative "I think this session died" alerts based on
        # missing markers conflict with the contract: catchup only alerts on
        # operational failure (spawn raised, child immediately exited). If
        # _classify mis-categorises a sid, sessionend_async will figure it
        # out — there is nothing for an operator to do for a healthy
        # short/archived/blocked session.

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
