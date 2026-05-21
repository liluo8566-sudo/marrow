# Marrow — current decisions (single source of current truth)

> Read this first. Every line carries a confidence tag:
> verified = real-source/shipped/pytest-proven · reasoned = argued, not yet built · assumed = hearsay, suspect by default — challenge freely.
> Overturn = overwrite the line in place (never stack versions). DESIGN holds goal+structure; this holds what we currently believe.

## Phase 1 (shipped, pytest 132)
- [verified] is_headless = assistant model-set non-empty AND ⊆ config worker_models; entrypoint marker abandoned · goal6 · PROGRESS 2026-05-19 blocker#1
- [verified] diary = 04:00 routine writes just-closed day + 16:00 catchup backfills ≤7d (cap3 + overflow alert); local-04:00 day boundary; per-session map → haiku stitch → sonnet write · goal4 · PROGRESS 2026-05-17/18
- [verified] LLM calls default stream-json subscription window / claude -p fallback / Ollama emergency; intent-only client(role,body,tier), default→fallback→emergency chain + per-step alert · goal1 · PROGRESS 2026-05-17
- [verified] SessionEnd = code-only clean+archive (strip tool/thinking/system, keep full human dialogue) + dashboard-top regen, no LLM/emotion · goal6 · PROGRESS 2026-05-17 #7
- [verified] 4 hooks at Phase-1 subset shipped; PreToolUse = global prompt-guard scope-extended to marrow, not a local copy · goal6
- [verified] timeout = process-group kill (start_new_session + os.killpg, both LLM paths) · goal6 · PROGRESS 2026-05-19 #8
- [verified] DB backup = VACUUM INTO + os.replace atomic + iCloud offsite + keep14, daily 03:00 · goal3 · PROGRESS 2026-05-19 Major#4
- [verified] concurrent-write = fcntl.flock app-lock serializes routine/catchup/manual · goal6 · PROGRESS 2026-05-19 Major#3
- [verified] migrate.py = 5 parsers + source_hash idempotent + dry-run/--apply · goal7 · PROGRESS 2026-05-17 #5
- [verified] lesson left base → FUTURE opt-in addon; dashboard Alerts = bug/pipeline-fail only, Open Threads = daily/study/project only · goal4 · PROGRESS 2026-05-19 grill round 4
- [verified] launchd 4 jobs active (diary-routine/catchup/db-backup/jsonl-cleanup); jsonl-cleanup is_headless-driven, currently provable no-op · goal6 · PROGRESS 2026-05-19

