# Marrow handover — 2026-05-22 00:10

## Phase 2 status: DONE
- All 5 handover priorities from 2026-05-21 23:45 cleared.
- main has: refusal sentinel + sub-page render + diary single-call + recall (bge-m3 + fusion) + SessionStart affect backdrop/heartbeat + UserPromptSubmit→recall wire.
- 234/234 pytest green.

## This session shipped
- `/rr phase 2` adjudication → 8 main fixes (CJK tokenizer / events_vec dim alert / config deep merge / CN refusal fingerprints / atomic hash / session_end PermissionError alert / test fixture date / speaker labels). `38ba644`.
- Speaker-label fix for diary pronoun POV (root cause: `[user]/[assistant]` collided with sonnet identity → flipped 屿忱/念念). Applied main + worktree-A.
- 3 worktree merges: C `b3aacda` (0 conflicts), A `c61f0ab` (auto-merged 2 files), D `25ad192` (2 conflicts kept-HEAD: storage.py FTS rebuild + dim alert / config.default.toml [embedding].model).
- D's UserPromptSubmit stub wired to C's `recall.recall_fusion`. `2ff1875`.
- Portability cross-phase note in DECISIONS + DESIGN goal 1 rewrite (Lumi-authored, committed alongside). `9869427`.

## User action needed
- `~/.config/marrow/config.toml` has `[recall] vector = false` (predates this phase). Flip to `true` to actually run vector recall on UserPromptSubmit. Config now deep-merges defaults under user toml, so all `w_*` / `min_score` keys land automatically; only the boolean needs editing.

## Open / deferred (FUTURE candidates)
- diary.py worktree-A is 785 LOC (single-call + 3-stage fallback duplication); soft cap 300. Defer to simplify pass.
- subpages.py + subpages_render.py duplicate `_MARKER_*` constants. Defer.
- llm.py `_log_usage` opens new DB conn per LLM call (smell under WAL, not bug). Defer.
- SCHEMA.md root file is pre-Phase-2 stale (still lists retired emotions/people/preferences/dir). Doc-only.
- DESIGN.md L190 heartbeat ">48h OR gap-day" line is stale (DECISIONS L37 supersedes with gap-day-only). Remove next doc pass.
- mood labels in `marrow/hooks.py:28-29` are CN (沉/暖/亮/轻/重). Old handover decision flagged EN but rationale unclear; CN reads fine in the SessionStart backdrop for a CN-major user. Leave unless Lumi requests EN.
- Locked worktree branches still in `.claude/worktrees/`. Safe to clean up: `git worktree remove --force <path> && git branch -D <branch>` for A/C/D once merges are confirmed live.

## Next session
- Decide Phase 3 scope. Options worth grilling: cross-channel parity (WeChat/CLI thread continuity), workflow carry-over (where I left off / next step survival), or sticker/vocab learn loop. Run `grill-with-doc` to converge before code.
- `~/.claude` has a local-only hook commit (`21094cd`, prompt-lint per-key flag) per global rule — never push.

## Reference
- `docs/notes/review-phase-2.md` — /rr findings + decisions
- `.claude/rules/build-workflow.md` — /rr usage
- `.claude/rules/agent-dispatch.md` — delegation policy
