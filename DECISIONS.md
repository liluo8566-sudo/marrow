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
- [verified] emotion data = per-event sparse affect table (id / event_id nullable / date / valence / arousal / importance / label / source / superseded_by / created_at + VIEW affect_live); overturns SCHEMA per-session emotions placeholder AND ADR-0005 diary.mood-only · goal3/5 · desktop plan §4 M1
- [verified] affect carries a mention-count column; entry formula default weight 0, config-openable · goal3 · A3
- [verified] SessionStart emotional backdrop = TWO bands: peak band top-3 single high-score rows + trend band 1 line (weighted aggregate of last ~7d affect); fixed ≤5 lines ≤350 chars; SessionStart total payload (open-threads+alerts+backdrop) held ≤6000 chars hard line, never near 10000 · goal3/5 · A1
- [verified] unresolved = a state flag set/cleared by the 04:00 sonnet reading the whole day's turns (reconciled → clear), NOT time-decayed · goal3 · A2
- [reasoned] entry score decoupled from decay, two formulas two purposes; entry keeps the emotion term; cut Ombre activation_count^0.3 / freshness / archive; importance-led · goal3 · desktop §4 M3
- [reasoned] entry injection = deterministic code template (valence-band × intensity-band lookup → 沉/亮/暖 + 轻/重 + label), NOT LLM dehydrate, NOT diary raw text; SessionStart forbids LLM · goal5/6 · desktop §4 M3
- [reasoned] in-session recall = single weighted scalar fusion (0.55 vec + 0.30 bm25 + 0.15 recency + 0.10 affect bonus capped), NOT RRF; copy claude-imprint lane engineering (FTS5 + sqlite-vec cosine + bge-m3 + CJK tokenize + vector BLOB + write-time semantic dedup superseded_by), do NOT copy its 3-pool RRF + pool rerank · goal1/7 · C2 Lumi-adjudicated
- [reasoned] decay = read-time lazy weighting + FLOOR tiers (source=override or imp≥8 → FLOOR 0.5 Permanent; 4≤imp≤7 → 0.18; imp≤3 & age>90d & no keyword revive → dormant=1, excluded from entry candidate pool only, Demote-sink); no destructive background job; FTS keyword hit on dormant → clear to 0, revive · goal3 · desktop §4 M5
- [reasoned] emotion generation = reuse the 04:00 diary sonnet call; output contract widened from prose to {diary, affect:[{label,valence,arousal,importance,event_hint}]}; code-only post: FTS match event_hint → event_id → insert affect; zero new LLM/link/failure-node · goal4/6 · desktop §4 M2
- [reasoned] embedder = bge-m3 via Ollama 1024d, fallback nomic-embed-text 768d; events_vec gains embedder-id/dim provenance at recall-module build so model swap re-embeds without base-schema rewrite · goal1/7 · desktop §1 + FUTURE
- [reasoned] entities/entity_facts merged (kind person|pref|place, append-only superseded_by); trigger-load, never resident in SessionStart · goal7 · Lumi ok
- [hold] entity pipeline (M6 — 04:00 contract widen for entity / trigger-load impl) suspended pending pipeline-bug fix; this round writes no mechanism · B
- [reasoned] de-risk (goes in DESIGN Safety-nets do-not-cut zone) = SessionStart code heartbeat assertion (latest affect >48h OR a gap day in last 7d → inject block first line [⚠ 情感记录可能中断: YYYY-MM-DD]) + affect bad/missing-JSON code fallback inserts neutral row (V0.5/A0.3/imp3) + idempotent catchup (pick days with events but no affect, missed run self-heals, same code as backfill) · goal6 · desktop §4 M8
- [reasoned] corrections table = Phase 2 placeholder, design fixed (Fact-corrections conflict priority: Lumi current input > Lumi-confirmed structured > system structured > raw event), not built · goal3 · DESIGN Fact corrections

## Doc system (this round C)
- [verified] doc system: DESIGN = goal+structure+hard-constraints+sub-pages (no still-changing decisions); DECISIONS = single current-truth entry, overwrite-in-place, read first; FUTURE = unbuilt plans by phase; PROGRESS = append-only action log; handover = next-session only, overwrite; docs/notes = hard-problem memo / research scratch, NOT a truth source; docs/adr deleted (conclusions folded into DECISIONS); CONTEXT = glossary maintained by grill-with-doc skill, outside this system but its conflicts get fixed each round · goal7 · this round C
- [verified] decision vs process: DECISIONS = current conclusions (overwrite-in-place, reflect only current truth); PROGRESS = historical actions (append-only, "happened" stays true even if later overturned) · goal7
- [verified] convergence discipline: overturn = overwrite in place, never stack; each phase-end a fresh no-context subagent scans DESIGN+DECISIONS consistency, Lumi adjudicates; trace a past decision via events (SessionEnd-cleaned transcript), never raw jsonl · goal7
