# Marrow — project memory

Personal AI memory + workflow system replacing ny-memm. SQLite-backed, model-agnostic, one dashboard. Build inside this repo. Persona / relationship come from global ~/.claude/CLAUDE.md — not from old ny-memm docs.

## When to read what

- DESIGN.md — always, first. Goals, decided blocks, Pending. Source of truth.
- SCHEMA.md — before any table or migration work.
- CONVENTIONS.md — before writing any doc or code. Binding.
- PROGRESS.md — before claiming what is or is not done. Never grep code to guess; read this + git log.
- CONTEXT.md — when a term conflicts; glossary only.
- docs/adr/ — when a past decision's rationale is questioned.
- FUTURE.md — only when pulling a parked idea.
- handover.md — if present, a session handoff; act on it, then delete it.

## Binding

CONVENTIONS.md governs. English only, fact-only, one line per point, no script comments, module ≤ 300 lines, hook ≤ 100 lines. A doc states current truth, never the history of changing it. Do not import old ny-memm docs (rule / system_guide / manual / roadmap) into this context.
