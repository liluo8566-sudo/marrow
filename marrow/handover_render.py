"""Handover renderer — §4.1, zero LLM in skeleton, one atomic write per write.

Rendered output goes to ~/.config/marrow/handover.md (NOT ~/cc-lab/marrow/handover.md).
Split rationale: ~/cc-lab/marrow/handover.md is hand-edited by Lumi each session;
writing there would clobber her content. The rendered artifact is a separate
machine-written file read by SessionStart for inject.

Single-writer rule (Bug #1): sessionend_async is the only writer that hits
handover.md in production. It calls write_handover_full() once, building
skeleton + LLM-filled ThisSession / NextSession + stamp in a single atomic
write. SessionStart is read-only — it MUST NOT call any function here that
mutates the file.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import config
from . import top_sections
from .dashboard import _atomic_write

_TEMPLATE_PATH = Path(__file__).parent / "handover_template.md"

# Sandwich markers from the template
_SEP_OPEN = "—————以下这一段应该是跟dashboard的top一模一样的—————"
_SEP_CLOSE = "—————以上这一段应该是跟dashboard的top一模一样的—————"

_RENDERED_PATH = config.DATA_DIR / "handover.md"


def _strip_instruction_lines(text: str) -> str:
    """Remove lines starting with '> ' (system instruction lines)."""
    kept = []
    for line in text.splitlines():
        if line.startswith("> "):
            continue
        kept.append(line)
    return "\n".join(kept)


def _replace_top_sections(text: str, rendered: str) -> str:
    """Replace content between _SEP_OPEN and _SEP_CLOSE with rendered block."""
    i = text.find(_SEP_OPEN)
    j = text.find(_SEP_CLOSE)
    if i == -1 or j == -1 or j <= i:
        return rendered + "\n\n" + text
    before = text[:i + len(_SEP_OPEN)]
    after = text[j:]
    return before + "\n" + rendered + "\n" + after


def _last_3_commits() -> str:
    """git log -3 --oneline from the marrow repo, empty on any failure."""
    repo = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.check_output(
            ["git", "log", "-3", "--oneline"],
            cwd=str(repo), text=True, timeout=2,
            stderr=subprocess.DEVNULL,
        )
        return "\n".join(f"- {ln}" for ln in out.strip().splitlines())
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def _inject_reference_commits(text: str, commits: str) -> str:
    """Insert commit list under `## Reference (last 3 commits)`."""
    if not commits:
        return text
    pat = re.compile(
        r"(^## Reference \(last 3 commits\)[ \t]*\n)(.*?)(?=^## |^<!--|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    return pat.sub(lambda m: f"{m.group(1)}{commits}\n\n", text, count=1)


def _inject_section(text: str, header: str, body: str) -> str:
    """Replace body under `## <header>` up to next `## ` or HTML comment."""
    if not body:
        return text
    pat = re.compile(
        rf"(^## {re.escape(header)}[ \t]*\n)(.*?)(?=^## |^<!--|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    return pat.sub(lambda m: f"{m.group(1)}{body}\n\n", text, count=1)


def render_skeleton(conn: sqlite3.Connection) -> str:
    """Build template body with top sections + commits, no stamp, empty bullets."""
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    template = _strip_instruction_lines(template)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    template = template.replace("{{YYYY-MM-DD HH:MM}}", now_str)
    top = top_sections.render_top(conn)
    template = _replace_top_sections(template, top)
    template = _inject_reference_commits(template, _last_3_commits())
    return template


def _append_stamp(text: str, stamp: str) -> str:
    if not text.endswith("\n"):
        text += "\n"
    return text + stamp + "\n"


def render_full(conn: sqlite3.Connection, sid: str,
                this_session: str, next_session: str) -> str:
    """Compose skeleton + ThisSession + NextSession + ready stamp."""
    text = render_skeleton(conn)
    text = _inject_section(text, "This Session", this_session)
    text = _inject_section(text, "Next Session", next_session)
    ts = int(time.time())
    stamp = f"<!-- handover: ready sid:{sid} ts:{ts} -->"
    return _append_stamp(text, stamp)


def write_handover_full(conn: sqlite3.Connection, sid: str,
                        this_session: str, next_session: str) -> Path:
    """Single-writer atomic write of complete handover.md (Bug #1 fix).

    Called exclusively from sessionend_async — never from session_start hooks
    or any other path. SessionStart must read this file, never overwrite it.
    """
    text = render_full(conn, sid, this_session, next_session)
    _atomic_write(str(_RENDERED_PATH), text)
    return _RENDERED_PATH
