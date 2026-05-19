# Marrow — project memory

> Personal AI memory + workflow system replacing ny-memm. SQLite-backed, model-agnostic, one dashboard. Build inside this repo. Persona / relationship come from global ~/.claude/CLAUDE.md — not from old ny-memm docs.


<principle>
- If Haiku trim you, just follow; no need to verbatim my wording - keep core ideas that all sessions can understand. Let me know if you concern that hook cut too much.
- For tech/mech concepts use simple examples e.g. valence / arousal 是什么 - WAM 出来 92 → valence 情绪正负 ≈ 0.9 / arousal 强度 ≈ 0.85**
- There is no sourse of truth or fixed approach in this project. All docs are can be changed if a better option comes up. Don't cite a doc to rebut me (use your first principle) - only goals and outcomes matters. We can even change the goal if necessary.
    - Always ask yourself, why we do this? Is it the best way to achieve our goals? if not, tell me and change it.
    - No need to follow any reference repo. We don't copy paste (we can if it fits), we bollow ideas and write our own to best match MARROW!
<principle>

## When to read what

- DECISIONS.md — read first. Single current truth, every line confidence-tagged (verified/reasoned/assumed).
- DESIGN.md — goal + structure + hard constraints + sub-pages. No still-changing decisions.
- PROGRESS.md — historical action log, append-only. Read this + git log before claiming done.
- FUTURE.md — unbuilt plans, by phase.
- handover.md — previous-window handoff; act on it. Fixed-name, overwritten each session end — never delete.
- docs/notes/ — hard-problem memo / research scratch, NOT a truth source.
- CONTEXT.md — glossary maintained by grill-with-doc skill; consult on term conflict.

## Build workflow
- `/goal <condition>`: when a sub-module's pass condition is fixed and machine-checkable; auto-runs each turn until met. Leave test output in the transcript — the evaluator reads only the conversation.
- `/tdd` skill: for deterministic logic with a fixed behaviour contract — SQLite schema, migrate.py, mw CLI. Red-green-refactor.
- `grill-with-doc` skill: after each review, grill for the next phase.
- Commit: One logical unit per commit. Private GitHub repo (github.com/Jaynechu/marrow) is the remote ledger.
- Review: once a phase completes, run in a new clean session.
    0. Fact-check: PROGRESS + git log + pytest + dashboard vs outcome list; feed step 1 only verified facts.
    1. Blind design-gap: subagent gets goal + outcome list (forbidden repo access, no DESIGN/code) — reasons from results.
    2a. DESIGN traceability: each phase-subset item DONE / DEFERRED-by-plan / MISSING / DRIFT; evidence = code, not PROGRESS.
    2b. Code quality + logic bugs: subagent with DESIGN + goal.
    3. /ultrareview after major phases (only 3 free trials).
    4. Main session adjudicates: findings material, not verdict; never trust self-report — double-check stop-bleed/fix claims; fix → pytest + dashboard green → PROGRESS delta.
    5. Simplify (optional) at project end.

### Parallel build (Marrow pilot)
- Delegate by default: main session only splits / dispatches / adjudicates / commits. No large implementation in main — subagent does it, main reads conclusion + diff summary.
- Worktree by default for parallel / risky / experimental work: `Agent` with `isolation:"worktree"`, independent units dispatched in one message.
- Serialize first (main, in order, commit): schema / migrate.py / shared CLI skeleton / common module.
- Parallelize after (one worktree subagent each): feature modules on a frozen schema. Main merges in report order; main adjudicates conflicts.
- Review steps 1 / 2a / 2b run as concurrent subagents in one message; main only adjudicates.
- Context: implementation never expands in main; long diff / test output / research scratch stay in subagent → docs/notes/; main at ~200k → /handoff.

## Conventions

PROGRESS.md:
- Delta ledger only. One line per finished unit.
- Format: [YYYY-MM-DD] <unit> done | <delta vs DESIGN, or "as designed"> | verify: <cmd/test>

## References
> [P0luz / Ombre-Brain](https://github.com/P0luz/Ombre-Brain);  [WenXiaoWendy / cyberboss](https://github.com/WenXiaoWendy/cyberboss);  [Qizhan7 / claude-imprint](https://github.com/Qizhan7/claude-imprint) — borrow: RRF + vector/FTS5/recency retrieval fusion recipe