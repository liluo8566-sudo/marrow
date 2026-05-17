# Marrow Handoff — 2026-05-17 (next window)

Read CLAUDE.md → DESIGN.md → PROGRESS.md first. Fixed-name persistent file: act on it, never delete it; overwritten at next session end.

## This session — done, see artifacts (not restated here)

- PROGRESS.md 2026-05-17 (3 lines): ADR-0002, prompt-guard scope-extend, prompt-lint hook.
- ADR: `docs/adr/0002-agent-invocation-credit-routing.md` — agent/credit routing, final form.
- DESIGN.md L131 (PreToolUse = global hook, scope-extended) + L157 (four hooks at phase-1 subset) reworded.

## Prior pending — all closed, do not carry

- turn-inject extra rules / coding.md merge / push-timing: Lumi confirmed long decided. Push "conflict" was a mis-record — marrow CLAUDE.md only states `commit per logical unit`, no push timing; no conflict with coding.md. Old handover's pending list is void.

## prompt-lint — test phase, watch then decide

- Live global hook. Scope `~/.claude/` + `~/cc-lab/marrow/`, whitelist meta-doc + `docs/adr/*` + `.claude/rules/*`. Not yet extended to NY / all-CLAUDE.md — Lumi decides after observing.
- Inherent side effect: Write trims on disk to a compressed version; an immediate Edit using the pre-trim text as `old_string` mis-matches and that Edit passes through. After writing a whitelisted meta-doc, re-read disk before editing the same spot.
- Rollback: `.bak` per file; remove = settings.json PreToolUse drop 2 lines + rm hook.

## Style-bloat — settled, no more rules

- prompt-layer (CLAUDE.md / skill / @import / rule / template) does not fix style bloat. Line-width hook also rejected: measured data shows Lumi's own approved files run long lines too — root cause is density not width, no clean regex gate.
- Only working cure = post-write haiku trim pass = prompt-lint (now live). Do not propose new style rules. Write meta-doc short/dense/one-assertion-per-line/no-explain/no-dup/reference-by-path regardless; prompt-lint is backstop, not licence.

## Marrow Phase 1 — not started, gate open

- Build per DESIGN Phase 1; skills: grill-with-docs, tdd, diagnose.
- Carried: `reviewer-blind` subagent — config once code exists, nothing to review yet.

## Non-blocking drift

- ADR-0001 + CONTEXT.md L38 still say `ny` CLI; should be `mw` (renamed, DESIGN swept, these two missed). One sed pass, not phase-1 blocking.