## Phase 2 (emotion + recall — converged 2026-05-19, real-source + blind-design)
- [verified] affect granularity = per-episode, Lumi-locked; NOT per-event, NOT per-day · goal3/5 · handover 2026-05-19
- [reasoned] affect schema = (id / date / ep / event_id nullable / valence / arousal / importance / label / entities / source / superseded_by / created_at) + VIEW affect_live (superseded_by IS NULL); date-keyed, deleted+rebuilt in same txn as diary-row on run_day force (no orphan); overturns prior per-event schema AND SCHEMA per-session emotions placeholder · goal3/5 · grill P1 adjudication
- [verified] affect carries a mention-count column; entry formula default weight 0, config-openable · goal3 · A3
- [verified] SessionStart backdrop conveys 4: ① recent few episodes' emotion ② current emotion ③ recent calm-vs-swing ④ emotional-pending (ONLY emotionally-unresolved-between-us; else → open threads); rendered peak-band (top-N recent high-score) + 1 trend line (≤7d weighted); ≤5 lines ≤350 chars; SessionStart total (threads+alerts+backdrop) ≤6000 chars hard · goal3/5 · A1 + handover 2026-05-19
- [verified] emotional-pending → backdrop ④ (emotion); work/study unresolved → open threads, never into emotion · goal3 · handover 2026-05-19 supersedes A2
- [reasoned] entry score decoupled from decay, two formulas two purposes; entry keeps the emotion term; cut Ombre activation_count^0.3 / freshness / archive; importance-led · goal3 · desktop §4 M3
- [reasoned] entry injection = deterministic code template (valence-band × intensity-band lookup → 沉/亮/暖 + 轻/重 + label), NOT LLM dehydrate, NOT diary raw text; SessionStart forbids LLM · goal5/6 · desktop §4 M3
- [reasoned] in-session recall = single weighted scalar fusion, NOT RRF. Normalized: vec=1−cos_dist; bm25=per-query in-group abs(rank)/max; recency=exp(−days/30); affect capped 0.10. Init weights 0.55/0.30/0.15/0.10; MIN_SCORE start 0.35; tuned at build from audit logs. Copy claude-imprint lane eng (FTS5 + sqlite-vec cosine + CJK tokenize + vector BLOB + write-time dedup superseded_by); NOT 3-pool RRF/rerank · goal1/7 · B3 handover 2026-05-19
- [reasoned] decay = read-time lazy weighting + FLOOR tiers (source=override or imp≥8 → FLOOR 0.5 Permanent; 4≤imp≤7 → 0.18; imp≤3 & age>90d & no keyword revive → dormant=1, excluded from entry candidate pool only, Demote-sink); no destructive background job; FTS keyword hit on dormant → clear to 0, revive · goal3 · desktop §4 M5
- [reasoned] recall trigger = BOTH ship (b not optional): (a) deterministic prompt line at backdrop tail = floor (model-pulled cue); (b) UserPromptSubmit deterministic vector fallback = primary (user turn → same bge-m3 search → top-K additionalContext); (b) waits until in-process embedding installed · goal5/7 · B7 handover 2026-05-19
- [reasoned] pipeline = ONE sonnet call (3 layers collapsed; map/stitch deleted, kept only as over-volume fallback, history 0 hits). Output isomorphic: `---` segmented CN prose, then trailing ===AFFECT=== JSON ===END===, one obj per `---` episode (ep/valence/arousal/importance/label/entities/event_hint); prose↔affect decoupled at parse (bad JSON never blocks diary); post FTS5 event_hint→event_id with uniqueness threshold (multi-match→NULL, never first-match) · goal4/6 · grill P1
- [reasoned] refusal sentinel (P0, DESIGN do-not-cut): verified policy-refusal (large plain-EN, stop_reason="refusal", is_error may be false → _parse_claude returns refusal text as success) MUST be detected → treated as failure → 3-stage fallback, NEVER into diary; prior fallback covered JSON-parse-fail only · goal6 · grill P1
- [reasoned] over-volume line = 200K net tokens ≈ 303K chars (real tokenizer: heaviest day 2026-05-18 = 151K, 0.66 tok/char, 1M path verified); pre-call char guard = early-exit to 3-stage fallback + neutral affect + alert; post-call refusal sentinel is the real net (content-type, not chars, is the true signal) · goal6 · token census + grill P1
- [verified] affect skews interesting-positive (DIARY_PROMPT (生动有趣/禁流水账)); Lumi adds corrective clauses herself; bias is her-curated, not a defect to auto-correct · goal5 · Lumi 2026-05-19
- [verified] embedder = bge-m3 1024d IN-PROCESS in the marrow daemon (NOT Ollama; Ollama no longer the recall lifeline, optional later; bge-m3 kept not downgraded), Lumi-locked; events_vec gains embedder-id/dim provenance at recall-module build so a model swap re-embeds without base-schema rewrite · goal1/7 · A1 handover 2026-05-19
- [reasoned] entity emitted IN the single sonnet call (affect-row entities field; no separate chain/LLM); entities/entity_facts merged (kind person|pref|place, append-only superseded_by), built with the recall module, trigger-load, never resident in SessionStart; prior [hold] lifted — blocking pipeline-bug fixed (PROGRESS 2026-05-19 no-p exoneration) · goal7 · C handover 2026-05-19 + Lumi ok
- [reasoned] de-risk (DESIGN do-not-cut) = SessionStart heartbeat fires ONLY on a day that HAD events but NO affect (supersedes >48h/gap-day) → block first line [⚠ (情感记录可能中断): YYYY-MM-DD]; + bad/missing-JSON neutral fallback (V0.5/A0.3/imp3); + idempotent catchup (days w/ events but no affect, self-heals, same code as backfill) · goal6 · B5 handover 2026-05-19
- [reasoned] corrections table = Phase 2 placeholder, design fixed (Fact-corrections conflict priority: Lumi current input > Lumi-confirmed structured > system structured > raw event), not built · goal3 · FUTURE Phase 2

## Portability (cross-phase)
- Portability extends to host-coupled surfaces: storage (MARROW_HOME env + config, never Path.home()), scheduler (interface: launchd today/systemd/cron), notifier (interface: md file/push/webhook), backup (interface: iCloud/S3/B2). Host change = deployment change, never base rewrite.
- Audit 2026-05-21: portable—LLM chain, alert/dashboard md, hooks JSON, SQLite schema. Not yet—Path.home() in config.py/cleanup.py/subpages_render.py, launchd 4 plists, iCloud backup. Cleanup interleaved with phases, not a dedicated phase.

## Doc system (this round C)
- [verified] doc system: DESIGN = goal+structure+hard-constraints+sub-pages (no still-changing decisions); DECISIONS = single current-truth entry, overwrite-in-place, read first; FUTURE = unbuilt plans by phase; PROGRESS = append-only action log; handover = next-session only, overwrite; docs/notes = hard-problem memo / research scratch, NOT a truth source; docs/adr deleted (conclusions folded into DECISIONS); CONTEXT = glossary maintained by grill-with-doc skill, outside this system but its conflicts get fixed each round · goal7 · this round C
- [verified] decision vs process: DECISIONS = current conclusions (overwrite-in-place, reflect only current truth); PROGRESS = historical actions (append-only, "happened" stays true even if later overturned) · goal7
- [verified] convergence discipline: overturn = overwrite in place, never stack; each phase-end a fresh no-context subagent scans DESIGN+DECISIONS consistency, Lumi adjudicates; trace a past decision via events (SessionEnd-cleaned transcript), never raw jsonl · goal7
