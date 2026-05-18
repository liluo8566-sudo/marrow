# Marrow handover

Phase-1 DONE (pytest 129, pushed). Current: grill round 3, emotion branch DONE (ADR-0005), DESIGN rewritten. Next: grill two remaining Phase 2 branches.

## Phase 2 emotion (DONE, ADR-0005)
- No feel layer (diary IS the lived first-person layer)
- emotion = `diary.mood` valence/arousal from diary sonnet call (no new agent, no SessionEnd LLM)
- decay: `score = importance × e^(-λ·days_idle)`, Demote-sink only, lazy
- session-start = one fused-rank recall (recency+arousal+importance); generic recall unbounded
- coord→tag render = opt-in addon
- DESIGN Emotion section + L139/L141, CONTEXT terms, PROGRESS landed

## Phase 2 not yet grilled (2 branches)
- sub-page render config-driven (opt-in/opt-out contract, stellan_wallet first addon)
- people/preferences trigger-load tables
- Method: do not connector-interview; subagent produces blind designs → converge to TWO versions → Lumi adjudicates (trigger: "两支")

## Phase 2 entry
- recall-module (vector + RRF fusion + embedder) — fusion = recency+arousal+importance blend
- #6 events_vec embedder-id/dim provenance: add WITH embedder at recall-module build → FUTURE `events_vec_embedder_provenance` (~/Desktop/NY/CLAUDE.md:5)
- Refs: Ombre-Brain (DESIGN weight-pool), claude-imprint (RRF), cyberboss
- embedder = fork #1 (still open)

## Residual (non-blocking)
- 4 launchd jobs loaded + disk plists restored

## Phase-1 shipped (verified on main, pushed)
- blocker: `is_headless` = assistant model-set ⊆ config `worker_models` (ADR-0004); `entrypoint` abandoned
- #3 diary `--force` overwrite + `fcntl.flock` app-lock
- #4 `backup.py` atomic VACUUM INTO + iCloud + keep=14
- #5 `archive_events` audit_log mirror
- #12 session_end dashboard PermissionError → skip (lossless)
- #2 restored ids 453-456
- #8 timeout = process-group kill (`start_new_session` + `os.killpg`, both llm paths)
- alert/thread/handoff id moved to line-front

## Don't redo / decided
- feel layer NOT a Marrow concept (ADR-0005) — do not reintroduce feel table or session-start dream-write
- lesson NOT a base concept (ADR-0006) — FUTURE addon only, do not re-add to dashboard/Open-Threads/SCHEMA
- DESIGN is overturnable working design, not T&C — override by engineering argument; only uncrossable technical/cost wall is hard constraint
- `entrypoint` NOT a headless marker (ADR-0004); #7 `_routine_target` correct under 04:00 boundary — do not "fix"
- #6 waits for embedder — never add empty provenance column to Phase-1 schema
- `isolation:"worktree"` subagents branch from origin baseline → cherry-pick to main, real-run pytest there
- dashboard lives in ~/Desktop/NY (Obsidian, TCC zone) — EPERM degrade is the fix, not relocation
