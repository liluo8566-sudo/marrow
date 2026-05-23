# Marrow handover — 2026-05-24 03:00
> Fixed-name overwrite, never delete. Keep points not touched this window.

## State
- pytest 319 passed + 1 manual-skip (6.19s)
- main 3 commits ahead origin (push pending Lumi nod) — `3e9bd0b` cleanup retire / `fa54662` candidate split + sessionend 4-to-1 call + vocab pinned / `8f97420` aging + schema v3 status
- channel cc / opus-4.7 (1M)
- schema v3 — vocab.pinned (LLM-written by VOCAB_CAND) + vocab.status (code-written by aging job)

## This window — 2.5d closeout

### Done
- task 1 — deleted `marrow/cleanup.py`, `tests/test_cleanup.py`, `deploy/mw-jsonl-cleanup.plist`; added `cleanupPeriodDays=30` to `~/.claude/settings.json` (worktree-A merged → `3e9bd0b`)
- task 2 — `marrow/aging.py` (185 LoC, weekly): vocab promote / demote / task auto-archive / milestone alert auto-confirm. Single txn + audit_log row. `enforce_anchor_pins` reads `candidates.VOCAB_ANCHOR_KEYS` (single source: (鸭子/念念/老公/老婆/Lumi/屿忱/Stellan) + type='cipher'). `deploy/mw-aging.plist` Sun 12:00, plutil OK, not yet launchctl loaded. schema v3 +status column. 25 new tests (worktree-B merged → `8f97420`)
- task 3+4+5 — shipped by neighbor as `fa54662`: `sessionend_prompts.py` rewritten (1 SESSIONEND_PROMPT to 4 marker blocks); `sessionend_async.py` refactored (1 sonnet to per-block writer + audit ok/partial/fail); new `candidates.py` (extract_block + 3 writer + VOCAB_ANCHOR_KEYS single source); new `daily_prompts.py` (DAILY_CAND_PROMPT to 3 block); `daily.py` `_extract_candidates()` before DIARY, LLMError warn-only; storage v3 +pinned + type='cipher' force-pin=1, pinned upgrade-only; fixed latent bug (old `_seg_milestone_cand` used `source` column but milestones only has `source_hash`)
- DESIGN:98 + DECISIONS:18 4-jobs list refreshed
- PROGRESS 2.5d closeout entry appended

### Live sessionend test (neighbor sid `94d7be2e-...`, fired 02:47-02:48)
- 4 marker blocks audit-ok
- DIGEST 519 chars landed in `session_digests` (2026-05-23 date row)
- **`~/.config/marrow/handover.md` ThisSession/NextSession empty, stamp still `pending sid:sid-hook-test`** — Bug #1 below

## Next window

### Bug #0 — recall outlet starves on default limit, ignores entity table (P0, blocks "memory exists" trust)

Acceptance: (李小云) / (大龙虾) queries return ≥10 of 12 known events at SessionStart `## Recall (auto)`. Blocks all backlog items.
- Symptom: Lumi mentioned (李小云) ~12 times over 5/19-5/22, auto recall surfaces only 3 (75% missing). (大龙虾) 2 events, 1 surfaced.
- Raw evidence (sqlite events_fts MATCH): (李小云) 12 distinct events; (大龙虾) 2.
- Write + FTS index path verified healthy. Failure is the OUTLET.
- Root causes:
  - `marrow/recall.py:511` default `limit=5`.
  - `marrow/recall.py:467` `ms_cap = max(1, (limit+2)//3)` gives milestones 2 slots; events get 3 (`:469`). Caps total event surfacing at 3 regardless of FTS hit count.
  - `[embedding].model = ""` in config.toml — bge-m3 not loaded; fusion degrades to FTS+recency only (DECISIONS Phase 2 locks bge-m3 1024d but never installed).
  - entities table has `mention_count` column but recall does NOT join on entity → never proactively surfaces history when current prompt contains an entity name.
- Fix direction (priority order):
  - (P0 cheap) default `limit` 5 → 15; drop `ms_cap` to 1 (or 0) when FTS strong-hit count > N; raise per-item budget cap accordingly.
  - (P0 design) entity-aware recall: when current prompt contains an entity name (FTS-match entities.name + aliases), JOIN entity_id → events directly, force-include all matches in top-K, bypass fusion scoring.
  - (P1) enable bge-m3 embedder so vector lane stops being dead weight; events_vec already has embedder-id/dim provenance slot reserved (FUTURE: events_vec_embedder_provenance).
  - (P1) use entities.mention_count as a recency/frequency booster in fusion weight.

