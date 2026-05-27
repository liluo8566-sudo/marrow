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

---

## Handover — 2026-05-28 03:00–04:10 atlas round 2

### Done (4 commits on main)
- `9247de9` feat(atlas): root section `## [name/](file://)` now carries marker + `note/write/naming/depth` fields, so depth on the root collapses/expands the whole subtree. `AUTHORIZED_ROOTS` `.config/marrow` → `.config`; new `CONFIG_BLACKLIST = {wechat-claude-bridge}` skips credential / chat-dump siblings in both atlas walk and drift_sweep rg + python ref scans. `atlas_sweep_fs` purges rows for blacklisted paths so stale rows from older sweeps self-clean. `reconcile_atlas` always triggers sweep. `subpage_specs.build_atlas_spec` caches root rows for `render_section_header` + forces canonical roots into `section_order`.
- `6c8208b` fix(atlas): sweep retract now walks every atlas row as candidate (not just `depth>0` seeds), so root depth 1→0 actually retracts stub-only children. `manual_fields` (note/write/naming non-empty) + `depth>0` survive.
- `2967a86` fix(sync): `_atomic.atomic_write` content-equality guard (skip `os.replace` when bytes match) + `sync_loop._process` db→md branch drops the 1s epsilon. Breaks the deadlock where every prior render pushed `md_mtime` within 1s of `db_mtime` → next tick "within epsilon — skip" → md frozen for minutes.
- `84f2ba3` fix(atlas): drop `protected=md_paths` shield in sweep. md-listed stub-only rows were being saved from retract, leaving a fixed-point (md stub → reconcile upsert → sweep skip → render no-op). Now only `has_manual` survives.

### Live verified
- `cc-lab depth=1 → 0` collapses the whole cc-lab subtree within ~5s (db drops from 23 rows → 1; atlas.md re-renders with cc-lab section header only)
- atlas.md mtime advances on every real db change instead of freezing for minutes
- pytest 760/762 pass (2 fail = unrelated `test_hooks_mm_prefix.py` mm- behaviour, Lumi changed `hooks.py` test never updated)

### Open bugs (NOT fixed)

**1. root depth=0 does not cascade through nested `depth>0` rows**
- Repro: set `.config` root `depth: 0`. Expected: whole `.config` subtree collapses. Actual: `.config/marrow` row has `depth: 1`, sweep retract skips it because `p_depth > 0`, then its layer-1 stubs (db-pages / state / scratch / ...) are spared because they're "covered" by `.config/marrow|1`.
- Live db state: `~/.config/marrow|1` still in atlas table after reconcile, with one layer of children.
- Design tension: user setting `.config depth=0` means "fold the whole branch" — root depth should dominate over descendant depth. But descendant depth was user-set too, can't be silently overridden either.
- Fix sketch: retract logic walks the ancestor chain; if ANY root ancestor (path in `AUTHORIZED_ROOTS`) has `depth=0`, retract every stub-only descendant regardless of intermediate `depth>0` (manual-field rows still survive). Mid-chain non-root rows keep their depth signal; only root-level zero is the global kill switch.
- Touch points: `marrow/atlas.py::atlas_sweep_fs` retract loop (around line 510-555).

**2. orphan path `/Users/Gabrielle/Library/Application Support/iTerm2` lives in atlas db**
- Path is outside every `AUTHORIZED_ROOT`. `_root_of()` returns None → `section_of()` falls back to path itself → it renders as its own section at the bottom of atlas.md with no canonical parent.
- Source unknown — sweep can't have written it (walk is bounded by AUTHORIZED_ROOTS). Likely historical hand-insert or a now-removed AUTHORIZED_ROOTS entry that was never cleaned up.
- Fix: one-time cleanup `DELETE FROM atlas WHERE path NOT LIKE '<each authorized root>%'`. Optionally: reconcile/sweep adds a guard that drops any row whose path is not under an AUTHORIZED_ROOT (defensive — paths can't appear there otherwise).

**3. dir-rename drift not alerted (`grill → grillme` case)**
- Lumi renamed `~/.claude/skills/grill/` → `~/.claude/skills/grillme/` deliberately to test drift_sweep. No alert fired; `mw drift apply` has nothing pending for it.
- Root cause: `DriftWatcher` exists at `drift_sweep.py:493` but `watcher.py` only wires file-level rename events to it. Dir-rename → watchdog `on_moved(is_directory=True)` is not subscribed. handover notes already had this flagged as "DriftWatcher 没接 watcher.py — watchdog rename 接入是真欠账".
- Fix: extend `watcher.py` DriftHandler (or equivalent) to handle dir `on_moved` — for each affected child path under `src`, queue a rename event so `drift_sweep.handle_move` can scan refs. Watch out for noisy events from worktree creation / `.pytest_cache` churn — apply existing `EXCLUDE_DIRS_*` filtering.

### Open alerts
- #117 / #123: `drift ready: SKILL.md → SKILL.md` — Lumi to `ls ~/.config/marrow/drift_pending/` and decide `mw drift apply <pid>` or reject. Both look like same-name modifies misfired as moves.
- #116: `subpage 'atlas' build failed: no such table: atlas` — historical (atlas table exists now); manual `UPDATE alerts SET resolved=1 WHERE id=116`.

### Python 3.14 SIGBUS
- Still unresolved per Marrow handover. Lumi to `brew reinstall python@3.14` or rebuild venv on 3.13 to stop the recurring watcher crashes. launchd keepalive masks the issue but each crash is a real fault.

### Reference
- `marrow/atlas.py` — render `_section_header(row=...)` + `_walk_collect` blacklist + retract walking all rows
- `marrow/subpage_specs.py:417,448` — root row cache + canonical section emission
- `marrow/drift_sweep.py:30,51,149,194` — AUTHORIZED_ROOTS / CONFIG_BLACKLIST / rg + python fallback
- `marrow/sync_loop.py:200-217` — epsilon removed (db→md strict `>`); md→db keeps 1s jitter guard
- `marrow/_atomic.py:6-27` — content-equality guard
