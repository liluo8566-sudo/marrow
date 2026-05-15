# Marrow Conventions

Binding. This file is its own example: short, fact-only, no padding.

## Writing (docs, plans, prompts)

- English only.
- Fact only. No examples (except format examples for subagents' prompt), no process, no self-correction narrative, no rationale paragraphs.
- One line per point. Short phrase or sentence.
- A doc states the current truth, never the history of changing it.
- Do not duplicate content already captured in other artifacts (DESIGN, SCHEMA, plans, commits). Reference them by path or URL instead.

## Code

- No comments. No docstrings beyond one line.
- Module soft cap 300 lines. Hook hard cap 100 lines. Over → split.

## PROGRESS.md

- Delta ledger only. One line per finished unit.
- Format: [YYYY-MM-DD] <unit> done | <delta vs DESIGN, or "as designed"> | verify: <cmd/test>

## Commit

- One logical unit per commit. Commit often.
- Private GitHub repo is the remote ledger. Never grep code to reconstruct progress — read PROGRESS.md + git log.
