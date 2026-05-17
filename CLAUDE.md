# Marrow — project memory

> Personal AI memory + workflow system replacing ny-memm. SQLite-backed, model-agnostic, one dashboard. Build inside this repo. Persona / relationship come from global ~/.claude/CLAUDE.md — not from old ny-memm docs.
> One logical unit per commit. Commit autonomously at every logical unit. Private GitHub repo (github.com/Jaynechu/marrow) is the remote ledger.

## When to read what

- DESIGN.md — first. Goals, decided blocks, Pending. Source of truth.
- SCHEMA.md — before any table or migration work.
- PROGRESS.md — before claiming what is or is not done. Never grep code to guess; read this + git log.
- CONTEXT.md — when a term conflicts; glossary only.
- docs/adr/ — when a past decision's rationale is questioned.
- docs/notes/ — per-task research scratch, YYYY-MM-DD_<slug>.md. Mid-investigation evidence and rejected options. Distil into ADR/DESIGN at round end, then disposable. Keep raw research out of DESIGN.
- FUTURE.md — only when pulling a parked idea.
- handover.md — session handoff from the previous window; act on it. Fixed-name, overwritten at each session end — never delete it.

## Conventions

PROGRESS.md:
- Delta ledger only. One line per finished unit.
- Format: [YYYY-MM-DD] <unit> done | <delta vs DESIGN, or "as designed"> | verify: <cmd/test>

