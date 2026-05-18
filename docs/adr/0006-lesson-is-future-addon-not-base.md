# ADR-0006 — lesson is a FUTURE addon, not base

Status: accepted (2026-05-19, grill round 4)

## Context

DESIGN goal 4 named "past mistakes self-summarise into avoided rules"; SCHEMA listed `lessons` as Phase 1 first-class and "the only manual-curation block (the self-correction goal)"; dashboard Alerts and Open Threads both declared a lesson stream. Implementation diverged: lesson extraction was removed from diary.py 2026-05-18 (it over-extracted facts / tool discoveries / design decisions already living in events + ADR/DESIGN) and parked as FUTURE `lesson_extraction_rework`. The docs kept describing a lesson flow that does not run — a concept rejected by implementation still sitting in the spec, same failure class as the feel layer (ADR-0005).

## Decision

- lesson leaves base entirely. Not a Phase 1 or Phase 2 deliverable; a FUTURE opt-in addon, revisited only if a real recurring need appears after settle-down.
- Why it is cheap as an addon: transcript clean + the haiku/sonnet day-summary chain already exist; a lesson consumer is a small script reading the existing summary, not a base capability — exactly goal 7's addon philosophy.
- dashboard Alerts = bug reports + pipeline-failure only; no lesson surface.
- Open Threads = three classes (daily / study / project); no lesson class.
- Permanent keepsake tier drops `lessons` from its list.
- `lessons` table and code already removed in prior session (storage DDL, cli field, tests, `DROP TABLE` executed; backup: `marrow-2026-05-19.db`; `grep -ri lesson marrow/` clean). This ADR updates surviving SCHEMA / DESIGN wording; revival recreates the table.

## Consequences

- Smaller base (goal 7): no manual-curation block, no promote-to-rule path in base; dashboard top is purely failure state.
- goal 4 keeps its workflow / build-carryover half; the self-summarise-mistakes half is explicitly a FUTURE addon, not a base promise.
- Open Threads reconcile contract simplifies — one fewer class to merge; code already clean.
- If revived: the addon recreates `lessons`, reads the existing day-summary, re-weaves a lesson surface into Open Threads + Alerts; the cost is the re-weave, not a new pipeline.
