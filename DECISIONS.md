# Marrow — decisions

> Forks taken. Each line is the current pick — change it when conditions change.
> Code state → MAP. Spec / thresholds → DESIGN. Reasoning detail → docs/notes.

## Current forks
- **LLM main**: `claude` stream-json subscription (no-p default). `-p` as manual fallback. No paid API. No ollama backup.
- **Embedder**: bge-m3 1024d, in-process inside the marrow daemon.
- **Recall fusion**: single weighted scalar (vec / bm25 / recency / affect). No RRF, no rerank stage.
- **Recall trigger**: dual track — deterministic cue at backdrop tail + UserPromptSubmit vector recall.
- **Decay**: read-time lazy weighting + floor tiers. No destructive background demotion job.
- **Affect granularity**: per-episode, Lumi-locked. Not per-event, not per-day.
- **Importance 1–5 anchor**: 5 = life-shaping (1m+) · 4 = weighty (days–weeks) · 3 = short-term (<1w) · 2 = daily routine · 1 = trivial. V/A measure THIS moment; importance measures future retention. Independent axes; tiebreak picks lower.
- **SoT**: md is SoT, DB is index. Hand-edits always preserved.
- **Candidate ingest**: 0-audit direct insert (entity/pref ≥0.8 · memes ≥0.7 · milestone ≥0.85). No staging table, no confirm CLI.
- **Handover shape**: state-axis (Done / Open / Plan / Reference).
- **SessionEnd LLM**: single sonnet call, multi-seg output, Popen detach. No multi-call split.
- **Pending detection**: sonnet emits `unresolved: bool` per ep, skip-generously, emotional only. Work / study → open threads.
- **Refusal sentinel**: policy-refusal caught as failure → 3-stage fallback. Never into diary.
- **LLM subprocess isolation**: all spawns go through `LLMClient` with `--setting-sources "" --strict-mcp-config`. Strips user hooks / MCP from the child.
- **SQLite journal mode = DELETE, never WAL** (verified): WAL's `.db-shm` mmap triggers a reproducible macOS APFS SIGBUS with 3+ threaded connections (2026-05-28 crash, docs/archives/PROGRESS.md:368-373). Contention handled by busy_timeout=30s + no-second-conn-inside-txn rule. Do not "optimise" back to WAL.
- **Alert contract — two-strike** (Lumi-set, plan docs/plans/0611-alert-redesign.md): every failure recorded in audit_log; first failure silent, alert only when the catchup retry also fails. Fingerprint = stable token (exception text in message), one deduped row per cause. add_alert never raises (file fallback). Skips are terminal, never alerted.
