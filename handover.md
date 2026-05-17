# Marrow Handoff — 2026-05-17 (next window: #6 mw CLI)

Read CLAUDE.md → DESIGN.md → SCHEMA.md → PROGRESS.md → this. Fixed-name, act on it, never delete; overwritten at session end.

## Done this session — see PROGRESS.md + git log (do not restate)

#4 daemon, #5 migrate.py (TDD), DESIGN fact-corrections section, prompt-lint backtick fix, subagents.md rewrite, render-templates note. pytest 40/40. Commits pushed, main synced.

## Real DB state — do NOT re-apply

`migrate.py --apply` already ran against the live `~/.config/marrow/marrow.db`: events 3 / goose_bites 36 / milestones 21 / pit 23 / vocab 5. Re-run is idempotent (source_hash) but there is nothing to migrate again. ny-memm runs in parallel ~2 weeks per DESIGN, then retires.

## Locked — do not re-litigate

- MCP: official `mcp` SDK (FastMCP), stdio per-session; logic all in `marrow` package, hooks = thin shells importing it (#7).
- migrate mapping simplified: only `2026.md` / `timeline` / `cipher` / `_pit` / goose-quote files + Lighthouse milestone. 3d/10d/Open-Threads/Garden/stickers dropped. scope = me/us only; Me-section date = birth 1995 + age start.
- Session-start handoff = open threads + open alerts only, no who-i-am (DESIGN L128/140 fixed).
- lesson: independent table + 04:00 haiku auto-capture + manual promote. Defined, storage built. Not re-open.
- Fact corrections: DESIGN "Fact corrections — conflict priority" — 3 hard rules locked (memory never rebuts Lumi; serial facts = append state-seq + latest pointer; priority Lumi-now > confirmed-structured > system-structured > raw event). Reuses lesson intake, skips promote-to-rule. `corrections` table = Phase 2 placeholder (SCHEMA), not built Phase 1.

## Build sequence: #6 → #7 → #8

- #6 mw CLI: `mw` entrypoint (pyproject already maps `mw = marrow.cli:main`). Point-edit/remove one record by id; deterministic, no LLM. USE `/tdd` (+ optional `/goal`).
- #7 four hooks at Phase-1 subset (DESIGN L126-133): SessionEnd code-only clean+archive (reuse repo.archive_events) + dashboard-top regen; SessionStart open-threads+alerts render into CLAUDE.md marker block + diary catchup; UserPromptSubmit must-never-fade (recall fallback off); PreToolUse = global prompt-guard mirror. Nightly 04:00 routine: haiku digest → sonnet diary + haiku lessons. Render templates: `docs/notes/2026-05-17_render-templates.md` (Lumi-locked spec, verbatim). NOT `/tdd` for daemon/hook glue.
- #8 dashboard top render (atomic write + conflict-guard hash).

SessionEnd #7 must enforce audit-drop (drop completed/cancelled/abandoned) + concurrent-write lock — Lumi's pain: parallel sessions never delete, threads pile up.

## Pending — Lumi to rule, not blocking #6

- corrections table build + capture wiring (Phase 2; design fixed).
- schema-evolution mechanism (user_version + ordered patch chain) — ADR-candidate, replaces interim hand-written ALTER in storage.init_db, post-Phase-1 cleanup.
- doc auto-render upkeep (DESIGN/SCHEMA/README/dir map) — cleanup phase, Lumi approved automating, no manual.

## State / gotchas

- env: `.venv` (py3.14), `mcp` 1.27.1 installed, claude bin `/Users/Gabrielle/.local/bin/claude`, ollama absent.
- storage.init_db has idempotent schema-backfill ALTER (goose_bites.source_hash) — pattern to extend per new column until the proper mechanism lands.
- prompt-lint hardened: prompt rule + conservative strip of whole-line backtick wrap (`~/.claude` commit f9d7dc3). Obey its trim, never bypass; escalate to Lumi only on real semantic loss.
- subagents must never git commit/push (subagents.md locked) — sonnet violated once this session; still state it explicitly in every dispatch prompt.
- CN in prompt-class .md must be inside ( ) or code or PreToolUse blocks the write.

## Skills next window

`/tdd` for #6 mw CLI (deterministic, fixed contract). `/goal` optional if pass condition machine-checkable. diagnose for heavy bugs. No `/tdd` for #7 hooks/daemon glue.
