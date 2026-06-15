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
- **SoT**: md is user-facing SoT for dims; hand-edits always preserved. sticker_update/ingest sync DB+md atomically. (verified)
- **Candidate ingest**: 0-audit direct insert (entity/pref ≥0.8 · memes ≥0.7 · milestone ≥0.85). No staging table, no confirm CLI.
- **Handover shape**: state-axis (Done / Open / Plan / Reference).
- **SessionEnd LLM**: single sonnet call, multi-seg output, Popen detach. No multi-call split.
- **Pending detection**: sonnet emits `unresolved: bool` per ep, skip-generously, emotional only. Work / study → open threads.
- **Refusal sentinel**: policy-refusal caught as failure → 3-stage fallback. Never into diary.
- **LLM subprocess isolation**: all spawns go through `LLMClient` with `--setting-sources "" --strict-mcp-config`. Strips user hooks / MCP from the child.
- **SQLite journal mode = DELETE, never WAL** (verified): WAL's `.db-shm` mmap triggers a reproducible macOS APFS SIGBUS with 3+ threaded connections (2026-05-28 crash, docs/archives/PROGRESS.md:368-373). Contention handled by busy_timeout=30s + no-second-conn-inside-txn rule. Do not "optimise" back to WAL.
- **Alert contract — two-strike** (Lumi-set, plan docs/archives/0611-alert-redesign.md): every failure recorded in audit_log; first failure silent, alert only when the catchup retry also fails. Fingerprint = stable token (exception text in message), one deduped row per cause. add_alert never raises (file fallback). Skips are terminal, never alerted.
- **Strong-hit tiered needles** (verified, 2026-06-12): two tiers with floors — name/key/title hit = user named the thing, floor 0.55 (top billing); body hit floor 0.45 (above noise band, below real matches). Body 2-char windows filtered by frequency + function-char + stop-bigram, NOT by length — a pure 3-char minimum broke (想减肥)→Weight-lose because the 2-char anchor (减肥) lived in the value. Length cut was tried and reverted same night; do not re-simplify to it. ASCII needles letter-boundary matched, digits transparent ((gpt) hits (gpt4画画), (nd) can't hit (handover)); mixed cjk/ascii tokens contribute their ascii runs ((马自达suv)→(suv)). Floor 0.50 + bias 0.10 kept — event lane max ≈1.10 vs dims ≈0.9, bias is the equaliser (Lumi-confirmed); min_score 0.40 (user config, was 0.35) is the noise gate. Verified live against marrow.db: 4 regressions hit, 3 noise queries zero, all keeps intact.

## 2026-06-12 — Recall noise batch: name-layer filtering + harness strip
- (verified) Strong-hit NAME-layer needles (entity name/alias, meme key, milestone title) now pass `_filter_generic_cjk`: 2-char CJK windows dropped on stop-bigram/func-char (no DF — small high-confidence tables); 3-4 char windows dropped on ≥2 func chars or embedded stop bigram (你说我/我现在 die, 在一起 lives). Root cause of 0.55-band dims noise: name layer had zero filtering while body layer had three.
- (verified) CC harness markers (`<command-message/name/args>`, `[Image #N]`, `[Image: source: ...]`, `<local-command-stdout>`) stripped at BOTH write path (transcript._text) and query path (hooks user_prompt_submit + recall_fusion). `[Image #1]` in prompts was name-hitting meme 'GPT image gen' via needle `image`.
- (verified) Stored events cleaned by scripts/clean_harness_events.py: update=106 (content + source_hash recomputed, vec rows dropped for re-embed), delete=25 pure-marker rows (tombstoned). Backup: ~/.config/marrow/backups/pre-harness-clean-20260612-2101.db. Residual 2 rows contain literal `[Image #N]` (letter N, discussion text) — correct non-matches, not leaks.
- (reasoned) `cc`/`gpt` stay as legit short ASCII name anchors (real names, letter-boundary protected); milestone#20 riding `cc` mentions is tolerated, gated by vec floor.
