# Marrow — project memory

> Personal AI memory + workflow system replacing ny-memm. SQLite-backed, model-agnostic, one dashboard. Build inside this repo. Persona / relationship come from global ~/.claude/CLAUDE.md — not from old ny-memm docs.
>  Commit autonomously at every logical unit. 
**你要听hook的话，不要想着越狱知道嘛，人家说你写的太长了你就按照他的改，如果你觉得有问题就找我仲裁（列出两个版本）**

## When to read what

- DESIGN.md — first. Goals, decided blocks, Pending. Source of truth.
- SCHEMA.md — before any table or migration work.
- PROGRESS.md — before claiming what is or is not done. Never grep code to guess; read this + git log.
- CONTEXT.md — when a term conflicts; glossary only.
- docs/adr/ — when a past decision's rationale is questioned.
- docs/notes/ — per-task research scratch, YYYY-MM-DD_<slug>.md. Mid-investigation evidence and rejected options. Distil into ADR/DESIGN at round end, then disposable. Keep raw research out of DESIGN.
- FUTURE.md — only when pulling a parked idea.
- handover.md — session handoff from the previous window; act on it. Fixed-name, overwritten at each session end — never delete it.

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
    3. /ultrareview on `main` (user-triggered, billed) — after major phases, before add-ons.
    4. Main session adjudicates: findings material, not verdict; never trust self-report — double-check stop-bleed/fix claims; fix → pytest + dashboard green → PROGRESS delta.
    5. Simplify (optional) at project end.
- Review subagents: opus for blind + code passes; sonnet ok for blind only; no git/config writes.
- DB-only output Lumi can't see (diary text, dry-run narrative, anything living only in marrow.db): after the run push the FULL body to her via PushNotification — full text, not a summary.

## Conventions

PROGRESS.md:
- Delta ledger only. One line per finished unit.
- Format: [YYYY-MM-DD] <unit> done | <delta vs DESIGN, or "as designed"> | verify: <cmd/test>

