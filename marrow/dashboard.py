"""Code-only dashboard top render. 4 top sections (Alerts/Tasks/Milestone/Affect)
between markers; hand-written zone outside markers untouched; atomic write;
hash conflict guard (Lumi hand-edit -> backup + one alert). No LLM.

Section renderers live in top_sections.py — shared with handover_render.py.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import config, repo, top_sections

M0 = "<!-- marrow:top:start -->"
M1 = "<!-- marrow:top:end -->"


def render_top(conn) -> str:
    return M0 + "\n" + top_sections.render_top(conn) + "\n" + M1


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


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
    hash_file = Path(state_dir) / "dashboard.hash"

    existing = ""
    if os.path.exists(path):
        existing = open(path, encoding="utf-8").read()
    before, cur_block, after = _split(existing)

    if cur_block:
        last = hash_file.read_text() if hash_file.exists() else ""
        if last and _hash(cur_block) != last:
            # Lumi hand-edited the system block — never overwrite silently.
            bak = Path(state_dir) / f"dashboard.{int(time.time())}.bak"
            shutil.copyfile(path, bak)
            repo.add_alert(
                "warn", "dashboard",
                "dashboard top hand-edited; backed up before re-render, "
                f"see {bak.name}", source="dashboard.py", db=db,
            )
        new = before + block + after
    elif existing:
        new = block + "\n\n" + existing
    else:
        new = block + "\n"

    _atomic_write(path, new)
    hash_file.write_text(_hash(block))
