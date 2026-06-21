"""SessionEnd code-only transcript clean. CC .jsonl -> event rows.

Keep human dialogue verbatim (user + assistant text blocks). Drop tool_use /
tool_result / thinking / system / attachment / meta / sidechain noise.
Deterministic, no LLM. Output feeds repo.archive_events (idempotent).
"""
from __future__ import annotations

import json
import re

from . import config

# Fallback if config.toml omits [transcript].worker_models (e.g. a live
# config predating this key). Mirrors config.default.toml. Used by the
# headless-detection signal in is_headless() below.
_DEFAULT_WORKER_MODELS = ["claude-haiku-4-5", "claude-sonnet-4-6"]

# Empty-model backstop: a spawn that exited before any assistant flush has
# no model signal. Its first user / queue-operation content head matches a
# Marrow-pipeline or prompt-lint spawn prompt; a real interrupted session
# carries a human prompt instead. Heads kept in sync with the prompt
# constants in marrow/daily.py, marrow/sessionend_prompts.py, and
# ~/.claude/hooks/prompt-lint.py.
_SPAWN_HEADS = (
    "===== BEGIN ORIGINAL TRANSCRIPT",
    "You compress ONE long session of dialogue",
    "Extract per-episode affect from the session",
    "Extract candidate entities mentioned in the session",
    "Extract task-like items from the session",
    "Extract candidate life-shaping milestones from the session",
    "Extract candidate memes from the session",
    "Write the handover narrative",
    "格式（单一/混合）：散文段落",
    "You are a ruthless markdown compressor",
    "You compress a markdown edit",
    "Compress this file per the rules",
    "Compress NEW per the rules",
)

# ── CC harness marker strip ──────────────────────────────────────────────────
# Six patterns injected by the CC harness that pollute event bodies and recall
# queries. Stripped before archiving and before building recall needles.
_CMD_MSG_RE = re.compile(r'<command-message>.*?</command-message>', re.DOTALL)
_CMD_NAME_RE = re.compile(r'<command-name>(.*?)</command-name>', re.DOTALL)
_CMD_ARGS_RE = re.compile(r'<command-args>(.*?)</command-args>', re.DOTALL)
_IMG_REF_RE = re.compile(r'\[Image #\d+\]')
_IMG_SRC_RE = re.compile(r'\[Image: source: [^\]]*\]')
_LOCAL_STDOUT_RE = re.compile(r'<local-command-stdout>.*?</local-command-stdout>', re.DOTALL)


def strip_harness_markers(text: str) -> str:
    """Strip CC harness markers from a prompt or event body.

    Removes (in order):
      1. <command-message>...</command-message> — stripped entirely
      2. <command-name>foo</command-name> — replaced with inner text
      3. <command-args>bar</command-args> — replaced with inner text
      4. [Image #N] — stripped
      5. [Image: source: url] — stripped
      6. <local-command-stdout>...</local-command-stdout> — stripped entirely

    Collapses whitespace runs to a single space, then strips.
    Safe to call on non-CC text — all patterns are specific enough to be no-ops.
    """
    text = _CMD_MSG_RE.sub('', text)
    text = _CMD_NAME_RE.sub(r'\1', text)
    text = _CMD_ARGS_RE.sub(r'\1', text)
    text = _IMG_REF_RE.sub('', text)
    text = _IMG_SRC_RE.sub('', text)
    text = _LOCAL_STDOUT_RE.sub('', text)
    return re.sub(r'[ \t]+', ' ', text).strip()


