"""Dashboard top inserter — md=SoT, block-level upsert.

Phase 3 reversal (2026-05-25): each top section carries a stable
`<!-- id:dashboard.<key> -->` marker; per-block content_hash lives in
md_index. write_dashboard now skips blocks the user has edited (hash
diverges from md_index baseline) and skips blocks the watcher has
tombstoned (user deleted the whole block). Free-form edits inside the
top region are preserved, not overwritten.

Reconcile passes (milestone candidates + tasks) still run BEFORE render
— they absorb user ticks / votes / deletions into the DB, then the
inserter writes the resolved DB state back to the two reconciled
blocks (always overwrite). Pure-display blocks (alerts / affect /
content) honour hash-skip so any hand-edit survives.

Section renderers + the canonical block-id list live in top_sections.py.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from . import repo, top_sections
from ._atomic import atomic_write as _atomic_write
from .md_index import MdIndex
from .reconcile import (reconcile_affect, reconcile_alerts,
                        reconcile_milestone_candidates,
                        reconcile_tasks, reconcile_timeline)

M0 = "<!-- marrow:top:start -->"
M1 = "<!-- marrow:top:end -->"


def render_top(conn, *, dashboard_path: str | None = None) -> str:
    return (M0 + "\n"
            + top_sections.render_top(conn, dashboard_path=dashboard_path)
            + "\n" + M1)


def _split(text: str) -> tuple[str, str, str]:
    # (before, block_incl_markers, after); block "" if markers absent.
    i = text.find(M0)
    j = text.find(M1)
    if i == -1 or j == -1 or j < i:
        return text, "", ""
    return text[:i], text[i:j + len(M1)], text[j + len(M1):]


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _current_top_text(existing: str) -> str:
    """Return the text BETWEEN the marker pair (exclusive), or '' if missing."""
    i = existing.find(M0)
    j = existing.find(M1)
    if i == -1 or j == -1 or j < i:
        return ""
    return existing[i + len(M0):j]


# Canonical block id marker — scoped to `dashboard.<key>` so per-row anchors
# like `<!-- id:1 -->` (task rows) don't act as block boundaries.
_BLOCK_MARKER_RE = re.compile(
    r"<!--\s*id:(dashboard\.[A-Za-z0-9_]+)\s*-->"
)


def _parse_top_blocks(top_text: str) -> dict[str, str]:
    """Split the top region into canonical `dashboard.<key>` blocks.

    Returns {block_id: body}. body runs from the marker line to the line
    before the next canonical marker (or EOT). Per-row `<!-- id:N -->`
    anchors inside the body are ignored as boundaries.
    """
    lines = top_text.splitlines(keepends=True)
    starts: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        m = _BLOCK_MARKER_RE.search(ln)
        if m:
            starts.append((i, m.group(1)))
    out: dict[str, str] = {}
    for idx, (s, bid) in enumerate(starts):
        e = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        body = "".join(lines[s:e]).rstrip("\n")
        out[bid] = body
    return out


def _assemble_top_region(bodies: list[str]) -> str:
    inner = "\n\n".join(bodies)
    return f"{M0}\n{inner}\n{M1}"


def _resolve_blocks(path: str, conn, fresh: list[tuple[str, str]],
                    current_top: str) -> tuple[list[str], list[tuple[str, str]]]:
    """For each canonical fresh block decide: skip / preserve user edit / overwrite.

    Returns (bodies, pending_records) — bodies splice back into the top
    region in canonical order; pending_records is the list of
    (block_id, content_hash) tuples the caller must record_block AFTER the
    atomic write succeeds. Recording before the write would leave md_index
    pointing at content that never reached disk on a write failure (SIGTERM
    mid-write / ENOSPC / EACCES) — permanent hash desync.
    """
    store = MdIndex(conn)
    current = _parse_top_blocks(current_top)
    out: list[str] = []
    pending: list[tuple[str, str]] = []
    for bid, fresh_body in fresh:
        if store.is_tombstoned(path, bid):
            # User deleted this block — watcher tombstoned it; do not re-emit.
            continue
        cur_body = current.get(bid)
        # Reconciled blocks: reconcile_* already absorbed any user edit into
        # the DB, so the fresh body IS the resolved state. Always overwrite.
        if bid in top_sections.RECONCILED_BLOCK_IDS:
            out.append(fresh_body)
            pending.append((bid, _hash(fresh_body)))
            continue
        if cur_body is None:
            # First render or user wiped this block but watcher hasn't
            # tombstoned yet — re-emit canonical fresh.
            out.append(fresh_body)
            pending.append((bid, _hash(fresh_body)))
            continue
        cur_hash = _hash(cur_body)
        stored = store.get_hash(path, bid)
        if stored is None or stored == cur_hash:
            # No user edit since last auto-write — safe to overwrite.
            out.append(fresh_body)
            pending.append((bid, _hash(fresh_body)))
        else:
            # User has edited this block — preserve their body verbatim and
            # do not bump the stored hash, so subsequent renders keep skipping
            # until the user re-aligns it (or watcher tombstones).
            out.append(cur_body)
    return out, pending


def write_dashboard(path: str, conn, *, state_dir: str,
                    db: str | None = None) -> None:
    # Reconcile md edits BEFORE render so Lumi's ✅/❌ + tick/untick flow back.
    # Fail-soft: a reconcile error must never block dashboard refresh.
    if os.path.exists(path):
        try:
            reconcile_milestone_candidates(conn, Path(path))
        except Exception as e:
            repo.add_alert(
                "warn", "dashboard",
                "dashboard_reconcile:milestone_candidates",
                source="dashboard.py", db=db,
                message=f"candidate reconcile failed: {e}; falling through to render",
            )
        try:
            reconcile_tasks(conn, Path(path))
        except Exception as e:
            repo.add_alert(
                "warn", "dashboard",
                "dashboard_reconcile:tasks",
                source="dashboard.py", db=db,
                message=f"task reconcile failed: {e}; falling through to render",
            )
        try:
            reconcile_affect(conn, Path(path))
        except Exception as e:
            repo.add_alert(
                "warn", "dashboard",
                "dashboard_reconcile:affect",
                source="dashboard.py", db=db,
                message=f"affect reconcile failed: {e}; falling through to render",
            )
        try:
            reconcile_alerts(conn, Path(path))
        except Exception as e:
            repo.add_alert(
                "warn", "dashboard",
                "dashboard_reconcile:alerts",
                source="dashboard.py", db=db,
                message=f"alerts reconcile failed: {e}; falling through to render",
            )
        try:
            reconcile_timeline(conn, Path(path))
        except Exception as e:
            repo.add_alert(
                "warn", "dashboard",
                "dashboard_reconcile:timeline",
                source="dashboard.py", db=db,
                message=f"timeline reconcile failed: {e}; falling through to render",
            )
    Path(state_dir).mkdir(parents=True, exist_ok=True)

    existing = ""
    if os.path.exists(path):
        existing = open(path, encoding="utf-8").read()
    before, cur_block, after = _split(existing)
    current_top = _current_top_text(existing) if cur_block else ""

    fresh = top_sections.iter_top_blocks(conn, dashboard_path=path)
    resolved, pending = _resolve_blocks(path, conn, fresh, current_top)
    new_top_region = _assemble_top_region(resolved)

    if cur_block:
        new = before + new_top_region + after
    elif existing:
        new = new_top_region + "\n\n" + existing
    else:
        new = new_top_region + "\n"

    # Write first, record hashes only on success. If _atomic_write raises
    # (ENOSPC, EACCES, SIGTERM mid-write), md_index keeps its prior baseline
    # so the next refresh still recognises Lumi's edits as user edits.
    _atomic_write(path, new)
    store = MdIndex(conn)
    for bid, h in pending:
        store.record_block(path, bid, h)


def _main() -> int:
    # CLI entry: `python -m marrow.dashboard` re-renders the dashboard.
    # Used by mw-dashboard-tick.plist (06:01 daily) and ad-hoc refresh.
    from . import config, storage
    db = config.db_path()
    conn = storage.connect(db)
    try:
        write_dashboard(config.dashboard_path(), conn,
                        state_dir=str(config.DATA_DIR / "state"), db=db)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
