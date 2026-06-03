# Build workflow

<reload-long-runners>
- daemon / recall / storage / entity_recall edit → restart cc
- After editing watcher-loaded modules: md_index / top_sections / reconcile / watcher / dashboard / repo / storage edit → `launchctl kickstart -k gui/501/com.marrow.watcher`
</reload-long-runners>

## Commit
- Auto commit per logical unit; push at session/phase end.
- If you see something Lumi modified, commit it together.
- Each session commits its own part, unless last session missed anything (inactive session).
- Don't ask Lumi unless destructive or conflict - just do it and report!!!

## Build tools
- `/tdd` skill: for deterministic logic with a fixed behaviour contract — SQLite schema, migrate.py, mw CLI. Red-green-refactor.

## Review
> No pytest if no code change
0. Fact-check (`fact-checker`): DECISIONS + DESIGN + FUTURE + git log + pytest + dashboard → baseline (DONE / DEFERRED / DRIFT / test status). Downstream steps work from this baseline.
1. Blind design-gap (`blind-reviewer`): goal + DONE list only; no repo access; reasons from outcomes.
2a. DESIGN traceability (`design-traceability-auditor`): each phase item DONE / DEFERRED / MISSING / DRIFT; evidence = code.
2b. Code quality + logic bugs + safety nets (`code-quality-reviewer`): with DESIGN + goal + Marrow safety-net checklist.
2c. Product-blind (`product-blind-reviewer`): code only, no DESIGN/goal/FUTURE; judges product quality + value as half-finished MVP. Mandatory grep counter-check before any "missing/coupled/hardcoded" claim.
3. `/ultrareview` after major phases (only 3 free trials).
4. Main session adjudicates: findings material, not verdict; never trust self-report — reject any finding without file:line evidence; double-check stop-bleed/fix claims; fix → pytest + dashboard green.
5. Simplify (optional) at project end.

One-shot: `/rr <phase>` runs step 0 then 1 + 2a + 2b + 2c concurrently; main session adjudicates.

## Parallel build
- Delegate by default: main session only splits / dispatches / adjudicates / commits. No large implementation in main — subagent does it, main reads conclusion + diff summary.
- Worktree by default for parallel / risky / experimental work: `Agent` with `isolation:"worktree"`, independent units dispatched in one message.
- Serialize first (main, in order, commit): schema / migrate.py / shared CLI skeleton / common module.
- Parallelize after (one worktree subagent each): feature modules on a frozen schema. Main merges in report order; main adjudicates conflicts.
- Review steps 1 / 2a / 2b run as concurrent subagents in one message; main only adjudicates.
- Context: keep main context short and clean - implementation send to agents

## Housekeeping
- After each agent worktree merges into main: `git worktree remove -f -f <path> && git branch -D <branch>`. Safe gate = `git merge-base --is-ancestor <worktree-head> main`. Never delete an un-merged worktree.
- Drop empty / stale stash entries (`git stash list` then `git stash drop`) once their content is verified landed or irrelevant.
- Sweep abandoned `/tmp/*.py`, `/tmp/*.db` scratch files created mid-session at session end.
- Drift sweep across docs at the end of session (if auto mech not done yet)
- Prune local-only branches that have no commits ahead of main.
- Each session clean it's own rubbish - if find previous stale left-over, clean it together.
- Just do it - don't ask Lumi!
