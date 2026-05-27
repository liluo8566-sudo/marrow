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

## Session 2 (main) — atlas subpage + drift_sweep .claude scope
**Goal**: atlas subpage replaces dir_tree; drift_sweep watches rule-class only; rename auto-propagates

Decisions locked (this session):
- dir_tree → retired, replaced by `atlas` subpage (path map, depth-aware, manually editable)
- cheatsheet stays parallel subpage (skill triggers / mcp / cli) — separate from atlas
- naming rules: keep `~/.claude/rules/files.md` as interim; sink into atlas `naming` field next phase
- write-location lookup: read-on-demand from `atlas.md`, rule line in `rules/files.md`; no UserPromptSubmit injection

Atlas schema (table `atlas`):
- `path TEXT PRIMARY KEY` — absolute, expanded
- `note TEXT` — what this dir is
- `write_hint TEXT` — what belongs here / what doesn't
- `naming_hint TEXT` — naming convention for this dir
- `depth INTEGER DEFAULT 0` — sweep expand depth per row (0=this only, 1=one sub-level, etc.)
- `stale INTEGER DEFAULT 0` — 1 if fs walk no longer finds this path
- `updated_at TEXT` — iso timestamp

Atlas render shape (markdown heading tree, `##` per monitored root → `###`/`####`/... per dir depth, max `######`):
```
## ~/cc-lab/
### marrow/
- note: SQLite-backed memory system
- write: docs/plans/, docs/notes/
- naming: mw- prefix outside repo, snake_case in repo
- depth: 1
```
Empty fields omitted. `(stale)` suffix on path heading when stale=1.

drift_sweep `~/.claude` subtree filter:
- Whitelist (sweep + watch): `CLAUDE.md`, `rules/`, `commands/`, `skills/`, `agents/`, `output-styles/`, `hooks/`, `keybindings.json`, `settings.json`
- Blacklist (skip): `projects/`, `image-cache/`, `statsig/`, `shell-snapshots/`, `paste-cache/`, `file-history/`, `sessions/`, `daemon/`, `session-env/`, `*.jsonl`, `*.log`

Watchdog wiring:
- `DriftWatcher` (drift_sweep.py:493) currently standalone — attach to `marrow/watcher.py` observer
- `on_moved` event for `.claude` whitelist + atlas-monitored roots → trigger 30s batch sweep (existing) → reconcile atlas path keys (note/write/naming follow the path)

Atlas sweep loop (depth-aware fs walk):
- For each row: walk `path` to depth `depth`, upsert child rows (note empty) if missing, mark vanished children as `stale=1` (preserves manual fields)
- Triggers: `mw refresh` (existing CLI), watcher md edit, FSEvents rename

Done:
- `marrow/atlas.py` (or split into storage migration + subpage_specs entry) + `_REGISTRY` registration
- `tests/test_atlas.py` covers: render heading layout, depth controls sub-expansion, reconcile preserves manual fields under fs rename, stale flag on missing dir
- `drift_sweep.py` `.claude` subtree filter live + `DriftWatcher` attached to watcher
- `~/.config/marrow/db-pages/atlas.md` produced by `mw refresh`, ≥5 root sections (cc-lab, .claude, Study, NY, Toolkit)
- `pytest -q` exits 0
- `docs/plans/today.md` (this file) reflects above
- `rules/files.md` adds one-line: write new file → `read atlas.md first`

Goal (machine-checkable):
- pytest -q exit 0
- `grep -F "atlas" marrow/subpages.py marrow/subpage_specs.py` hits both
- `grep -F "DriftWatcher" marrow/watcher.py` hits (observer attachment)
- `ls ~/.config/marrow/db-pages/atlas.md` exists, contains `## ~/cc-lab/` and `## ~/.claude/`
- `git log --oneline -10` shows ≥3 atomic commits this session

Dispatch:
- WT1 (worktree-implementer Sonnet): drift_sweep `.claude` subtree filter + `DriftWatcher` attach to `watcher.py` observer + tests
- WT2 (worktree-implementer Sonnet): atlas storage migration + subpage_specs + reconcile + depth-aware sweep + tests
- Main: serialize storage migration first (schema), then WT1 + WT2 in parallel; merge in report order

Pending-confirm (Lumi must point before move):
- `cc-lab/external/` 重排 — physically `mv claude-buddy image-gen-mcp weclaude` into `external/`, update workspace
- Shared `.claude` symlink scheme (cc-lab/.claude as single source, project .claude/rules symlinks back)

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
