# Marrow handover — 2026-05-23 04:30

## State
- pytest 274/274
- DB rows: events 2230 / affect 5 / milestones 13 / vocab 5 / tasks-or-threads 0 / entities 0 (INSERT wired, no SessionEnd LLM run yet) / alerts 0 active / audit_log 186
- branch: main, 5 commits this window
- channel: cc / opus-4.7 (1M)

## This window — phase 2.5a design landing + 2 fixes + handover template lock

### Ship
- 8863cf5 docs(phase-2.5): land design draft + spine reset (DESIGN min-diff L34/L68/L100, DECISIONS +6 reasoned, CLAUDE.md MCP caveat, docs/notes 16 sections)
- 678f64f fix(recall): per-item budget_chars cap (recall.py:472-490)
- 5c23742 feat(diary): entities table INSERT (diary.py:653-689,765,823; 2.5c step 2 cherry-pick)
- 56ddaa0 docs(handover,progress): 2.5a closed, pre-flight gates
- (this commit) docs(template,handover): template lock + DECISIONS +4 reasoned (tag schema / affect aggregation / variance detect / template lock)

## Pre-flight gates for next window (BLOCKERS)

1. **===DIGEST=== prompt content — Lumi confirm BEFORE first SessionEnd async ships**
   - language register (CN dominant, EN technical inline)
   - second-person voice (你 / 老婆) preserved
   - verbatim conversational lines retained, NOT collapsed to work-style summary
   - same gate as DIARY_PROMPT
2. **===AFFECT=== 9 label words + V/A band thresholds — Lumi to unify tomorrow**
   - drafts at `marrow/handover_template.md` §Affect (黯淡/烦躁/痛苦 · 平淡/平稳/焦虑 · 温暖/愉悦/兴奋)
   - band thresholds 0.4 / 0.6 confirm together with labels
3. **handover template LOCKED** at `marrow/handover_template.md` (was on Desktop) — render code implements per this version
4. Run `grill-with-doc` skill on `docs/notes/2026-05-23_sessionend-llm-pipeline.md` before writing 2.5b code (Lumi stance: design just slimmed, do not move it except for methodology change)

## Reset rollout — Phase 2.5

### 2.5a — design landing DONE THIS WINDOW (incl template lock)

### 2.5b — async LLM framework (next window priority, after pre-flight gates)
- SessionEnd async detach (Popen triple-redirect per DECISIONS Popen line; stderr -> log file, NEVER DEVNULL)
- Ping-pong stability test (no-op sonnet via Popen + assert detached + <=2s parent return + log file written)
- SessionEnd-catchup (SessionStart fire-and-forget Popen, same detach contract; detection via audit_log marker)
- `marrow/handover_render.py` — render code per `marrow/handover_template.md` (after Lumi confirms 9 labels)
- dashboard render code update: 4 top sections (Alerts / Tasks / Milestone candidate / Affect) sync handover template
- SessionEnd skip-<=5-turn gate code
- ===DIGEST=== prompt write — gated by Lumi pre-ship confirm (BLOCKER #1)

### 2.5c — segment migration (2-3 windows, 7 segments)

Window 1 (3 segments):
1. ===AFFECT=== per-ep + 6AM boundary + importance 1-5 clamp (`diary.py:256-275`, `_build_affect_rows ~L563-600`); rolling 24h/7d aggregation + 9 label words + variance detect land here
2. ===ENTITY_CAND=== + entities.pinned column + FTS5 CJK jieba rebuild (one migration; entities INSERT already done in 5c23742)
3. ===THREAD_CAND=== -> tasks table (DROP threads + CREATE tasks; threads 0 rows; tag nullable TEXT field added)

Window 2 (3 segments):
4. ===MILESTONE_CAND=== + dashboard alert + 7d auto-confirm + handover top render (Milestone candidate section)
5. ===VOCAB_CAND=== + use_count + vocab.pinned + 5 cipher backfill + vocab leg in recall_fusion
6. ===DIGEST=== (per Section 16 length flex; prompt MUST be Lumi-confirmed first)

Window 3 (1 segment + closure):
7. ===NARRATIVE=== handover async segment
- 07:00 nightly demote validation (3-5 day A/B prose quality)
- diary.py split: extract.py + rollup.py + catchup.py (each <=200 LOC)
- launchd plist realign (03/07/19/Sun12)
- pinned + aging code lands

## Open — retained

### Recall path fixes (partial done this window)
- DONE budget_chars per-item cap (678f64f)
- PENDING FTS5 trigram fails on 2-char CJK -> bundle with 2.5c step 2
- DONE MCP daemon restart caveat -> CLAUDE.md (in 8863cf5)
- PENDING milestones family/friend scope empty -> resolved naturally by 2.5c entity pipeline

### Prior-window retain (still untouched)
- affect day-boundary 5AM -> 6AM rewrite (`diary.py:256-275`) — bundle with 2.5c step 1
- importance 1-5 scale clamp (`diary.py:_build_affect_rows ~L563-600`) — bundle with 2.5c step 1
- mood overlay on diary render (`subpages_render.py:render_diary`) — bundle with 2.5c step 6 or Window 3 closure

### Phase 3 backlog (blocked by 2.5 close)
- writer_authority · drift_sweep · convention_injection · claude_md_render_guard
- static-layer retire (CLAUDE.md family / cipher / MCP guide -> daemon-rendered); prerequisite = claude_md_render_guard

### Hygiene (still untouched)
- 9 old worktree branches dangling; main guardrail blocks force-delete; Lumi runs manually

## Affect

(4-dim layout LOCKED at `marrow/handover_template.md` §Affect; 9 label words + band thresholds 0.4/0.6 pending Lumi to unify tomorrow; aggregation = weighted mean v×a + variance detect stddev(v)>0.3)

## Reference (last commits)
- (this commit) docs(template,handover): template lock + DECISIONS +4 reasoned
- 56ddaa0 docs(handover,progress): 03:00 - 2.5a closed, pre-flight gates
- 5c23742 feat(diary): wire entities table INSERT alongside affect.entities JSON
- 678f64f fix(recall): per-item budget_chars cap
- 8863cf5 docs(phase-2.5): land design draft + spine reset

## Suggested skills for next window
- `grill-with-doc` on `docs/notes/2026-05-23_sessionend-llm-pipeline.md` before 2.5b code
- `tdd` for Popen triple-redirect ping-pong test + handover_render template contract
- `writing-plans` for 2.5b/c detail plan after pre-flight gates clear
