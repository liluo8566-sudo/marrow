"""Code-only dashboard top render. 5 top sections
(Alerts/Tasks/Milestone/Affect/Content) between markers; hand-written
zone outside markers untouched; atomic write.

Free-form hand-edits inside the rendered block are silently overwritten
on next render (DB is SoT; non-anchored text is not preserved). No LLM.

Anchor-button votes on candidate rows (✅ pin · ❌ drop · ✏️ edit) are
absorbed by reconcile.reconcile_milestone_candidates BEFORE re-render
so Lumi's vote flows back to DB and the new render reflects it.

Section renderers live in top_sections.py — shared with handover_render.py.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import repo, top_sections
from .reconcile import reconcile_milestone_candidates, reconcile_tasks

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


def _atomic_write(path: str, data: str) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".dash.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
                f"candidate reconcile failed: {e}; falling through to render",
                source="dashboard.py", db=db,
            )
        try:
            reconcile_tasks(conn, Path(path))
        except Exception as e:
            repo.add_alert(
                "warn", "dashboard",
                f"task reconcile failed: {e}; falling through to render",
                source="dashboard.py", db=db,
            )
    block = render_top(conn, dashboard_path=path)
    Path(state_dir).mkdir(parents=True, exist_ok=True)

    existing = ""
    if os.path.exists(path):
        existing = open(path, encoding="utf-8").read()
    before, cur_block, after = _split(existing)

    if cur_block:
        new = before + block + after
    elif existing:
        new = block + "\n\n" + existing
    else:
        new = block + "\n"

    _atomic_write(path, new)
