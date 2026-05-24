# Build workflow

## Commit
- Auto commit per logical unit; push at session/phase end.
- If you see something Lumi modified, commit it together.
- Each session commits its own part.

## Build tools
- `/goal <condition>`: when a sub-module's pass condition is fixed and machine-checkable; auto-runs each turn until met. Leave test output in the transcript — the evaluator reads only the conversation.
- `/tdd` skill: for deterministic logic with a fixed behaviour contract — SQLite schema, migrate.py, mw CLI. Red-green-refactor.
- `grill-with-doc` skill: after each review, grill for the next phase.

## Review (run in a new clean session after a phase completes)
> No pytest if no code change
0. Fact-check (`fact-checker` agent): integrate DECISIONS + DESIGN + FUTURE + PROGRESS + git log + pytest + dashboard → one baseline (DONE / DEFERRED-by-plan / DRIFT / test status). Downstream steps work from this baseline.
1. Blind design-gap (`blind-reviewer` agent): goal + DONE list only; forbidden repo access; reasons from outcomes.
2a. DESIGN traceability (`design-traceability-auditor` agent): each phase item DONE / DEFERRED / MISSING / DRIFT; evidence = code, not PROGRESS.
2b. Code quality + logic bugs + safety nets (`code-quality-reviewer` agent): with DESIGN + goal + Marrow safety-net checklist.
3. `/ultrareview` after major phases (only 3 free trials).
4. Main session adjudicates: findings material, not verdict; never trust self-report — double-check stop-bleed/fix claims; fix → pytest + dashboard green → PROGRESS delta.
5. Simplify (optional) at project end.

One-shot: `/rr <phase>` runs step 0 then steps 1 + 2a + 2b concurrently; main session adjudicates.

## Parallel build
- Delegate by default: main session only splits / dispatches / adjudicates / commits. No large implementation in main — subagent does it, main reads conclusion + diff summary.
- Worktree by default for parallel / risky / experimental work: `Agent` with `isolation:"worktree"`, independent units dispatched in one message.
- Serialize first (main, in order, commit): schema / migrate.py / shared CLI skeleton / common module.
- Parallelize after (one worktree subagent each): feature modules on a frozen schema. Main merges in report order; main adjudicates conflicts.
- Review steps 1 / 2a / 2b run as concurrent subagents in one message; main only adjudicates.
- Context: implementation never expands in main; long diff / test output / research scratch stay in subagent → `docs/notes/`; main at ~200k → /handoff.

## Housekeeping
- After each agent worktree merges into main: `git worktree remove -f -f <path> && git branch -D <branch>`. Safe gate = `git merge-base --is-ancestor <worktree-head> main`. Never delete an un-merged worktree.
- Drop empty / stale stash entries (`git stash list` then `git stash drop`) once their content is verified landed or irrelevant.
- Sweep abandoned `/tmp/*.py`, `/tmp/*.db` scratch files created mid-session at session end.
- Prune local-only branches that have no commits ahead of main.
- Untracked `docs/notes/` scratch belongs to the author session
- Each session clean it's own rubbish.
