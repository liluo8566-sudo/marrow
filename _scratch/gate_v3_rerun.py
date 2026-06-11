#!/usr/bin/env python3
"""Gate v3 rerun script — run the 3 gate v2 sessions through the NEW merged
TASK_AFFECT_DIGEST_PROMPT and write blind/key docs.

Usage:
    python3 _scratch/gate_v3_rerun.py

Reads from production DB (~/.config/marrow/marrow.db).
Writes to docs/notes/ (blind) and docs/notes/ (key).
Nothing written to production DB.

Sessions:
  b45a9959 - 2026-06-10 - 71 events - task
  5bba1890 - 2026-06-09 - 29 events - casual (study)
  383cafc3 - 2026-06-09 - 88 events - casual (personal)
"""
from __future__ import annotations

import datetime as _dt
import random
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

# Add worktree root to path so we can import marrow
_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root))

from marrow import config, storage
from marrow.llm import LLMClient, LLMError
from marrow.sessionend_async import _session_events_text, _load_active_tasks_for_sonnet
from marrow.sessionend_prompts import TASK_AFFECT_DIGEST_PROMPT

_TZ = ZoneInfo("Australia/Melbourne")

_SIDS = [
    ("b45a9959-c3e5-4378-8ee5-e3153574dd13", "2026-06-10", "task", 71),
    ("5bba1890-4998-42bc-bdbb-b0d9177d3832", "2026-06-09", "casual-study", 29),
    ("383cafc3-c6ff-4516-842f-6260f07967f5", "2026-06-09", "casual-personal", 88),
]

# Blind letter assignment — randomise order so letter != session rank
_LETTERS = ["A", "B", "C"]


def _run_session(conn, sid: str, llm: LLMClient) -> str:
    """Extract transcript and call the merged prompt."""
    events_text, _ = _session_events_text(conn, sid)
    active_tasks = _load_active_tasks_for_sonnet(conn)
    raw = llm.call(
        role="sessionend_task_affect",
        body=TASK_AFFECT_DIGEST_PROMPT.format(
            sid=sid, events=events_text,
            active_tasks=active_tasks, git_log=""),
        tier="mid",
    )
    return raw


def _extract_digest_block(raw: str) -> str:
    """Slice ===DIGEST===...===END=== for the blind doc."""
    i = raw.find("===DIGEST===")
    if i < 0:
        return raw.strip()
    tail = raw[i + len("===DIGEST==="):]
    j = tail.find("===END===")
    return tail[:j].strip() if j >= 0 else tail.strip()


def main():
    db_path = "/Users/Gabrielle/.config/marrow/marrow.db"
    conn = storage.connect(db_path)

    cfg = config.load()
    llm = LLMClient(cfg=cfg)

    # Shuffle for blind assignment
    order = list(range(len(_SIDS)))
    random.seed(42)  # deterministic blind shuffle
    random.shuffle(order)

    results: list[tuple[str, str, str, str, str]] = []  # (letter, sid, kind, raw, digest)
    for letter, idx in zip(_LETTERS, order):
        sid, date, kind, ev_count = _SIDS[idx]
        print(f"Running session {letter} (sid={sid[:8]}, {kind}, {ev_count}ev)...")
        try:
            raw = _run_session(conn, sid, llm)
            digest = _extract_digest_block(raw)
            results.append((letter, sid, kind, raw, digest))
            print(f"  OK — digest {len(digest)} chars")
        except LLMError as e:
            print(f"  FAILED: {e}")
            results.append((letter, sid, kind, f"ERROR: {e}", f"ERROR: {e}"))

    conn.close()

    # Write blind doc (digest blocks only, no sid/kind info)
    blind_lines = [
        "2026-06-11",
        "",
        "# TL Gate v3 — Blind Comparison",
        "",
        "Prompt: TASK_AFFECT_DIGEST_PROMPT (merged sonnet call, Batch 3 final)",
        "Sessions: same 3 as gate v2. Letters randomised.",
        "",
        "Judge criteria (same as v2):",
        "- LIFE accuracy: zero confabulation",
        "- CN fluency, plain words, life perspective",
        "- TL: 念念 POV, no project jargon",
        "- VOICE: near-verbatim fragments",
        "- FACTS (task): concise, outcome-focused",
        "- AFFECT: near-verbatim descriptions, open flag accuracy",
        "",
    ]
    for letter, sid, kind, raw, digest in results:
        blind_lines.append(f"## Session {letter}")
        blind_lines.append("")
        blank_kind = "task" if "task" in kind else "casual"
        blind_lines.append(f"Kind hint: {blank_kind}")
        blind_lines.append("")
        blind_lines.append("```")
        blind_lines.append(digest)
        blind_lines.append("```")
        blind_lines.append("")

    # Write key doc (mapping + full raw outputs)
    key_lines = [
        "2026-06-11",
        "",
        "# TL Gate v3 — Key",
        "",
        "## Session → sid → kind",
        "",
    ]
    for letter, sid, kind, raw, digest in results:
        key_lines.append(f"- Session {letter}: sid={sid}, kind={kind}")
    key_lines.append("")
    key_lines.append("## Full raw outputs")
    key_lines.append("")
    for letter, sid, kind, raw, digest in results:
        key_lines.append(f"### Session {letter} (sid={sid})")
        key_lines.append("")
        key_lines.append("```")
        key_lines.append(raw)
        key_lines.append("```")
        key_lines.append("")

    out_dir = _root / "docs" / "notes"
    out_dir.mkdir(parents=True, exist_ok=True)

    blind_path = out_dir / "0611-gate-v3-blind.md"
    key_path = out_dir / "0611-gate-v3-key.md"

    blind_path.write_text("\n".join(blind_lines), encoding="utf-8")
    key_path.write_text("\n".join(key_lines), encoding="utf-8")

    print(f"\nBlind: {blind_path}")
    print(f"Key:   {key_path}")
    print("Done.")


if __name__ == "__main__":
    main()
