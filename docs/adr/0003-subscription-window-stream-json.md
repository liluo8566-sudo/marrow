Status: accepted 2026-05-17

**Constraint**
No Anthropic API key. Pipeline LLM via local `claude` CLI subprocess. From 2026-06-15, `-p` (print) moves to credit-pool billing; OAuth 5-hour subscription window carries pipeline at ~0 cost.

**Decision**

Default provider: `_run_claude_stream` (llm.py)
```
claude --output-format stream-json --input-format stream-json --verbose --model <m> --setting-sources "" --strict-mcp-config
```

Stdin: one JSON line `{"type":"user","message":{"role":"user","content":"<prompt>"}}`, close, read stdout until `type":"result"`.

Flag meanings:
- `--output-format stream-json --input-format stream-json` — interactive session, bills to OAuth 5h window (not credit pool).
- `--verbose` — required for `result` event.
- `--setting-sources "" --strict-mcp-config` — isolation, no persona/user MCP/output-style inheritance.
- Watch stdout for `rate_limit_event` (rateLimitType, isUsingOverage).

Evidence: Haiku returned PONG in 2.7s, `rate_limit_event` with `rateLimitType:"five_hour"` confirmed.

**Fallback**

`_run_claude_p` (config `[llm.claude_cli] mode = "p"`) uses `-p --output-format json`, credit-pool billing. One-line switch, callers unchanged. Ollama available (local, no cost). Use `-p` only if stream breaks.

**Day boundary + map-reduce**

A diary day = local `[D 04:00, D+1 04:00)` (00:00-04:00 = previous day). Computed via `zoneinfo Australia/Melbourne` (auto AEST/AEDT), not UTC substr. `run_day` is per-session map-reduce: one haiku digest per session (oversized session chunked first), merged digests then sonnet diary + haiku lessons — never the whole day in one prompt.

**Scheduling (independent launchd jobs, decoupled)**

- `mw-diary-routine.plist` — 04:00 local, `marrow.diary` (no flag), writes the just-closed day.
- `mw-diary-catchup.plist` — 16:00 local, `marrow.diary --catchup`, scans last 7 days (cap 3, overflow alert), backfills anything the routine missed.
- `mw-jsonl-cleanup.plist` — Sunday 05:00 local, `marrow.cleanup --apply`, reaps sdk-cli jsonl older than grace_days (disk/UX only, data already firewalled). Standalone, never inside the diary routine.
- Separate so one job failing never starves another; all idempotent (`diary.date` / re-scan). Sources in `deploy/`, `launchctl bootstrap gui/$UID` directly from the repo path (not copied to `~/Library/LaunchAgents/`), DST auto-followed.
- Do NOT use Anthropic `/schedule` skill — cloud sandbox has no local SQLite, .venv, or OAuth `claude`.

**Consequences**

- Steady state: diary ≈ 3 stream calls/day on subscription window, ~0 pool burn.
- Re-verify `rate_limit_event` after `claude` CLI upgrade; `-p` fallback exists if behaviour regresses.
