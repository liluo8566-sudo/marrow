"""monitor.md renderer — the alert surface.

Renders unresolved alerts (resolved=0) into db-pages/monitor.md with per-line
`<!-- id:alert.N -->` anchors reconcile_alerts expects. One-way DB→md render
plus md-delete=resolve absorb: a line the user deletes resolves the alert
instead of being re-rendered back, so reconcile runs BEFORE render (mirrors
daybrief.update). Path comes from config ([paths].monitor).
"""
from __future__ import annotations

import os
import sqlite3

from . import config, repo
from ._atomic import atomic_write
from .md_index import MdIndex, _hash
from .reconcile import reconcile_alerts

# md_index block id for the alerts block — same `<!-- id:... -->` convention
# daybrief uses so the watcher/md_index can track it.
_ALERTS_BLOCK_ID = "monitor.alerts"

_H1 = "# Monitor"


def render_alerts(conn: sqlite3.Connection) -> str:
    """Alerts block: `## Alerts` H2 + anchored bullets, resolved=0 only.

    The `<!-- alert-block-anchored -->` sentinel and `<!-- id:alert.N -->`
    anchors are exactly what reconcile_alerts locates."""
    rows = conn.execute(
        "SELECT id, severity, message FROM alerts WHERE resolved = 0 "
        "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 "
        "ELSE 2 END, created_at ASC"
    ).fetchall()
    lines = ["## Alerts", "<!-- alert-block-anchored -->"]
    if rows:
        lines.append("<!-- resolve: mw resolve alerts <id> (auto-refreshes) -->")
        lines += [f"- {r[1]}: {r[2]} <!-- id:alert.{r[0]} -->" for r in rows]
    else:
        lines.append("_none_")
    return "\n".join(lines)


def render(conn: sqlite3.Connection) -> str:
    """Full monitor.md body: H1 + the alerts block, id marker stamped."""
    block = f"<!-- id:{_ALERTS_BLOCK_ID} -->\n{render_alerts(conn)}"
    return "\n".join([_H1, "", block, ""])


def _out_path() -> str:
    return str(config.monitor_path())


def update(conn: sqlite3.Connection | None = None) -> str:
    """Reconcile md hand-edits (deleted line = resolve) BEFORE render, then
    atomic-write monitor.md. Opens its own read/write connection when none is
    passed (subprocess / __main__ entry). Returns the out path.

    reconcile_alerts keeps its mtime gate + zero-anchor no-op guard: only rows
    created before the md snapshot can be resolved by a delete, and an empty
    block never mass-resolves unless the anchor sentinel is present."""
    own = conn is None
    if own:
        from . import storage
        conn = storage.connect(config.db_path())
    try:
        from pathlib import Path
        path = _out_path()
        # Reconcile md hand-edits BEFORE render so a deleted line resolves the
        # alert instead of being re-rendered back. Fail-soft: a reconcile error
        # must never block the refresh.
        if os.path.exists(path):
            try:
                reconcile_alerts(conn, Path(path))
            except Exception as e:  # noqa: BLE001
                repo.add_alert(
                    "warn", "monitor", "monitor_reconcile:alerts",
                    source="monitor.py", db=config.db_path(),
                    message=f"alerts reconcile failed: {e}; falling through to render",
                )
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        new = render(conn)
        atomic_write(path, new)
        # Record the block hash AFTER the write so the watcher/md_index has a
        # baseline. The alerts block is reconcile-driven (always overwrite), so
        # the fresh body IS the resolved state.
        MdIndex(conn).record_block(path, _ALERTS_BLOCK_ID, _hash(new))
        return path
    finally:
        if own:
            conn.close()


def main() -> int:
    update()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
