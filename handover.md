# Marrow Handoff — 2026-05-15 (next window)

Grill round complete. All decisions committed + pushed. Private repo: github.com/Jaynechu/marrow. Read CLAUDE.md first (it lists when to read what). Do not re-derive. Delete this file after use.

## Next window task

- FUTURE.md sweep: ~80 items, agent-scraped from old _pit/roadmap. Judge each: still-applies-to-marrow / superseded-by-rebuild / dead. One commit. Do before Phase 1 build.

## Pending Lumi decision (parked — do not action)

- archive/ history rewrite: archive/ untracked + gitignored (no-loss done), but DESIGN-original.md (personal health/identity fragments) still in commit ae23fc4 on GitHub. Full removal = force-push history rewrite. Lumi said leave it for now.

## Architecture — decided this session

- Build Marrow inside ~/cc-lab/marrow (alias: mm). Persona/relationship continuity is global (~/.claude/CLAUDE.md), loads in any dir.
- Do NOT import old ny-memm docs (rule, system_guide, manual, roadmap) into Marrow context.
- Work continuity = CLAUDE.md + PROGRESS.md + git log. Not 3d/10d.
- 3d/10d/reference/timeline all still exist in ~/Desktop/NY/memory/ (not deleted). They are migrate.py sources. Do not delete; just do not load into Marrow context.
- NY CLAUDE.md old-system index: keep while the old system runs in parallel; clear when Phase 1 retires it.

## Drift to fix (not this session's scope)

- reference.md directory tree still calls the repo `ny/` and lists README.md (now deleted). The whole marrow subtree there is stale — rewrite in one pass later, not piecemeal.

## Done this session

- DESIGN data-lifecycle 3-tier; reconcile split by view; injection weak-model fallback + session recall=0 alert gate; README removed (folded into CLAUDE.md); CONTEXT.md + ADR-0001; CONVENTIONS.md + project CLAUDE.md (with read-order) + PROGRESS.md; handoff rule changed to fixed-name overwrite; aliases study + mm added.
