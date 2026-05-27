# Today — 2026-05-27 Wed evening

## Principle
- Keep going until the goal is truly achieved.
- If user-like verification is possible, run it before reporting.
- The only standard of goal verification is whether it works in practice. Tests and dry runs are just safeguards.

## Dispatch Policy (read first)
- Strictly follow agent-dispatch.md
- You are the orchestrator — dispatch tasks to agent or wt and keep context clean. 
- You can ask questions if not sure but no need to ask if you know the optimal answer.
- You decide agent count and agent type (follow agent-dispatch.md and less Opus)

## Session 1 (main) — Phase 3 bug cleanup
**Goal**: clear alerts + mm+ both modes work + study/pit two-way landed

Steps:
- TDD two red lines first
  - active sid + events=0 + `reset:mm_plus` marker → NOT skipped by short_session
  - reopened sid retry → covered by `_already_done` path (regression guard)
- Patch `sessionend_async.py:286` short_session early-exit to honor reset marker (mirror `:76`)
- study index gets an inserter spec (mirror `build_projects_index_spec`); unit detail stays render-only
- Lift pit out of `SUBPAGE_BUILDERS`: no more render, no more writes to `pit` table; projects index keeps `[[projects/pit|...]]` link; export current `pit` rows to `~/.config/marrow/db-pages/projects/pit.md` once, then retire the table
- watcher allowlist: subpage detail md (`study/<unit>.md`, `projects/<name>.md`, `projects/pit.md`) skips md_index
- Sweep git status backlog (M/D/??) into commits

Done:
- `pytest -q` exit 0
- `dashboard.md` Alerts no longer contains #114 #115
- mm+ on an active session reruns and writes events
- `pit.md` hand-edit survives a render cycle (echo a line, run refresh, verify)

Dispatch:
- Explore agent: `build_projects_index_spec` structure + current `pit` row count + watcher path filtering state
- worktree-implementer agent: TDD red lines + patches above; main session reviews diff and merges

## Session 2 (planning only — no implementation this window)
Design locked. Detail moves to DECISIONS / DESIGN / FUTURE.

Implementation split, dispatched in new sessions:
- S2a — drift_sweep `.claude` white/blacklist + `DriftWatcher` attach to `watcher.py`
- S2b — sync loop (10s tick, md↔db bidirectional, all subpages)
- S2c — atlas subpage (schema + render + reconcile + depth-aware fs walk)

Order: S2a → S2b → S2c (atlas depends on sync loop).

Pending-confirm: `~/cc-lab/external/` 重排 + shared `.claude` symlink scheme.

## Session 3 (main + brainstorming) — NY memm retire sync
**Goal**: memm fully offline + code transfer paths decided

Steps:
- Decide with Lumi: which plists to unload (memm pipeline / curator / rotate / monitor)
- Which skills to delete (summ / ss / goose-slim / carryover-load)
- `~/Desktop/NY/code/` — archive / migrate to marrow / drop
- Lumi clears NY folder while main session unloads plists + deletes skills

Done:
- `docs/plans/ny-retire.md` checklist landed
- plist unload + skill delete + code archive executed

Dispatch:
- Explore agent: ny- prefixed plists under `~/Toolkit/scripts` + ny-relevant skills under `~/.claude/skills/`
- main session executes plist unload + skill delete after Lumi confirms each batch

## Session 4 (main + brainstorming) — Phase 4 WeChat full-chain direction
**Goal**: cyberboss evaluation + recommended path + FUTURE Phase 4-5 rewrite

Steps:
- Pull cyberboss source, map architecture
- Map against current weclaude pain points: multi-msg merge / `/stop` / `/rewind` / media / group chat / bridge long timeout / permission approval
- Decide: replace entirely with cyberboss vs modify cyberboss vs rewrite from scratch
- Mark every uncertain item TBD (Lumi 2026-05-24 rule — no wrong info in docs)

Done:
- `docs/plans/phase4-direction.md` (evaluation + path + effort)
- `FUTURE.md` Phase 4-5 section rewritten

Dispatch:
- fetcher agent: cyberboss repo README + key module list
- general-purpose agent with web: any feature Lumi names that needs current docs lookup

## Constraints
- Concurrent ≤ 3
- Hold: cheatsheet / housekeeping / placement_rules.toml (cheatsheet recall lane design parked in FUTURE.md, ships after P4)
- Pit recall lane (vec + force_include) deferred — Lumi pending decision; today's Session 1 still retires the `pit` table, so the future lane will use a fresh `pit_entries` table without conflict
- mm+ feedback already in (Lumi screenshot), no further wait
