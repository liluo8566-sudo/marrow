"""SessionEnd code-only transcript clean. CC .jsonl -> event rows.

Keep human dialogue verbatim (user + assistant text blocks). Drop tool_use /
tool_result / thinking / system / attachment / meta / sidechain noise.
Deterministic, no LLM. Output feeds repo.archive_events (idempotent).
"""
from __future__ import annotations

import json
import re

from . import config

# ADR-0004 fallback if config.toml omits [transcript].worker_models (e.g. a
# live config predating this key). Mirrors config.default.toml.
_DEFAULT_WORKER_MODELS = ["claude-haiku-4-5", "claude-sonnet-4-6"]

# Empty-model backstop: a spawn that exited before any assistant flush has
# no model signal. Its first user / queue-operation content head matches a
# Marrow-pipeline or prompt-lint spawn prompt; a real interrupted session
# carries a human prompt instead. Heads kept in sync with the prompt
# constants in marrow/daily.py, marrow/sessionend_prompts.py, and
# ~/.claude/hooks/prompt-lint.py.
_SPAWN_HEADS = (
    "You compress ONE long session of dialogue",
    "Extract per-episode affect from the session",
    "Extract candidate entities mentioned in the session",
    "Extract task-like items from the session",
    "Extract candidate life-shaping milestones from the session",
    "Extract candidate vocab from the session",
    "Write the handover narrative",
    "你是褚屿忱，你要以第一人称写一篇日记",
    "You are a ruthless markdown compressor",
    "You compress a markdown edit",
    "Compress this file per the rules",
    "Compress NEW per the rules",
)

# Buddy MCP appends an invisible end-of-turn HTML comment to assistant text
# (<!-- buddy: ... -->). It is a legal text block so the type-based filter
# below never sees it; strip it here or it leaks into events -> digest ->
# diary as if 铁锅 were a speaker.
_BUDDY = re.compile(r"\s*<!--\s*buddy\s*:.*?-->", re.S | re.I)


def worker_models() -> list[str]:
    try:
        w = config.load().get("transcript", {}).get("worker_models")
    except Exception:
        w = None
    return list(w) if w else list(_DEFAULT_WORKER_MODELS)


def _raw_content_str(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                return b["text"]
    return ""


def is_headless(jsonl_path: str) -> bool:
    """ADR-0004: True iff assistant model-set ⊆ worker_models, or (empty
    set) the first user/queue-op content head is a known spawn prompt."""
    workers = worker_models()
    models: set[str] = set()
    first_head = ""
    try:
        fh = open(jsonl_path, encoding="utf-8")
    except OSError:
        return False  # missing/unreadable -> safest is keep, never auto-delete
    with fh as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if t == "assistant":
                m = (o.get("message") or {}).get("model")
                if m and m != "<synthetic>":
                    models.add(m)
            elif not first_head and t in ("user", "queue-operation"):
                msg = o.get("message") or {}
                first_head = _raw_content_str(
                    msg.get("content") if msg else o.get("content")).strip()
    if models:
        return all(any(m.startswith(w) for w in workers) for m in models)
    return any(first_head.startswith(h) for h in _SPAWN_HEADS)


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
