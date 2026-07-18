"""Write the timeline backdrop to the dashboard md (fork-local feature).

Upstream retired the full dashboard render chain (P2, 2026-07). This thin
writer keeps the Obsidian-visible timeline: render_timeline() output is
written to [paths].dashboard on each refresh tick. The file is a pure
render product — fully regenerated each time, never user-edited, no
md_index reconcile.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import tempfile
from pathlib import Path

from . import config, timeline


def update(conn: sqlite3.Connection) -> None:
    """Render timeline into the configured dashboard file. No-op when
    [paths].dashboard is empty or the DB renders to nothing."""
    raw = (config.load().get("paths", {}).get("dashboard") or "").strip()
    if not raw:
        return
    body = timeline.render_timeline(conn)
    if not body:
        return
    stamp = _dt.datetime.now(config.get_tz()).strftime("%Y-%m-%d %H:%M")
    md = f"# Dashboard\n\n> refreshed {stamp}\n\n{body}\n"
    path = Path(raw).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(md)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
