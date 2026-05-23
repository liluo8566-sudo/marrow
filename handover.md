# Marrow handover — 2026-05-23 18:30

## State
- pytest 301/301 + 1 manual-skip (5.7s)
- DB rows: events 2230 / affect 5 / milestones 13 / vocab 5 / tasks-or-threads 0 / entities 0 / alerts 0 active / audit_log 188 (post-pollution-cleanup; 2 live sessionend_extract ok rows from tonight's ping-pong)
- branch: main, 9 commits this window (framework x4, render x4, fixture fix x1)
- channel: cc / opus-4.7 (1M)

## This window — phase 2.5b async LLM framework + render layer DONE (DIGEST deferred)

### Ship — framework
- 6b52f12 feat(popen) — popen_detach §3 4-flag hard contract (stdin=DEVNULL / log_path append / start_new_session / close_fds)
- 01cb963 feat(sessionend) — sessionend_async ping-pong + skip<=5-turn gate + idempotent audit (action=sessionend_extract, summary=ok|fail:<E>|skip:short_session)
- 8d0ebfd feat(catchup) — sessionstart_catchup pending-sid detection via `events DISTINCT − audit`
- 0647b18 feat(hooks) — wire SessionEnd async fire + SessionStart catchup, `[sessionend].skip_turn_threshold=5` config

### Ship — render
- c090c30 feat(top-sections) — shared renderer for alerts/tasks/milestone/affect (205 LoC); 9-tone v×a×imp aggregation, stddev>0.3 variance label, ephN/eplN, Pending body empty until 2.5c affect.unresolved column
- e3549c9 feat(handover) — handover_render.py atomic write to `~/.config/marrow/handover.md` (sync skeleton ≤500ms, narrative-pending stamp, template L48 drift fix)
- 7d90b75 feat(hooks,dashboard) — wire handover_render + dashboard top swapped to 4-section template (Alerts / Tasks / Milestone candidate / Affect); old Open Threads / Sub Pages nav dropped (subpages still rendered via subpages.write_all_subpages)
- fd8b4c3 fix(tests) — test_dashboard alert format align

### Ship — pollution fix
- 93ee49b test(fixture) — tests/conftest.py autouse no-op for hooks.popen_detach. Root cause: fixture's `monkeypatch.setattr(config, "db_path", ...)` patches in-process only; the popen_detach child subprocess loads REAL config and writes to `~/.config/marrow/marrow.db`. 1143 polluted sessionend_extract rows + 93 sessionend_async_*.log files manually purged. Re-ran full suite — prod DB clean.

### Live verification (P9 hook-isolation contract verified in subprocess context)
- Foreground: `python -m marrow.sessionend_async --sid c3da9d7a-3385-4474-849c-cb2a6ed15347` → audit `ok` at 08:21:25Z, wall 3.2s
- Fire-and-forget: `popen_detach([...sessionend_async --sid 45dace23-161c-4678-8ed2-a57f138cf76d])` → parent return **1.3ms** (<<2s §3 budget), child wrote audit `ok` at 08:21:52Z (~30s post-spawn)
- Both ping-pong calls hit real sonnet with CN body containing PreToolUse-trigger pattern → returned clean text; isolation flag (`--setting-sources "" --strict-mcp-config`) holds end-to-end

## Pending — Lumi morning review (BLOCKERS for 2.5c)

> Plan landed at `docs/notes/2026-05-23_diary-rewrite-plan.md` (335 LoC). Four explicit decision points below — everything else in the plan is locked.

1. **L3 — P7 CN exclude line for DIARY_PROMPT** — write the one-liner that filters coding/debug arguments out of diary. Plan §0 has the slot.
2. **L6 — DIGEST prompt body** — full content, or your OK to ship empty placeholder. Same gate as DIARY_PROMPT (pipeline §16.2). Without this, sessionend_async stays in ping-pong-only mode; 2.5c segment migration cannot start.
3. **L2 — reconcile_prev EN polish** — read the draft wording in plan §3 (AFFECT contract field), polish if needed; semantics already locked.
4. **plist shim vs rename** — keep `diary.py` as a thin shim (recommended by plan author) vs rename launchd plists to `marrow.catchup`. Plan §8 step 10 has both paths.

P1 (handover_template §Affect paste) and P4 affect schema migration land with the diary.py rewrite, NOT independently — bundled in plan §4.

## 2.5b leftover (small, can ship next window)

- **P2 — Lumi self-writes Affect legend in `~/.claude/CLAUDE.md`** — quick reference for new sessions to read dashboard semantics. Same drop as before.
- **Pending tick reverse-lookup** — handover_render currently renders Pending block empty; the `- [ ]` checkbox + aid HTML comment + file watcher lands with diary.py rewrite (needs affect.unresolved column).
- **`pytest.mark.manual` registered in pyproject** (done in this window) — manual live test gated by `PYTEST_RUN_MANUAL=1` env var.

## Reset rollout — Phase 2.5

### 2.5a — DONE (prior window)
### 2.5b — DONE this window (framework + render + fixture fix + live verify); only DIGEST prompt + Pending tick deferred
### 2.5c — blocked on Lumi morning review (4 questions above)

Window 1 segments still as designed (`docs/notes/2026-05-23_sessionend-llm-pipeline.md` §12):
1. ===AFFECT=== per-ep + 6AM boundary + importance 1-5 clamp
2. ===ENTITY_CAND=== + entities.pinned column + FTS5 CJK jieba rebuild
3. ===THREAD_CAND=== → tasks table (DROP threads + CREATE tasks; threads 0 rows)

Window 2 / Window 3 unchanged.

## Open — retained

### Recall path
- PENDING FTS5 trigram fails on 2-char CJK → bundle with 2.5c step 2
- PENDING milestones family/friend scope empty → resolved naturally by 2.5c entity pipeline

### Prior-window retain (still untouched)
- affect day-boundary 5AM → 6AM rewrite — bundle with 2.5c step 1
- importance 1-5 scale clamp — bundle with 2.5c step 1
- mood overlay on diary render — bundle with 2.5c step 6 or Window 3 closure

### Phase 3 backlog (blocked by 2.5 close)
- writer_authority · drift_sweep · convention_injection · claude_md_render_guard
- static-layer retire (CLAUDE.md family / cipher / MCP guide → daemon-rendered); prerequisite = claude_md_render_guard

### Hygiene
- 9 old worktree branches dangling; main guardrail blocks force-delete; Lumi runs manually

## Carryover scratch
- `~/Desktop/brainstorm-future.md` — 10-section future-features brainstorm (addon contract / wallet MCP split / iOS path / active-device routing / chord-progression from 和弦 / imprint borrows / cccompanion fork). 3 items in FUTURE Phase 5; 9 pending (待加).

## Affect

(4-dim layout LOCKED at `marrow/handover_template.md` §Affect; rendered to `~/.config/marrow/handover.md` by handover_render.write_handover at session_end; Pending body empty until 2.5c affect.unresolved column lands.)

## Reference (last commits)
- 93ee49b test(fixture) — autouse no-op for hooks.popen_detach
- fd8b4c3 fix(tests) — test_dashboard alert format
- 7d90b75 feat(hooks,dashboard) — wire handover_render + dashboard 4-section swap
- e3549c9 feat(handover) — handover_render sync skeleton + template drift fix
- c090c30 feat(top-sections) — shared renderer alerts/tasks/milestone/affect
- 0647b18 feat(hooks) — wire SessionEnd async + SessionStart catchup
- 8d0ebfd feat(catchup) — sessionstart_catchup
- 01cb963 feat(sessionend) — sessionend_async ping-pong + skip gate + idempotent
- 6b52f12 feat(popen) — popen_detach §3 contract

## Suggested skills for next window
- `tdd` for the diary.py rewrite (Lumi-approved batch P1-P9 landing as one migration + extract.py/rollup.py/catchup.py split)
- `writing-plans` only if Lumi wants per-step task breakdown after plan approval
- `/rr` after 2.5c Window 1 closes (3 segments)
