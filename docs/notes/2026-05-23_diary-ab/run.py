"""One-off haiku vs sonnet A/B on SINGLE_CALL_PROMPT. Read-only on DB."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/Gabrielle/cc-lab/marrow")

from marrow.diary import (  # noqa: E402
    SINGLE_CALL_PROMPT,
    _fence,
    _hhmm,
    _local_md,
    _parse_single_call,
    _speaker,
)
from marrow.llm import LLMClient  # noqa: E402

SESSION_ID = "d89edbc5-a098-4982-ab37-33d2cf07b0cf"
DATE = "2026-05-21"
DB = "/Users/Gabrielle/.config/marrow/marrow.db"
OUT_DIR = Path("/Users/Gabrielle/Desktop/marrow_ab")


def load_transcript() -> str:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role, content, timestamp FROM events "
        "WHERE session_id = ? AND role IN ('user','assistant') "
        "ORDER BY timestamp, id",
        (SESSION_ID,),
    ).fetchall()
    conn.close()
    if not rows:
        raise SystemExit("no events for that session")
    start = rows[0]["timestamp"]
    end = rows[-1]["timestamp"]
    span = f"{_local_md(start)} {_hhmm(start)}-{_hhmm(end)}"
    lines = [
        f"[{_speaker(r['role'])}] {r['content']}" for r in rows
    ]
    blocks = f"[{span}] session {SESSION_ID}\n" + "\n".join(lines)
    return _fence(blocks)


def run_one(tier: str, sessions_text: str) -> dict:
    llm = LLMClient()
    prompt = SINGLE_CALL_PROMPT.format(date=DATE, sessions=sessions_text)
    t0 = time.monotonic()
    raw = llm.call("diary", prompt, tier=tier)
    elapsed = time.monotonic() - t0
    prose, affect_raw, outcome, err = _parse_single_call(raw)
    episodes = [p.strip() for p in prose.split("---") if p.strip()]
    return {
        "tier": tier,
        "elapsed_s": round(elapsed, 2),
        "chars": len(raw),
        "raw": raw,
        "prose": prose,
        "affect": affect_raw,
        "outcome": outcome,
        "err": err,
        "ep_count": len(episodes),
    }


def main() -> None:
    sessions_text = load_transcript()
    print(f"transcript chars: {len(sessions_text)}", flush=True)
    full_prompt = SINGLE_CALL_PROMPT.format(date=DATE, sessions=sessions_text)
    print(f"full prompt chars: {len(full_prompt)}", flush=True)

    results = {}
    for tier in ("cheap", "mid"):
        print(f"\n== running tier={tier} ==", flush=True)
        try:
            r = run_one(tier, sessions_text)
        except Exception as e:
            print(f"FAILED tier={tier}: {e}", flush=True)
            results[tier] = {"error": str(e)}
            continue
        results[tier] = r
        print(f"  elapsed={r['elapsed_s']}s chars={r['chars']} "
              f"outcome={r['outcome']} ep={r['ep_count']}", flush=True)
        (OUT_DIR / f"{tier}_raw.txt").write_text(r["raw"])
        (OUT_DIR / f"{tier}_prose.txt").write_text(r["prose"])
        (OUT_DIR / f"{tier}_affect.json").write_text(
            json.dumps(r["affect"], ensure_ascii=False, indent=2))

    summary = {
        tier: {k: v for k, v in r.items() if k not in ("raw", "prose")}
        for tier, r in results.items()
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n== done ==")
    print(f"outputs in {OUT_DIR}")


if __name__ == "__main__":
    main()
