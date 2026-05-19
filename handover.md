# Marrow handover — Phase 2 pre-build grill CLOSED (2026-05-19)

Read DECISIONS.md first (Phase 2 = current truth, just rewritten). This file = next-window only, act on it.

## Done this round (NOT pushed)

- Hard-verify: 1M context empirically real on the claude_cli stream-json sonnet path; real heaviest day 2026-05-18 = 151K net tokens (0.66 tok/char); the policy-refusal filter is content-type-dependent (plain-EN refused @136K; real CN+EN diary passed @171K). Evidence: docs/notes/2026-05-19_1m-probe-and-token-census.md
- Grill P1 (single-call output contract) adversarially converged → docs/notes/2026-05-19_grill-p1-{proponent,adversary,adjudication}.md
- DECISIONS Phase 2 rewritten in place; CONTEXT per-event→per-episode; DESIGN 4 stale refs synced (Lumi-authorized this round).

## Locked — do NOT reopen

- Single-call contract: ONE sonnet call → `---` CN prose + trailing ===AFFECT=== JSON ===END=== (one obj/episode: ep/valence/arousal/importance/label/entities/event_hint); prose↔affect decoupled at parse, bad JSON never blocks diary.
- affect = per-episode; rows date-keyed, cascade-rebuilt with the diary row on --force.
- per-episode scalar collapse + affect=subjective-narrative = accepted design cost, not a bug (adversary overruled).
- affect interesting-positive skew = Lumi self-curates the corrective clause in her own DIARY_PROMPT; system never forces low/quiet days high, no semantic auto-judge.

## Must enter Phase 2 build (P0 / engineering, from adjudication)

- Refusal sentinel (P0): detect stop_reason=="refusal" OR a refusal fingerprint → treat as failure → 3-stage fallback, NEVER write the refusal text into the diary. Current llm.py `_parse_claude` returns it as success (is_error may be false) — real hole, prior fallback covered JSON-parse-fail only.
- Date-keyed affect cascade: affect rows for a date deleted+rebuilt in the SAME txn as the diary-row rewrite (no orphan; today there is no alert on orphan).
- event_hint→event_id FTS uniqueness threshold: multi-match → NULL, never first-match (a wrong link corrupts recall worse than no link). event_id feeds ONLY the recall affect-bonus, not the SessionStart backdrop.
- Over-volume guard: chars > 303K (≈200K net tok) → retained 3-stage fallback + neutral affect + alert; pre-call early-exit, the post-call refusal sentinel is the real safety net.

## Next = Phase 2 build

- Serialize first (main, in order, commit): affect + entities schema in storage.py → migrate patch → mw CLI fields. Then parallelize feature modules on the frozen schema (worktree subagents); main merges in report order + adjudicates. Per CLAUDE.md parallel-build.
- Build from DECISIONS Phase 2, not from this file.

## Open / parked

- Cost monitor (task #7 = FUTURE subagent_usage_logging): `_parse_claude` discards the result-event usage/modelUsage; parse it → audit_log → Monitor Zone. Pure code, 0 quota, independent of build — do in a gap. Real single-call ≈ $0.8 API-equiv/day (171K tok sonnet); production cost is fine. Measure haiku-vs-sonnet on identical input by piggybacking the build call (no extra quota).
- DIARY_PROMPT corrective clause: Lumi adds it herself (her file) — do not auto-add.

## Don't redo

- Locked section + grill conclusions; DESIGN already synced (no unprompted DESIGN edits); docs/notes are memo NOT a truth source; DECISIONS overwrite-in-place, never stack.
- 1M / refusal empirically settled — re-probe only with the real-path full-day method, never from assumption.

## Push

- Commits this round on main, NOT pushed — Lumi's call when to push (github.com/Jaynechu/marrow).
