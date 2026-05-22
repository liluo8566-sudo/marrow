"""Code-only dashboard top render (#8). Deterministic Alerts + Open Threads
between markers; hand-written zone outside markers untouched; atomic write;
hash conflict guard (Lumi hand-edit -> backup + one alert). No LLM.

The curated Next/Soon + dehydrated craft/study rewrite is the nightly LLM
routine's job (render-templates.md); this is the always-correct code refresh.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import config, repo

M0 = "<!-- marrow:top:start -->"
M1 = "<!-- marrow:top:end -->"

# Order matters — milestone first (highest-touch), curated by hand to put
# the highest-utility navigation links closest to where Lumi reads. New
# views: add to subpages.build_all_configs AND mirror the key here.
_SUB_PAGE_NAV = [
    "milestone", "diary", "study", "projects",
    "cheatsheet", "memes", "goose",
]


def render_top(conn) -> str:
    alerts = repo.open_alerts(conn)
    threads = repo.open_threads(conn)
    out = [M0, "# Marrow", "", "## Alerts"]
    if alerts:
        for a in alerts:
            src = f" ({a['source']})" if a.get("source") else ""
            out.append(f"- #{a['id']} [{a['severity']}] {a['message']}{src}")
    else:
        out.append("- none")
    out += ["", "## Open Threads"]
    if threads:
        for t in threads:
            due = f" [Due {t['due']}]" if t.get("due") else ""
            nxt = f" — {t['next_step']}" if t.get("next_step") else ""
            out.append(f"- #{t['id']} [{t['category']}] {t['title']}{nxt}{due}")
    else:
        out.append("- none")
    out += ["", "## Sub Pages"]
    folder = Path(config.sub_pages_path())
    for key in _SUB_PAGE_NAV:
        f = folder / f"{key}.md"
        if f.exists():
            out.append(f"- [[sub_pages/{key}]]")
    out.append(M1)
    return "\n".join(out)


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