# ── synapse-wx bridge boilerplate strip ──────────────────────────────────────
# Three patterns injected by the bridge that must not enter recall queries or
# event bodies.
#
# 1. Media Read instruction — a bare "Use the Read tool to view: <paths>"
#    line appended by synapse_wx/media/inbound.py build_read_tool_instruction
#    (no tag, paths comma-joined on ONE line). Match that line only — user
#    text can follow it in stitched event bodies (seen live, event#1096).
_WX_READ_INSTR_RE = re.compile(
    r"^(?:<instruction>\s*)?Use the Read tool to view:[^\n]*\n?",
    re.I | re.M,
)
# 2. Merge note — prepended as the first line by synapse_wx/loop.py.
#    Defensive: match any full line of the form "[bridge: ...]".
_WX_MERGE_NOTE_RE = re.compile(r"^\[bridge:[^\]]*\]\n?", re.M)
# 3. Lone "." sentinel — a pure-media bubble arrives as body "." + instruction.
#    After patterns 1 & 2 are stripped this may leave a bare dot line.
_WX_DOT_SENTINEL_RE = re.compile(r"^\.\s*$", re.M)


def strip_wx_boilerplate(text: str) -> str:
    """Strip synapse-wx bridge boilerplate from a prompt or event body.

    Removes (in order):
      1. Media Read instruction block (``<instruction>Use the Read tool...``)
      2. Bridge merge-note lines (``[bridge: ...]``)
      3. Bare dot-sentinel lines left by pure-media bubbles

    Returns the cleaned text stripped of leading/trailing whitespace.
    Safe to call on non-wx text — patterns are specific enough to be no-ops.
    """
    text = _WX_READ_INSTR_RE.sub("", text)
    text = _WX_MERGE_NOTE_RE.sub("", text)
    text = _WX_DOT_SENTINEL_RE.sub("", text)
    return text.strip()


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
    """True iff assistant model-set ⊆ worker_models (prefix-match), or
    (empty set) the first user/queue-op content head is a known spawn
    prompt. Conservative: no match -> not headless (keep)."""
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
    return strip_harness_markers(strip_wx_boilerplate(s))


def _active_chain_uuids(records: list[dict]) -> set[str]:
    """Reconstruct the post-rewind active conversation by walking parentUuid.

    CC `/rewind` does NOT set isSidechain on rewound turns; it just writes a
    new branch whose first turn's parentUuid points back above the rewind
    point. The active conversation is therefore the chain ending at the LAST
    record in file order with a uuid. Walk parentUuid backward from there
    to collect all uuids on that chain. Records whose uuid is not in this
    set were rewound out and must be dropped.
    """
    by_uuid: dict[str, dict] = {}
    tail: str | None = None
    for r in records:
        u = r.get("uuid")
        if not u:
            continue
        by_uuid[u] = r
        tail = u  # last uuid in file order
    if tail is None:
        return set()
    chain: set[str] = set()
    cur: str | None = tail
    while cur and cur in by_uuid and cur not in chain:
        chain.add(cur)
        cur = by_uuid[cur].get("parentUuid")
    return chain


def clean(jsonl_path: str, *, skip_headless_check: bool = False, channel: str = "cli") -> list[dict]:
    rows: list[dict] = []
    if not skip_headless_check and is_headless(jsonl_path):
        return rows  # spawned claude -p (lint/digest): not a real session
    try:
        fh = open(jsonl_path, encoding="utf-8")
    except FileNotFoundError:
        return rows  # unflushed/headless transcript: nothing to clean, not an error
    with fh as f:
        records: list[dict] = []
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(o)
    # First pass: build active-chain uuid set so rewound turns drop out.
    # CC `/rewind` leaves isSidechain=False on rewound turns, so the
    # type/isSidechain filter alone would silently digest them as if they
    # had happened. Walk parentUuid from the file's last uuid backward.
    active = _active_chain_uuids(records)
    for o in records:
        if o.get("type") not in ("user", "assistant"):
            continue
        if o.get("isMeta") or o.get("isSidechain"):
            continue
        u = o.get("uuid")
        # Only enforce chain membership when the record has a uuid; records
        # without one (legacy / summary-style lines) keep the prior behavior.
        if u and u not in active:
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
            "channel": channel,
        })
    return rows
