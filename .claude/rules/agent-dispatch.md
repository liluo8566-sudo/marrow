# Agent dispatch policy

> Main session = orchestrator. Implementation, scanning, summarising → outsource.
> Decide by the 5 dimensions, not gut feeling.

## 5 dimensions
- Context cost: long output / multi-file scan / full doc → agent.
- Decision-making: main session needs the raw result for the next decision? Yes → self. No → agent reports verdict only.
- Determinism: pass/fail or fact lookup → agent reports binary verdict.
- Reusability: ≥3 times → command/agent/hook. <3 → inline.
- Parallelizable: independent units on frozen schema → worktree agent, dispatched in one message.

## Main session keeps
- DECISIONS.md / DESIGN.md / SCHEMA.md / current goal / handover.md
- API design, adjudication, commit messages, user-facing reply
- Files already loaded in this session's working context

## Delegate by default
- grep / find / "where is X used" → Explore (Haiku)
- Web fetch / gh PR / long doc digest → fetcher (Haiku)
- pytest / log read / status check → fact-checker (Haiku)
- Implementation on frozen schema → worktree-implementer (Sonnet/Opus, isolation:"worktree")
- Phase review (3-way concurrent) → `/rr` command
- Literature / journal claims → general-purpose with web search

## Keep inline
- 1–2 tool calls finish it
- Result feeds the very next decision
- Touching a file already in main session context

## Agent reporting contract
- Return verified facts + verdict, not raw output.
- Cite file:line for code claims, source URL for web claims.
- State "could not verify X" instead of guessing.
- ≤400 words unless caller asks for more.
- No git commit / push / config / settings edit from inside a subagent.
