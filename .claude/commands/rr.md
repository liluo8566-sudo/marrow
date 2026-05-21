---
description: Phase review — fact-check, then 3-way concurrent (blind + traceability + code-quality), main session adjudicates.
---
Run a full phase review for: $ARGUMENTS

Steps:
1. Dispatch `fact-checker` agent (phase=$ARGUMENTS). Wait for baseline.
2. Dispatch three agents IN A SINGLE MESSAGE (parallel):
   - `blind-reviewer` — goal + DONE list only
   - `design-traceability-auditor` — baseline + phase name
   - `code-quality-reviewer` — baseline + phase name + changed-file list
3. Adjudicate all three reports:
   - Material findings → `docs/notes/review-<phase>.md`
   - Decide: fix-now vs defer-to-FUTURE vs drop-from-DESIGN
   - Each fix-now: assign to worktree-implementer or do inline
4. Re-run pytest. Update PROGRESS.md with delta lines.

Constraints:
- Cross-check all three reports; never trust a single one
- Subagents never commit; main session commits
- Keep main session under 30k new tokens; ask agent to re-summarise if huge
- ultrareview (manual/$) and simplify (project-end) are NOT part of /rr
