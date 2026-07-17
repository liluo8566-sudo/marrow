"""daybrief.md renderer — the single owner of the between-wake brief.

Pure glue: composes the SAME render functions the SessionStart hook injects
(usage.sessionstart_lines, schedule.render_daily, timeline.render_timeline)
into one zone-marked file, so brief and injection can never diverge.

Zones use stable HTML-comment markers (byte-preserved carry-over for the
hand-written First / Timetrack zones). Retired cortex day_log; single source
now lives here in marrow.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from . import config, repo, schedule, timeline, usage
from ._atomic import atomic_write
from .md_index import MdIndex, _hash
from .reconcile import emit_conflict_alerts, reconcile_timeline

# Timeline zone id marker — lets md_index/watcher track the block by the same
# `<!-- id:... -->` convention the dashboard uses. Stamped on the H2 line so
# reconcile_timeline still locates `## Timeline` and Obsidian hides the comment.
_TIMELINE_BLOCK_ID = "daybrief.timeline"

STATUS_START = "<!-- marrow:status:start -->"
STATUS_END = "<!-- marrow:status:end -->"
REMCAL_START = "<!-- marrow:remcal:start -->"
REMCAL_END = "<!-- marrow:remcal:end -->"
TIMELINE_START = "<!-- marrow:timeline:start -->"
TIMELINE_END = "<!-- marrow:timeline:end -->"
FIRST_START = "<!-- marrow:first:start -->"
FIRST_END = "<!-- marrow:first:end -->"
TIMETRACK_START = "<!-- marrow:timetrack:start -->"
TIMETRACK_END = "<!-- marrow:timetrack:end -->"

_FIRST_PLACEHOLDER = (
    "### First\n<!-- placeholder: hand-written 3-5 lines (deferred) -->")
_TIMETRACK_PLACEHOLDER = (
    "#### Timetrack\n<!-- placeholder: category buckets + sleep (deferred) -->")


def _extract_bounded(existing: str | None, start: str, end: str, default: str) -> str:
    """Verbatim body between markers, or `default` when absent/corrupted."""
    if not existing:
        return default
    s = existing.find(start)
    e = existing.find(end)
    if s == -1 or e == -1 or e < s:
        return default
    body = existing[s + len(start): e].strip("\n")
    return body or default


def _status_body() -> str:
    lines = usage.sessionstart_lines()
    body = "\n".join(lines) if lines else "(no usage data)"
    return "### Status\n" + body


def _remcal_body(existing: str | None) -> str:
    """schedule.render_daily body with its '## Daily Schedule' header stripped.
    Empty render (cadence subprocess failed) keeps the previous zone instead of
    blanking the page over a transient failure."""
    content = schedule.render_daily()
    if not content:
        return _extract_bounded(existing, REMCAL_START, REMCAL_END,
                                "### Rem & Cal\n(no schedule data)")
    lines = content.splitlines()
    if lines and lines[0].startswith("## Daily Schedule"):
        lines = lines[1:]
    body = "\n".join(lines).strip("\n")
    return "### Rem & Cal\n" + (body or "(no schedule data)")


def _stamp_block_id(body: str) -> str:
    """Prepend the id marker on its own line above the `## Timeline` heading so
    md_index/watcher track the block; idempotent."""
    marker = f"<!-- id:{_TIMELINE_BLOCK_ID} -->"
    if marker in body:
        return body
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            lines.insert(i, marker)
            return "\n".join(lines)
    return f"{marker}\n{body}"


def _extract_timeline_zone(existing: str | None) -> str | None:
    """Old timeline zone body between the marker pair, for carry_trail_t."""
    if not existing:
        return None
    s = existing.find(TIMELINE_START)
    e = existing.find(TIMELINE_END)
    if s == -1 or e == -1 or e < s:
        return None
    return existing[s + len(TIMELINE_START): e].strip("\n")


def _timeline_body(conn: sqlite3.Connection, existing: str | None,
                   absorbed: bool = False) -> str:
    """render_timeline output verbatim — H2 header, line anchors and trail all
    kept — with the id marker stamped and the render timestamp carried over an
    unchanged block (carry_trail_t). Identical to the dashboard timeline zone.

    absorbed=True (reconcile just wrote an edit into the DB) forces a fresh t=
    so the per-row db-win gate does not deadlock on the next reconcile."""
    content = timeline.render_timeline(conn)
    if not content:
        return "### Timeline\n(no timeline yet)"
    body = _stamp_block_id(content)
    return timeline.carry_trail_t(body, _extract_timeline_zone(existing), absorbed)


def render(conn: sqlite3.Connection, existing: str | None = None,
           absorbed: bool = False) -> str:
    now = datetime.now(timezone.utc).astimezone(config.get_tz())
    date = now.strftime("%Y-%m-%d")
    first_body = _extract_bounded(existing, FIRST_START, FIRST_END, _FIRST_PLACEHOLDER)
    track_body = _extract_bounded(
        existing, TIMETRACK_START, TIMETRACK_END, _TIMETRACK_PLACEHOLDER)
    parts = [
        date,
        "",
        STATUS_START,
        _status_body(),
        STATUS_END,
        "",
        REMCAL_START,
        _remcal_body(existing),
        REMCAL_END,
        "",
        TIMELINE_START,
        _timeline_body(conn, existing, absorbed),
        TIMELINE_END,
        "",
        FIRST_START,
        first_body,
        FIRST_END,
        "",
        TIMETRACK_START,
        track_body,
        TIMETRACK_END,
        "",
    ]
    return "\n".join(parts)


def _out_path() -> str:
    return str(config.daybrief_path())


def update(conn: sqlite3.Connection | None = None) -> str:
    """Render + atomic-write daybrief.md. Opens a read-only connection itself
    when none is passed (subprocess / __main__ entry). Returns the out path."""
    own = conn is None
    if own:
        from . import storage
        conn = storage.connect(config.db_path())
    try:
        from pathlib import Path
        path = _out_path()
        db = config.db_path()
        # Reconcile md hand-edits BEFORE render so timeline edits flow to DB.
        # Fail-soft: a reconcile error must never block the refresh.
        absorbed = False
        if os.path.exists(path):
            try:
                _rpt = reconcile_timeline(conn, Path(path), db=db)
                absorbed = _rpt.any_change()
                emit_conflict_alerts(_rpt, "daybrief:timeline", db=db)
            except Exception as e:
                repo.add_alert(
                    "warn", "daybrief", "daybrief_reconcile:timeline",
                    source="daybrief.py", db=db,
                    message=f"timeline reconcile failed: {e}; falling through to render",
                )
        existing = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            pass
        new = render(conn, existing, absorbed)
        atomic_write(path, new)
        # Record the timeline block hash AFTER the write so the watcher's
        # sync_file_observe has a baseline. Timeline is reconcile-driven
        # (always overwrite), so the fresh body IS the resolved state.
        zone = _extract_timeline_zone(new)
        if zone is not None:
            MdIndex(conn).record_block(path, _TIMELINE_BLOCK_ID, _hash(zone))
        return path
    finally:
        if own:
            conn.close()


def main() -> int:
    update()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
