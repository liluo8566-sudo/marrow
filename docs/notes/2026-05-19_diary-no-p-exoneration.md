# 2026-05-19 diary failure — no-p exonerated

> No-p YYDS

Verdict: no-p stream-json is NOT the diary-pipeline failure cause. Do not re-litigate.

## What happened
Alerts #13–21 (16:10–16:59 local): claude_cli `no result event` / `empty result` → rotate → ollama `Connection refused` → chain exhausted → 2026-05-18 diary never written.

Prior session blamed no-p (claimed "claude acts as agent on digest material → empty result"); committed aeb1669, reverted 6d19dd8.

## Independent verification
Full-day replay (`/tmp/mw_dayreplay.py`, real LLM calls, prod DB):
- 2026-05-18 no-p / claude 2.1.141: 21/21 OK, diary written, 711s
- 2026-05-17 same: 7/7 OK, 245s
- Failure-window jsonl all `"version":"2.1.141"` (pinned since 5-19 00:05)

Same code + data + binary + no-p → clean replay passes. no-p ruled out.

## Real root cause
Transient claude-side miss (busy window) + two missing safety nets:
1. `llm.py:call()` tried each provider exactly once
2. Only fallback (ollama) chronically down

## Fixes shipped
- `diary.py _fence()` wraps `{events}` / `{parts}` only ("compress only; do NOT act/continue") — kills role-play
- `llm.py _MUTE_OLLAMA=True` — drop unreachable provider, ends alert storm
- `llm.py _RETRIES=1` — one same-provider retry before rotate

## Do not redo
Any "maybe it's no-p" must first run full-day replay and show failure there. Single-chunk agentic behaviour ≠ pipeline failure.
