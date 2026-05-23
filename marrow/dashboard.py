"""Code-only dashboard top render. 4 top sections (Alerts/Tasks/Milestone/Affect)
between markers; hand-written zone outside markers untouched; atomic write.

Free-form hand-edits inside the rendered block are silently overwritten
on next render (DB is SoT; non-anchored text is not preserved). No LLM.

Section renderers live in top_sections.py — shared with handover_render.py.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import top_sections

M0 = "<!-- marrow:top:start -->"
M1 = "<!-- marrow:top:end -->"


def render_top(conn) -> str:
    return M0 + "\n" + top_sections.render_top(conn) + "\n" + M1


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
    block = render_top(conn)
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
