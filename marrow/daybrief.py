"""daybrief.md renderer — the single owner of the between-wake brief.

Pure glue: composes the SAME render functions the SessionStart hook injects
(usage.sessionstart_lines, schedule.render_daily, timeline.render_timeline)
into one zone-marked file, so brief and injection can never diverge.

Zones use stable HTML-comment markers (byte-preserved carry-over for the
hand-written First / Timetrack zones). Retired cortex day_log; single source
now lives here in marrow.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from . import config, schedule, timeline, usage
from ._atomic import atomic_write

# Human-read file: tl reconcile anchors (line + trailing render-state) carry
# no function here — dashboard.py needs them, daybrief does not. Strip only
# in this post-processing step; timeline.py itself is untouched.
# Line anchors: <!-- tl:{sid} -->, <!-- tl:{sid}:{seq} -->,
# <!-- tl:{sid}:{seq}:{idx} -->, <!-- tl:d:{date} -->, <!-- tl:e:{event_id} -->
_TL_LINE_ANCHOR_RE = re.compile(r"[ \t]*<!--\s*tl:[^>]*?-->")
_TL_TRAIL_RE = re.compile(r"\n?<!--\s*tl-rendered:[^>]*?-->")

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


def _remcal_body() -> str:
    """schedule.render_daily body with its '## Daily Schedule' header stripped."""
    content = schedule.render_daily()
    if not content:
        return "### Rem & Cal\n(no schedule data)"
    lines = content.splitlines()
    if lines and lines[0].startswith("## Daily Schedule"):
        lines = lines[1:]
    body = "\n".join(lines).strip("\n")
    return "### Rem & Cal\n" + (body or "(no schedule data)")


def _strip_tl_anchors(content: str) -> str:
    """Remove tl reconcile anchors (line + trailing render-state comment) for
    the human-read daybrief. Line content is unchanged; only the trailing
    HTML-comment markers are dropped, trailing whitespace tidied."""
    content = _TL_TRAIL_RE.sub("", content)
    lines = [_TL_LINE_ANCHOR_RE.sub("", ln).rstrip() for ln in content.splitlines()]
    return "\n".join(lines)


def _timeline_body(conn: sqlite3.Connection) -> str:
    """render_timeline content with its leading '## Timeline' header stripped
    (same treatment as schedule's header) and tl anchors removed — line
    content otherwise identical to the dashboard."""
    content = timeline.render_timeline(conn)
    if not content:
        return "### Timeline\n(no timeline yet)"
    lines = content.splitlines()
    if lines and lines[0].startswith("## Timeline"):
        lines = lines[1:]
    body = _strip_tl_anchors("\n".join(lines)).strip("\n")
    return "### Timeline\n" + (body or "(no timeline yet)")


def render(conn: sqlite3.Connection, existing: str | None = None) -> str:
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
        _remcal_body(),
        REMCAL_END,
        "",
        TIMELINE_START,
        _timeline_body(conn),
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
    return str(config.DATA_DIR / "daybrief.md")


def update(conn: sqlite3.Connection | None = None) -> str:
    """Render + atomic-write daybrief.md. Opens a read-only connection itself
    when none is passed (subprocess / __main__ entry). Returns the out path."""
    own = conn is None
    if own:
        from . import storage
        conn = storage.connect(config.db_path())
    try:
        path = _out_path()
        existing = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            pass
        atomic_write(path, render(conn, existing))
        return path
    finally:
        if own:
            conn.close()


def main() -> int:
    update()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
