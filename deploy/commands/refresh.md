---
description: Re-render marrow dashboard + daybrief (and optionally all sub-pages with --all).
---
Run `~/.local/bin/mw refresh $ARGUMENTS` via Bash. Report stdout verbatim (one line confirmation).

Constraints:
- No discussion, no recall, no extra tool calls.
- `$ARGUMENTS` may be empty (dashboard only) or `--all` (dashboard + sub-pages).
