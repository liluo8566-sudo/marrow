"""Backfill session_digests task-kind (FACTS) lines: single midpoint HH:MM
-> true segment HH:MM-HH:MM range.

Usage:
  python scripts/backfill_tl_range.py --dry-run
  python scripts/backfill_tl_range.py --apply

Scope: kind='task' rows only. life_lines for task sessions holds the single
FACTS-derived line written at the segment midpoint (the bug being fixed).
kind='casual' LIFE lines already legitimately mix single HH:MM (per-scene,
substantive-change split) and HH:MM-HH:MM (merged homogeneous scene) by
design — those are NOT touched by this script.

Segment span: true first->last role='user' event timestamp in the segment,
bounded by session_watermarks for multi-segment sids (mirrors the live
write-path query in sessionend_writers._seg_event_span / the render-side
lookup in timeline.py _query_session_event_span). Segments whose span rounds
to the same local minute (single-message segments) are skipped — a single
HH:MM is already correct there, not a bug. Rows whose events are gone
(pruned) are skipped.

--apply backs up the DB to /tmp first (sqlite3 .backup), then writes in a
single transaction. --dry-run only reads (no writes, safe to run anytime).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

# Allow running as a script from repo root or scripts/.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from marrow import config  # noqa: E402

_TZ = config.get_tz()
# Leading HH:MM not already followed by a "-HH:MM" range.
_SINGLE_HHMM_RE = re.compile(r"^(\d{2}:\d{2})(?!-\d{2}:\d{2})")


def _local_hhmm(utc_iso: str) -> str | None:
    """UTC ISO timestamp -> local HH:MM. None on parse error."""
    s = (utc_iso or "").strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_TZ).strftime("%H:%M")


def _segment_bounds(conn: sqlite3.Connection, sid: str,
                    segment_seq: int) -> tuple[int | None, int | None]:
    """(lower_event_id_exclusive, upper_event_id_inclusive) from
    session_watermarks for this segment.

    lower = last_event_id of the watermark row immediately preceding this
        segment_seq (None when this is the earliest/only segment).
    upper = last_event_id of the watermark row recorded FOR this exact
        segment_seq (None when this is the tail/current segment — no upper
        bound, matching the live write-path query which never bounded above).
    """
    rows = conn.execute(
        "SELECT segment_seq, last_event_id FROM session_watermarks"
        " WHERE sid=? ORDER BY segment_seq ASC",
        (sid,),
    ).fetchall()
    lower = None
    upper = None
    for r in rows:
        if r["segment_seq"] < segment_seq:
            lower = r["last_event_id"]
        elif r["segment_seq"] == segment_seq:
            upper = r["last_event_id"]
    return lower, upper


def _event_span(conn: sqlite3.Connection, sid: str, lower: int | None,
                upper: int | None) -> tuple[str | None, str | None]:
    q = ("SELECT MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts"
         " FROM events WHERE session_id=? AND role='user'")
    params: list = [sid]
    if lower is not None:
        q += " AND id > ?"
        params.append(lower)
    if upper is not None:
        q += " AND id <= ?"
        params.append(upper)
    row = conn.execute(q, params).fetchone()
    if row and row["first_ts"] and row["last_ts"]:
        return row["first_ts"], row["last_ts"]
    return None, None


def _rewrite_line(line: str, start_hhmm: str, end_hhmm: str) -> str | None:
    """Replace the leading single HH:MM token with a range.

    None = no change (span rounds to the same local minute — single-message
    segment, not a degenerate range).
    """
    if start_hhmm == end_hhmm:
        return None
    return _SINGLE_HHMM_RE.sub(f"{start_hhmm}-{end_hhmm}", line, count=1)


def _build_plan(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT sid, segment_seq, life_lines FROM session_digests"
        " WHERE kind='task' AND life_lines IS NOT NULL"
        " ORDER BY ts",
    ).fetchall()
    plan: list[dict] = []
    for r in rows:
        sid, seg, life_lines = r["sid"], r["segment_seq"], r["life_lines"]
        if not _SINGLE_HHMM_RE.match(life_lines):
            continue  # already a range, or no leading time token — leave alone
        lower, upper = _segment_bounds(conn, sid, seg)
        first_ts, last_ts = _event_span(conn, sid, lower, upper)
        if not first_ts or not last_ts:
            plan.append({"sid": sid, "segment_seq": seg, "action": "skip:no_events",
                        "old": life_lines, "new": None})
            continue
        start_hhmm = _local_hhmm(first_ts)
        end_hhmm = _local_hhmm(last_ts)
        if start_hhmm is None or end_hhmm is None:
            plan.append({"sid": sid, "segment_seq": seg, "action": "skip:parse_error",
                        "old": life_lines, "new": None})
            continue
        new_line = _rewrite_line(life_lines, start_hhmm, end_hhmm)
        if new_line is None:
            plan.append({"sid": sid, "segment_seq": seg, "action": "skip:same_minute",
                        "old": life_lines, "new": None})
            continue
        plan.append({"sid": sid, "segment_seq": seg, "action": "rewrite",
                    "old": life_lines, "new": new_line})
    return plan


def _print_preview(plan: list[dict]) -> None:
    for p in plan:
        tag = f"[{p['sid'][:8]}#{p['segment_seq']}] {p['action']}"
        if p["action"] == "rewrite":
            print(f"{tag}\n  old: {p['old']!r}\n  new: {p['new']!r}")
        else:
            print(tag)
    counts: dict[str, int] = {}
    for p in plan:
        counts[p["action"]] = counts.get(p["action"], 0) + 1
    touched = counts.get("rewrite", 0)
    print(f"\nSummary: {counts} total={len(plan)} would_touch={touched}")


def _backup_db(db: str) -> str:
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = f"/tmp/marrow_backfill_tl_range_{ts}.db"
    subprocess.run(["sqlite3", db, f".backup {dest}"], check=True)
    return dest


def _apply(conn: sqlite3.Connection, plan: list[dict]) -> None:
    with conn:
        for p in plan:
            if p["action"] != "rewrite":
                continue
            conn.execute(
                "UPDATE session_digests SET life_lines=? WHERE sid=? AND segment_seq=?",
                (p["new"], p["sid"], p["segment_seq"]),
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="preview only — no writes")
    g.add_argument("--apply", action="store_true",
                   help="apply changes (backs up DB to /tmp first)")
    ap.add_argument("--db", default=None,
                    help="db path (default: marrow config)")
    args = ap.parse_args(argv)

    db = args.db or config.db_path()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        plan = _build_plan(conn)
        _print_preview(plan)
        if args.apply:
            backup = _backup_db(db)
            print(f"\nBacked up DB to {backup}")
            _apply(conn, plan)
            print("Applied.")
        else:
            print("\nDry-run — no changes written.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