### Bug #1 — handover render race (high priority, blocks dev-brief retire)
- `handover_render.write_handover()` at SessionStart does full-file overwrite of `~/.config/marrow/handover.md`: reads empty template → renders top + Reference + stamp → atomic_write whole file. Overwrites the ThisSession/NextSession content that the previous sessionend just wrote.
- Evidence: prev sessionend 02:48:46 audit ok → this SessionStart 02:49:43 handover.md mtime → ThisSession/NextSession blank, stamp `pending sid:sid-hook-test`.
- Fix options:
  - (a) sessionend writes skeleton + ThisSession/NextSession atomically together; SessionStart reads + injects only, never writes.
  - (b) SessionStart writes skeleton but preserves existing non-empty ThisSession/NextSession blocks.
- Until fixed: hand-written `~/cc-lab/marrow/handover.md` (this file) is authoritative. Retire gate = bug fixed + 1 real session verified.

### Bug #2 — dashboard tasks polluted with marrow-internal coding work (Lumi flagged)
- Symptom: `Completed [16]` and `To-Do List [9]` on dashboard are full of marrow implementation items (merge worktree / implement sessionend / fix DIGEST / rename mw-diary plist / etc.) — Stellan/marrow dev steps, NOT Lumi's real tasks (study / work / project goals / life).
- Root cause hypothesis: TASK_CAND prompt extracts from session chat without scope filter; dev sessions where Lumi+Stellan discuss marrow implementation get every implementation step ingested as Lumi tasks.
- Fix direction:
  - prompt-side: TASK_CAND prompt rewritten — explicit "extract only Lumi's real-life intent: study, work shift, GAMSAT, life errands, project external goals. EXCLUDE: marrow/cc/Stellan/LLM implementation steps, debugging tasks, refactor units, anything that lives inside a coding session."
  - schema-side (optional safety net): tasks add `source` column ('lumi_intent' / 'marrow_dev' / 'external_import'); dashboard renders only 'lumi_intent'; marrow_dev still audit-loggable but hidden.
- Improvement (tick UX): currently dashboard tick appears to strike-through only; what Lumi wants — tick on a Todo row → auto-move that row into Completed section on next render (status: active → done via anchored-row bidirectional sync). Needs `top_sections.render_tasks` + reconcile to honor tick as a done signal.

### Bug #3 — Affect renders only emotion tags, no events (Lumi flagged)
- Symptom Today line: ((痛苦) · ep2h (释然) | (释然) · ep2l (震惊) | (震惊)) — tones only, plus opaque ep2h/ep2l codes, no subject/event text.
- Symptom This Week line: ((专注 → 痛苦) · (编记忆) · (晚安吻) · (骑豹剧) · (释然)) — mixes events and a tone in one bullet without separation.
- Inconsistent: Today vs This Week formats diverge.
- Fix direction:
  - AFFECT prompt: confirm it emits `subject` / `cause` (event anchor) per row; if yes, plumb to renderer.
  - `top_sections.render_affect`: unify Today / This Week / Pending — `[tone] · <event1> · <event2> · ...` consistent across all three; drop ep2h/ep2l opaque codes from surface (keep in DB).
  - Lumi format spec needed: how she wants tone transition (主→转) visualised vs flat tone list.

### Phase 2 Lumi-owned closeout (3 items + 1 new)
- `dashboard_v2_redo` — top block redesign + milestone one-click pin / task delete / cross-subpage anchors / format unify
- `milestone_format_unify` — `[YYYY-MM-DD] subject: description (50-100w / 2-3 sentences)` unify dashboard + milestone.md; drop theme field from render
- `subpage_redo` — full subpage layout redesign (study / projects / milestone / mood treated as scaffolding)
- **NEW — dashboard top free-form fix**: outside marker = Lumi notepad (marrow untouched); anchored row = bidirectional sync (Lumi wins); free-form inside system block = wiped on render. Lumi does this together with `subpage_redo` / `dashboard_v2_redo`.

### Phase 3 backlog (blocked by 2.5 close + Phase 2 Lumi-owned)
- writer_authority · drift_sweep · convention_injection · claude_md_render_guard
- static-layer retire (CLAUDE.md family / cipher / MCP guide to daemon-rendered); prereq = claude_md_render_guard

### Operational
- aging plist NOT yet launchctl loaded — Lumi load: copy `deploy/mw-aging.plist` to `~/Library/LaunchAgents/`, then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/mw-aging.plist`
- all worktrees prunable per Lumi (2026-05-24 02:55): both A/B from this window + 9 dangling old ones. Run `git worktree list` then `git worktree remove <path> && git branch -D <branch>` per entry.

## Pending — retained

### Lumi self-writes
- `~/.claude/CLAUDE.md` Affect quick-reference legend (P2)

### Carryover scratch
- `~/Desktop/brainstorm-future.md` — 10-section future features (3 items Phase 5; 9 pending)

## Reference (this window's commits)
- 8f97420 feat(aging): weekly maintenance job + schema v3 status column
- fa54662 feat(phase-2.5d): candidate split + sessionend 4-to-1 call + vocab pinned
- 3e9bd0b chore: retire marrow/cleanup.py — cc cleanupPeriodDays owns jsonl retention
