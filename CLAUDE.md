# Marrow — project memory

Personal AI memory + workflow system replacing ny-memm. SQLite-backed, model-agnostic, one dashboard. Build inside this repo. Persona / relationship come from global ~/.claude/CLAUDE.md — not from old ny-memm docs.

Always focus on the main goals. Stay on the right track.

## When to read what

- DESIGN.md — first. Goals, decided blocks, Pending. Source of truth.
- SCHEMA.md — before any table or migration work.
- PROGRESS.md — before claiming what is or is not done. Never grep code to guess; read this + git log.
- CONTEXT.md — when a term conflicts; glossary only.
- docs/adr/ — when a past decision's rationale is questioned.
- docs/notes/ — per-task research scratch, YYYY-MM-DD_<slug>.md. Mid-investigation evidence and rejected options. Distil into ADR/DESIGN at round end, then disposable. Keep raw research out of DESIGN.
- FUTURE.md — only when pulling a parked idea.
- handover.md — session handoff from the previous window; act on it. Fixed-name, overwritten at each session end — never delete it.

This file is binding and always loaded. Do not import old ny-memm docs (rule / system_guide / manual / roadmap) into this context.

## Conventions
**When you talk to me, skip useless filler e.g. you are right, that's a good question, to be honest, I have to remind you. Directly list the facts and findings without preaching!!** Warm but concise, don't be robotic!
Writing (docs, plans, prompts):
- English only. Fact only — no examples (except format examples in subagent prompts), no process, no self-correction narrative, no rationale paragraphs.
- One line per point. Short phrase or sentence.
- A doc states current truth, never the history of changing it.
- Do not duplicate content already in another artifact (DESIGN, SCHEMA, plans, commits). Reference by path or URL.

Code:
- No comments. No docstrings beyond one line.
- Module soft cap 300 lines. Hook hard cap 100 lines. Over → split.

PROGRESS.md:
- Delta ledger only. One line per finished unit.
- Format: [YYYY-MM-DD] <unit> done | <delta vs DESIGN, or "as designed"> | verify: <cmd/test>

## Coding discipline

Planning:
- I tell the outcome; you decide how. Plan first — both agree before acting. Vague prompt → first-principles.
- Propose one concrete solution with reasoning, not a menu. Real fork → name options inline, recommend best.
- Prefer code or config over prompt — code is deterministic; instruction and memory are fallback.
- claude -p / Agent SDK / cc gh actions / third-party Agent-SDK apps burn extra credit — find a subscription path. Anthropic API key is not an option.
- Name vendor lock points and a concrete Codex / OSS swap path at plan time.

Execution:
- Implement only what was asked, plus essential gaps and safety nets (concurrent-writer locks, retry caps, catchup idempotency, atomic writes, I/O boundaries, silent-failure alerting, security guards).
- Effect first, then minimum diff. Wrong foundation → delete the section, do not polish it.
- Execute an agreed plan end-to-end in one pass. In-scope cleanup / rename / dead-code / typo fixes permitted. Pause only on destructive ops or scope expansion.
- Self-review and cut over-engineering every 50 LOC. Wait for the third caller before extracting an abstraction.
- Delete cleanly: no rename-to-unused, no tombstone, no re-export shim.
- Drift sweep on every rename / move / retrigger: fix every affected location in one pass. Path change → `ps -ef | grep <old-path>` for stale processes. Exclude logs and archives.

Verifying:
- Verify with evidence before any statement. No guessing, no fabrication. Every explanation needs proof.
- Before overturning your own conclusion, audit the prior one — what was wrong and why. No jumping to a new theory.
- Solution failed twice → stop, re-read the error, rediagnose from scratch.
- Scripts with side effects → `--dry-run` first, then `--apply` once preview matches.
- UI changes → run the dev server, exercise golden path + edges + regression. Cannot test → say so; never claim untested success.
- Validate at boundaries only — user input, external APIs. Trust internal code elsewhere.

Reporting:
- Natural-language outcome first, then brief mechanical detail and change log. Group by effect, not file order.
- No silent editing. Surface lingering bugs, modified config, moved files. File refs: full path with :N.

Commit / git:
- One logical unit per commit. Commit autonomously at every logical unit. Private GitHub repo (github.com/Jaynechu/marrow) is the remote ledger.
- Push at the end of the session/phase. No confirm needed unless destructive.
- Never bypass hooks, signing, or pre-commit checks unless explicitly told.

Tools:
- Bugs / stuck debugging → diagnose skill. Trivial one-liners need none.
- Use TDD when suitable - Deterministic logic with a fixed behavior contract
- LLM output quality, daemon / MCP glue, hook stdout → not TDD; verify per module at first build, diagnose skill when stuck.
- CC shortcuts / hooks / MCP / commands / settings → WebFetch https://blakecrosley.com/guides/claude-code-cheatsheet before guessing; official docs first for new features.
- Hook stdout injection caps ~10000 chars.
- GitHub ops → gh CLI over WebFetch or hand-rolled cURL.
- OSS used or borrowed → star on GitHub.
