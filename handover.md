# Marrow handover — 2026-05-22 18:20

## State
- pytest 244/244 (234 + 10 milestone recall tests)
- UserPromptSubmit live verified end-to-end: query (`鸭子`) → 5 hits incl. milestone #15/#16

## This session shipped
- **milestones into recall_fusion** `marrow/recall.py` (new `_milestone_candidates` + scoring leg) — LIKE-scan over `title || ' ' || description`, CJK + ASCII tokenizer, `w_bm25 * kw_score + 0.10 pinned`. Sidesteps trigram FTS5 CN >=3-char limit (would miss 2-char `鸭子`). 21 rows, scan cost negligible.
- **UserPromptSubmit hook wired** `~/.claude/settings.json` UserPromptSubmit array — `python -m marrow.hooks user_prompt_submit` registered alongside name-react / turn-inject / code-bar. SessionStart/SessionEnd pattern.
- **`tests/test_recall.py`** +10 tests: tokenizer / exact-term surface / partial token / no-match / pinned ordering / mixed events+milestones / min_score gate / content render / pinned-pushes-over-gate.

## Open / not touched — Phase 3 milestone & people gap
- **Lumi-facing language consistency** — keep CN/EN consistent across surfaces Lumi reads (handover, dashboard, SessionStart context, alerts). No random CN/EN flipping; mix is fine if it reads natural; align where alignment matters (headers, labels, tags).
- **milestone persistent input** — `DESIGN.md:118` says structured-view persistence walks md edit + reconcile (short-id per row, edit/add/delete -> reconcile). `FUTURE.md:19` parks the milestone sub-page render template in `build_time_deferred` — no md file currently lets Lumi write new milestones. `timeline.md` is the one-shot migration source (ny-memm retiring), not the ongoing input. Three options for Phase 3:
    1. Render milestone sub-page md (under dashboard or standalone), Lumi hand-edits, SessionEnd reconcile writes back — matches DESIGN L118.
    2. `mw add milestone --scope us --date YYYY-MM-DD --title ... [--description ...]` CLI.
    3. Both (md = daily, CLI = scriptable fallback). Recommended.
- **people / preference pipeline** — `DECISIONS.md:36`: `entities` + `entity_facts` (kind=person|pref|place), append-only with `superseded_by`, emitted in the diary single sonnet call. Reality: `entity_facts` schema not built; `entities` exists but unused; `affect` table 0 rows (single call never produced output successfully). Static `<Family_and_Friend>` block in global CLAUDE.md is the only live people memory. Root: single-call pipeline never delivered — see next item.
- **affect / single-call 0 rows** — `affect` 0 rows despite diary rows 5/17–5/20. Trace `diary.py:520-535` `_parse_single_call` (===AFFECT=== extraction) and `_build_affect_rows` / `_write_affect`. Same blockage holds back entity extraction.
- **alert provider-chain severity** — fires `critical` for every tier despite success. Rule: tier failure = `warn`; chain "no output" = `critical`. Interim mute in CLAUDE.md; real fix at diary/provider-chain `severity="critical"` emit site.
- **sub-page render** — `DESIGN.md:163` Phase 2; `FUTURE.md` `build_time_deferred` punted. Reconcile + atomic write done; no sub-page to disk yet. Milestone gap above depends on this.

## Reference
- `DESIGN.md:72,93,118,163`
- `DECISIONS.md:36`
- `FUTURE.md:19,67`
- `docs/notes/review-phase-2.md` (/rr Phase 2)
- `.claude/rules/build-workflow.md`
- `.claude/rules/agent-dispatch.md`
