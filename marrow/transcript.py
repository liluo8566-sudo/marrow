"""SessionEnd code-only transcript clean. CC .jsonl -> event rows.

Keep human dialogue verbatim (user + assistant text blocks). Drop tool_use /
tool_result / thinking / system / attachment / meta / sidechain noise.
Deterministic, no LLM. Output feeds repo.archive_events (idempotent).
"""
from __future__ import annotations

import json
import re

# Buddy MCP appends an invisible end-of-turn HTML comment to assistant text
# (<!-- buddy: ... -->). It is a legal text block so the type-based filter
# below never sees it; strip it here or it leaks into events -> digest ->
# diary as if 铁锅 were a speaker.
_BUDDY = re.compile(r"\s*<!--\s*buddy\s*:.*?-->", re.S | re.I)


def is_headless(jsonl_path: str) -> bool:
    """Hard-disabled bleed-stop: entrypoint cannot separate a real SDK session from a spawned `claude -p` — step4 rework."""
    # ep!="cli" was wrong: clawbot/Task-agent/worktree/vscode/desktop are
    # all real human sessions yet carry "sdk-cli" too. The old rule made
    # clean() drop them from events and cleanup.py unlink their .jsonl.
    # Keeping a fake spawn is reversible (dedup later); losing a real
    # session is not. Real headless signal TBD (ADR-0003 / step4).
    return False
    return False


def _text(content) -> str:
    if isinstance(content, str):
        s = content
    elif isinstance(content, list):
        s = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
            and b.get("text")
        )
    else:
        return ""
    return _BUDDY.sub("", s).strip()


def clean(jsonl_path: str) -> list[dict]:
    rows: list[dict] = []
    if is_headless(jsonl_path):
        return rows  # spawned claude -p (lint/digest): not a real session
    try:
        fh = open(jsonl_path, encoding="utf-8")
    except FileNotFoundError:
        return rows  # unflushed/headless transcript: nothing to clean, not an error
    with fh as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("type") not in ("user", "assistant"):
                continue
            if o.get("isMeta") or o.get("isSidechain"):
                continue
            msg = o.get("message") or {}
            text = _text(msg.get("content"))
            if not text:
                continue
            rows.append({
                "session_id": o.get("sessionId") or o.get("session_id") or "",
                "timestamp": o.get("timestamp", ""),
                "role": msg.get("role") or o.get("type"),
                "content": text,
                "channel": "cli",
            })
    return rows
