Read DECISIONS.md first — single current-truth, confidence-tagged. Decisions + deletions in commits b185d39 (rebuild) + d89c658 (reconcile); do not restate.

## This round done (2026-05-19, NOT pushed)
- Doc-system rebuilt, reviewed (zero BLOCK); DECISIONS.md entry; docs/adr + SCHEMA.md deleted; SCHEMA → DESIGN Data-model; 6 contradictions cleared; FUTURE regroups by phase; CONTEXT 3 conflicts fixed.
- Pipeline bug fixed by Lumi; root cause claudemiss, not `-p`.

## Open
- Push: 2 commits on main, NOT pushed — waiting on Lumi's go (system-level change + ADR/SCHEMA deletion).
- DESIGN slim: ~293 lines, target ~150; Lumi reviewing — do NOT touch DESIGN unprompted. Proposed: move 5 `## Pending —` sections to FUTURE (by phase); move Editing&correction + Fact-corrections mechanism to DECISIONS (pointer in DESIGN).
- entity (M6): hold-precondition cleared; buildable; no mechanism written; DECISIONS `[hold]` until Lumi approves.

## Don't redo
- docs/adr deleted — never recreate; conclusions live only in DECISIONS; overturn = overwrite in place, never stack.
- DESIGN slim — Lumi owns it this round; no unprompted DESIGN edits.
- pipeline bug — root cause claudemiss, not `-p`; do not reblame `-p`.
- CONTEXT.md — grill-with-doc skill's file; fix conflicts only, never change its role/placement.

## Next session
- Skill grill-with-doc for grilling/advancing Phase 2 entity pipeline.
- Phase 2 build (affect / single-scalar recall / decay FLOOR) converged in DECISIONS — ready to implement when Lumi starts it; entry payload ≤6000 chars, backdrop ≤5 lines ≤350 chars.
