# diary.py rewrite plan — 2026-05-23 (DRAFT — Lumi approval required)

> Source: handover.md §Plan batch P1-P9 · docs/notes/2026-05-23_sessionend-llm-pipeline.md · DECISIONS 2026-05-23 lines.
> Scope: slimming, not redesign. Collapse two paths to all-sonnet single call; fold P1-P9 in one batch; ship one migration.
> Status: DRAFT. No code, no commits until Lumi approves.

## 0. Lumi review checklist (read first)

> Every prompt/template/format string that needs Lumi eyes. Format per item: short name · target file:line · status · who fills · notes.

### L1 — AFFECT `unresolved` field wording
- target: `marrow/prompts.py:~40` (AFFECT_FIELDS block)
- status: **locked** (handover.md:50-52)
- owner: Stellan applies; Lumi grammar polish only. Semantics frozen.

### L2 — AFFECT `reconcile_prev` field wording
- target: `marrow/prompts.py:~55`
- status: **Lumi confirms wording**
- owner: Stellan drafts EN; Lumi polishes. Semantic locked (DECISIONS:27).
- draft: `reconcile_prev (bool): true when this episode clearly resolves a prior unresolved emotional state. Code links the most recent affect.unresolved=1 row at write time.`

### L3 — DIARY_PROMPT new EXCLUDE rule (P7 coding-arg filter)
- target: `marrow/prompts.py:~115` (inside DIARY_PROMPT body, under (不写) section)
- status: **Lumi-fills** (CN, ≤2 lines)
- owner: Lumi authors. Stellan cannot author CN (PreToolUse CJK guard).
- slot: append to current (不写) list at `diary.py:160-164`.

### L4 — importance 1-5 EN anchor
- target: `marrow/prompts.py:~75` (lifted verbatim from `diary.py:270-276`)
- status: **locked** (b728b2a)
- owner: Pure move; no edit.

### L5 — main-tone × fine-label rule block
- target: `marrow/prompts.py:~85` (lifted verbatim from `diary.py:277`)
- status: **locked** (b728b2a)
- owner: Pure move; no edit.

### L6 — DIGEST prompt (per pipeline §16)
- target: `marrow/prompts.py:DIGEST_PROMPT` (NEW)
- status: **Lumi-fills** (pre-ship gate #1)
- owner: Lumi authors.
- required wording scope: CN-dominant; second-person (你/老婆) preserved; verbatim conversational lines retained, NOT collapsed to work-style summary; (心理活动) blocks must NOT proliferate, keep prose close to original transcript shape; sonnet judges compression by work-vs-chat density.
- **BLOCKER for first SessionEnd async ship** (per handover.md:22-28), not blocker for this rewrite if shipped as empty string placeholder.

### L7 — DIARY_PROMPT body (existing)
- target: `marrow/prompts.py:DIARY_PROMPT` (moved from `diary.py:137-194`)
- status: **locked**
- owner: Pure move; no wording change.

### L8 — SINGLE_CALL_PROMPT body (existing)
- target: **DELETED** — collapsed into DIARY_PROMPT + AFFECT_BLOCK
- status: **Lumi confirms collapse**
- owner: After rewrite, single call uses DIARY_PROMPT + appended AFFECT contract. Confirm the AFFECT_BLOCK CONTRACT block (`diary.py:256-280`) lifts verbatim and only the two new fields are appended.

### L9 — handover_template §Affect Pending row format
- target: `marrow/handover_template.md:46-48` (existing)
- status: **locked**
- owner: Render code reads `affect.unresolved=1 AND resolved_at IS NULL`.

### L10 — dashboard Pending row format (P5 checkbox)
- target: `marrow/handover_render.py` (NEW, 2.5b scope, NOT this rewrite)
- status: **deferred to 2.5b**
- owner: format = `- [ ] {date} {fine-label} | {description} <!-- aid=N -->`

### L11 — handover_template.md §Affect paste (P1)
- target: `marrow/handover_template.md:32-39`
- status: **Lumi pastes manually**
- owner: Hook blocks CN edits from Stellan tool calls. Lumi opens editor, replaces lines 32-39 with `marrow/_affect_template_paste.txt`.

### L12 — global ~/.claude/CLAUDE.md Affect legend (P2)
- target: Lumi-owned
- status: **Lumi self-writes**
- owner: Outside repo scope; Stellan does not touch global CLAUDE.md.

> Blockers for the diary.py rewrite landing: L1 + L2 + L3 + L6 (and L6 can ship as empty placeholder if Lumi defers).
> L11 + L12 are Lumi-side hand work, parallel to this rewrite.

## 1. Current responsibilities audit (diary.py 900 LoC)

> Format per row: line range · responsibility · verdict.

- 1-19 · Module docstring (two-path narrative, over-volume fallback explainer) · **rewrite** (single path now)
- 32-42 · Transcript fence helper `_TX_OPEN / _TX_CLOSE / _fence` · **extract** to `prompts.py` (fence is prompt scaffolding)
- 45-116 · `DIGEST_SHORT` + `DIGEST_LONG` (haiku-tier fallback digests) · **DELETE** (haiku tier gone; all-sonnet per §5)
- 118-135 · `STITCH_PROMPT` (haiku-tier stitch) · **DELETE** (haiku tier gone)
- 137-194 · `DIARY_PROMPT` (fallback path, 3-stage write) · **extract** to `prompts.py` (kept; see §3 note on future DIGEST path)
- 196-283 · `SINGLE_CALL_PROMPT` (main path, prose+AFFECT) · **rewrite + extract** to `prompts.py` as `DIARY_PROMPT + AFFECT_BLOCK` (collapse; see L8)
- 285-294 · over-volume guard + neutral affect constants + markers · **keep**, move to `extract.py` (constants)
- 296-326 · day-boundary constants `_TZ / _CUTOFF_H` + session caps + drop thresholds · **keep**, move to `extract.py`; **`_CUTOFF_H = 4` → `6`** (P4)
- 296-307 · `_SKIP_DROP_MAX / _SKIP_JUDGE_MAX` (haiku routing thresholds) · **DELETE** (haiku gone)
- 322-326 · `_SESSION_CHAR_CAP / _CHUNK_CHARS` (haiku map-reduce caps) · **DELETE** (single-call path does not chunk per session)
- 328-349 · `_to_local / _diary_day / _routine_target` (day boundary helpers) · **extract** to `extract.py` (shared)
- 352-369 · `_app_lock` (fcntl serialise) · **extract** to `catchup.py` (only run-orchestration callers need it)
- 372-405 · `_scan_rows / pending_days / day_events` (event pull + missing-day detection) · **extract** to `catchup.py`
- 408-411 · `_has_diary` · **extract** to `catchup.py`
- 414-428 · `_hhmm / _local_md` (timestamp formatting for prompt span tags) · **extract** to `extract.py`
- 431-463 · `_speaker / _sessions` (role-tag remap, session grouping) · **extract** to `extract.py`
- 466-479 · `_chunks` (oversize-line splitter for haiku map-reduce) · **DELETE** (single call does not chunk)
- 482-507 · `_is_skip / _session_digest` (haiku per-session digest router) · **DELETE**
- 510-524 · `_stitch` (haiku stitch helper) · **DELETE**
- 529-555 · `_parse_single_call` (split prose / AFFECT JSON; outcome enum) · **extract** to `extract.py` (parse contract owner)
- 558-570 · `_resolve_event_hint` (FTS5 uniqueness lookup) · **extract** to `extract.py`
- 573-617 · `_build_affect_rows / _neutral_affect_rows` (row construction + neutral fallback) · **extract** to `extract.py`; **add** `unresolved / reconcile_ref / resolved_at` fields per P3
- 620-645 · `_SINGLE_CALL_ACTIONS / _log_single_call_outcome` (audit_log marker) · **extract** to `extract.py`
- 648-656 · `_write_affect` (INSERT) · **extract** to `extract.py`; **extend** INSERT with 3 new columns
- 659-697 · `_write_entities` (INSERT entities, dedup, kind whitelist) · **extract** to `extract.py`
- 700-707 · `_sessions_flat` (flat fenced text for single-call prompt) · **extract** to `extract.py`
- 710-836 · `run_day` (orchestrator: pull events → single call → atomic write; LLMError → 3-stage fallback) · **rewrite** to `rollup.py` (single path; fallback deleted; see §3)
- 839-862 · `run` (routine vs catchup dispatch) · **extract** to `catchup.py`
- 865-900 · `main / __main__` CLI entrypoint · **keep thin** in `diary.py` (the `python -m marrow.diary` plist entry; see §4)

## 2. Final module shape (after rewrite)

> 4 files, total ≤ 600 LoC (was 900). Each ≤200 LoC.
> Plist `python -m marrow.diary` entrypoint preserved (`deploy/mw-diary-routine.plist:13`, `deploy/mw-diary-catchup.plist:13`). Tests `from marrow import diary` preserved.

### `marrow/diary.py` (~80 LoC — thin entrypoint shim)
- Module docstring: nightly 07:00 read-only roll-up entrypoint (was 04:00 SessionEnd-async write path).
- Public: `main(argv) / run(conn, llm, ...) / run_day(conn, date, llm, ...)` re-exported from `rollup` + `catchup` (back-compat for tests + plist).
- No prompt text, no extraction logic.

### `marrow/prompts.py` (NEW, ~180 LoC — prompt module, P6)
- Constants: `_TX_OPEN / _TX_CLOSE / _fence` (transcript fence helper).
- `DIARY_PROMPT` (lifted from `diary.py:137-194`, prose body unchanged; L3 new EXCLUDE line appended under (不写) list).
- `AFFECT_BLOCK_CONTRACT` (lifted from `diary.py:256-280`; **adds** `unresolved` and `reconcile_prev` fields per P3 wording L1 + L2).
- `DIARY_PROMPT_FULL = DIARY_PROMPT + "\n\n" + AFFECT_BLOCK_CONTRACT` (used by single-call path; replaces `SINGLE_CALL_PROMPT`).
- `DIGEST_PROMPT` (NEW, **Lumi-fills L6**) — SessionEnd async §16 length-flex prompt.
- `IMPORTANCE_ANCHOR` (lifted from `diary.py:270-276`) — referenced inside AFFECT_BLOCK_CONTRACT.
- `FINE_LABEL_RULE` (lifted from `diary.py:277`) — referenced inside AFFECT_BLOCK_CONTRACT.
- Exports: `DIARY_PROMPT_FULL`, `DIGEST_PROMPT`, `_fence`, `AFFECT_OPEN = "===AFFECT==="`, `AFFECT_CLOSE = "===END==="`.

### `marrow/extract.py` (NEW, ~180 LoC — single-call extraction + parse)
- Constants: `_TZ`, `_CUTOFF_H = 6` (P4: was 4), `_OVER_VOLUME_CHARS = 303_000`, `_NEUTRAL_VALENCE/AROUSAL/IMPORTANCE`, `_ENTITY_KINDS`.
- Pure helpers: `_to_local`, `_diary_day`, `_routine_target`, `_hhmm`, `_local_md`, `_speaker`, `_sessions`, `_sessions_flat`.
- Parse contract: `_parse_single_call(text) -> (prose, affect_raw, outcome, err)`.
- Row construction: `build_affect_rows(conn, date, prose, affect_raw, outcome) -> list[dict]` — **adds** importance clamp `max(1, min(5, int(x)))` and the three new fields (`unresolved`, `reconcile_ref`, `resolved_at`).
- `_reconcile_lookup(conn, date) -> int | None` — most recent `affect.id WHERE unresolved=1 AND resolved_at IS NULL`, used when row has `reconcile_prev=true`.
- `neutral_affect_rows(date, n, source) -> list[dict]`.
- `_resolve_event_hint(conn, hint) -> int | None`.
- `write_affect(conn, rows)` — INSERT extended with 3 new columns.
- `write_entities(conn, affect_raw)` — unchanged.
- `log_outcome(conn, date, outcome, ep_count, err)` — audit_log marker.

### `marrow/rollup.py` (NEW, ~120 LoC — nightly 07:00 roll-up write)
- `run_day(conn, date, llm, *, db=None, force=False) -> bool` — idempotent date-keyed write.
- reads DIGEST + structured affect rows (NOT raw transcript) per pipeline §2.4; the read source flips here once 2.5c step 6 ships DIGEST.
- **interim** (until DIGEST ships): still calls single sonnet with `DIARY_PROMPT_FULL` over fenced sessions (current behaviour preserved minus haiku fallback).
- atomic txn: DELETE+INSERT diary row + affect rows + entities + audit_log marker.
- on `LLMError`: write neutral affect row + raise alert + return False (no 3-stage retry; SessionEnd-catchup or `--force` reruns).

### `marrow/catchup.py` (NEW, ~100 LoC — scan + dispatch + lock)
- Constants: `CATCHUP_WINDOW_DAYS = 7`, `CATCHUP_MAX = 3`.
- `pending_days(conn, window_days) -> list[str]`.
- `day_events(conn, date) -> list[dict]`.
- `_has_diary(conn, date) -> bool`.
- `_app_lock(path=None, *, blocking=True)` — fcntl serialise.
- `run(conn, llm, *, db=None, day=None, catchup=False, force=False) -> list[str]`.
- `main(argv) -> int` — CLI entry (`python -m marrow.diary` re-exports this; or `python -m marrow.catchup` direct, depending on plist choice — see §4).

## 3. Two-path collapse to all-sonnet (per pipeline §5)

> A/B test (`docs/notes/2026-05-23_diary-ab/`): haiku 2/3 runs drop prose-ep/affect-row count alignment; sonnet 3/3 align. Nightly is user-invisible — 50s vs 95s irrelevant. No turn-routing.

### Dead code paths to delete (line refs in current `diary.py`)
- `DIGEST_SHORT` block — `diary.py:55-85` (~31 lines)
- `DIGEST_LONG` block — `diary.py:87-116` (~30 lines)
- `STITCH_PROMPT` block — `diary.py:118-135` (~18 lines)
- `_SKIP_DROP_MAX / _SKIP_JUDGE_MAX` constants — `diary.py:305-306`
- `_SESSION_CHAR_CAP / _CHUNK_CHARS` constants — `diary.py:323-324`
- `_chunks` helper — `diary.py:466-479`
- `_is_skip` helper — `diary.py:482-484`
- `_session_digest` helper — `diary.py:486-507`
- `_stitch` helper — `diary.py:510-524`
- 3-stage fallback branch inside `run_day` — `diary.py:789-816` (the `if use_fallback or not narrative:` arm)
- Code-level skip-by-turn-count drop — `diary.py:737-742` (kept logic is now ≤5-turn skip at SessionEnd per pipeline §2.7, NOT here at nightly)
- Over-volume guard line (`if total_chars > _OVER_VOLUME_CHARS: ... add_alert`) — `diary.py:759-768`: **DOWNGRADE not delete**. Keep the alert; remove the fallback branch (still a single sonnet call, just with the warning).

Net delete: ~210 LoC from `diary.py` (was 900; after extract + collapse, total ~480 LoC across 4 files, plus ~180 LoC `prompts.py`).

### Audit-log action names to clean
- `diary_single_call` / `diary_single_call_no_affect` / `diary_single_call_no_affect_marker` / `diary_single_call_affect_parse_fail` / `diary_single_call_affect_ok` (`diary.py:620-624`) — keep; they distinguish outcomes within the (now sole) single-call path.
- `diary_fallback` source tag (`diary.py:816`) — **DELETE** along with `_neutral_affect_rows` fallback caller; `neutral_affect_rows` itself stays for the bad-JSON case but always carries `source="diary_single_call_no_affect"`.

## 4. Migration — single shot (P4)

> One block executed at `storage.connect()` startup, gated by `PRAGMA user_version`. Marrow already uses `CREATE TABLE IF NOT EXISTS`; add ALTERs gated by a `_migrate_to_v2()` step.

### Affect schema (3 new columns, P3 + P4)
- `ALTER TABLE affect ADD COLUMN unresolved INTEGER DEFAULT 0;`
- `ALTER TABLE affect ADD COLUMN reconcile_ref INTEGER REFERENCES affect(id);`
- `ALTER TABLE affect ADD COLUMN resolved_at TEXT;`
- View `affect_live` (`storage.py:155-157`) inherits the new columns automatically (SELECT *).

### Importance 1-5 clamp
- Runtime: `extract.build_affect_rows()` clamps `max(1, min(5, int(raw.get("importance"))))` before INSERT.
- Backfill: NO retroactive UPDATE on existing rows (Lumi note: 5/22's 5 affect rows stay as-is; 5/17-5/20 skip already locked per pipeline §6). Confirm: leave historical rows untouched (open question §10.6).

### 6AM day boundary (P4 + pipeline §6)
- Code: `extract._CUTOFF_H = 6` (was 4 at `diary.py:319`).
- Schedules realigned in 2.5b plist work, NOT in this rewrite (`deploy/mw-diary-routine.plist` 04:00 → 07:00; `deploy/mw-diary-catchup.plist` 16:00 → 19:00; `deploy/mw-jsonl-cleanup.plist` Sun 05:00 → Sun 12:00). **Out of scope for this batch.**

### threads → tasks rename
- **NOT in this batch.** Pipeline §12 places it in 2.5c Window 1 step 3 (separate ===THREAD_CAND=== work). Migration sketch (for later): `DROP TABLE threads; CREATE TABLE tasks (id, title, status, due, completed_at, tag TEXT NULL, ts ...)` — 0 rows in threads, no backfill needed (handover.md:5).

### plist entrypoint preservation
- `deploy/mw-diary-routine.plist:13` and `deploy/mw-diary-catchup.plist:13` both call `python -m marrow.diary`. To keep these untouched: `marrow/diary.py` retains a `main()` that re-exports `catchup.main`. Alternative (Lumi pick): update both plists to `marrow.catchup` and delete `diary.py:main` — cleaner, but touches deploy/. **Default plan: keep shim.**

## 5. P1-P9 mapping

### P1 — handover_template.md §Affect paste
- absorbed by: (Lumi hand)
- target file:line: `marrow/handover_template.md:32-39`
- Lumi review: ☐ L11 — Lumi pastes manually

### P2 — global CLAUDE.md Affect legend
- absorbed by: (Lumi hand)
- target file:line: `~/.claude/CLAUDE.md`
- Lumi review: ☐ L12 — Lumi self-writes

### P3 — AFFECT prompt `unresolved` + `reconcile_prev` fields
- absorbed by: `prompts.py`
- target file:line: `marrow/prompts.py:~40,~55` (AFFECT_BLOCK_CONTRACT)
- Lumi review: ☐ L1 + L2 — semantics locked, wording polish

### P4 — affect schema 3 cols + importance clamp + 6AM single migration
- absorbed by: `extract.py` + `storage.py` migration block
- target file:line: `marrow/storage.py:_migrate_to_v2`, `marrow/extract.py:_CUTOFF_H`
- Lumi review: ☐ none — code-only

### P5 — Pending resolve = dashboard tick
- absorbed by: (deferred to 2.5b — `handover_render.py` + `dashboard_watch.py`)
- target file:line: NOT in this batch
- Lumi review: ☐ L10 — format Lumi confirms in 2.5b

### P6 — diary.py rewrite (split + prompt module + possible rename)
- absorbed by: all of §2 (`prompts.py` / `extract.py` / `rollup.py` / `catchup.py` / `diary.py` shim)
- target file:line: new files above
- Lumi review: ☐ all of L1-L8

### P7 — DIARY_PROMPT new EXCLUDE line (filter coding-arg quotes)
- absorbed by: `prompts.py`
- target file:line: `marrow/prompts.py:~115` inside DIARY_PROMPT
- Lumi review: ☐ L3 — **Lumi-fills CN line**

### P8 — ollama removal (~60 LoC + 4 tests)
- absorbed by: `llm.py` + `tests/test_llm.py` + `config.default.toml` + docs
- target file:line: see §6 below
- Lumi review: ☐ none — code-only

### P9 — daemon LLM hook-isolation ping-pong test
- absorbed by: 2.5b first task (BEFORE other 2.5b)
- target file:line: NOT in this rewrite
- Lumi review: ☐ none — verified in 2.5b

> Landed by this rewrite: P3 + P4 + P6 + P7 + P8.
> NOT landed: P1 + P2 (Lumi hand) · P5 + P9 (2.5b scope).

## 6. P8 ollama removal piggyback

> Delete ~60 LoC net + 4 tests; keep `emergency` config key empty as OSS-fork extension point (handover.md:89 + Lumi 2026-05-23 directive).

### Code deletes
- `marrow/llm.py:56-61` — `_MUTE_OLLAMA` flag + comment block (~6 lines)
- `marrow/llm.py:90-94` — chain-build branch filtering ollama under mute (~5 lines)
- `marrow/llm.py:138-139` — `if kind == "ollama": return self._run_ollama(...)` dispatch (~2 lines)
- `marrow/llm.py:330-347` — `_run_ollama` method body (~18 lines)
- `marrow/llm.py:2` — module docstring `default -> fallback -> emergency` becomes `default` only (~1 line edit)
- `marrow/config.default.toml:30` — `emergency = "ollama"` becomes `emergency = ""` (Lumi note: keep KEY for OSS fork; empty value is the no-op signal) (~1 line edit)
- `marrow/config.default.toml:39-41` — entire `[llm.ollama]` block (~3 lines)

### Test deletes (file `tests/test_llm.py`)
- `test_ollama_muted_by_default_chain_is_claude_only` — line 64
- `test_rotation_path_intact_when_unmuted` — line 117 (monkeypatches `_MUTE_OLLAMA`)
- `test_whole_chain_fails_raises_and_critical_alert` — line 132 (monkeypatches `_MUTE_OLLAMA`)
- Edits to `tests/test_llm.py:12-14` config fixture: drop `"emergency": "ollama"` and `"ollama": {...}` lines (~3 lines)
- Edit `test_multi_tier_all_fail_last_alert_critical_exhausted` (line 98) if it monkeypatches `_MUTE_OLLAMA` — drop the monkeypatch line, keep rest

> 4th BLOCKING test per handover.md:88 is `test_whole_chain_fails_raises_and_critical_alert`. The remaining 3 above are `test_ollama_muted_*` / `test_rotation_path_intact_*` / fixture lines.

### Doc trims
- `DESIGN.md:80` — chain line: drop ` → local Ollama (emergency)`.
- `DESIGN.md:85` — `LLM via claude CLI subprocess (OAuth) or local Ollama` becomes drop ` or local Ollama`.
- `DECISIONS.md:10` — drop ` / Ollama emergency` from the verified line; keep the rest.
- `CLAUDE.md` (project) — line 16 ollama caveat — `Ignore all ollama / claude_cli rotating provider-chain alerts ...` — **KEEP** (Lumi may still rotate provider configs in OSS forks; the principle is still useful).

### Verify
- `pytest tests/test_llm.py -k "ollama"` returns no tests after delete.
- `grep -rn "ollama" marrow/ tests/ DESIGN.md DECISIONS.md` returns only the empty-config-key line and any historical PROGRESS entries.

## 7. Risk + rollback

### Risk if shipped partial
- **prompts.py extracted but DIGEST_PROMPT (L6) not Lumi-filled** → SessionEnd async §2.3 cannot ship DIGEST segment; 2.5c step 6 blocked. Diary nightly path still works (reads sessions directly, interim §3 note). **Mitigation:** DIGEST_PROMPT can ship as empty string placeholder; only the 2.5c-step-6 SessionEnd async caller is gated; diary roll-up is independent.
- **migration runs but new fields not populated** → `affect.unresolved = 0` default for all historical + neutral-fallback rows; renders as no Pending entries. Safe (matches current state where no Pending rows exist).
- **plist still calls `marrow.diary`, shim missing** → launchd FAILS silently → no nightly diary. **Mitigation:** keep `diary.py` shim with `main = catchup.main` re-export; pytest covers the `python -m marrow.diary` entry.
- **`reconcile_ref` lookup races SessionEnd-catchup** → catchup reruns may double-link a resolved ep. **Mitigation:** the `_reconcile_lookup` query filters `resolved_at IS NULL`, and the run_day txn is atomic; a stale lookup at worst writes one extra `resolved_at` to the same row (idempotent).

### Rollback
- `git revert <rewrite commit>` restores `diary.py` 900-LoC monolith.
- Migration is forward-only (3 new columns + clamp); rollback leaves the columns in place — harmless (defaults = 0/NULL/NULL).
- `prompts.py / extract.py / rollup.py / catchup.py` = pure deletes on revert.
- No data loss path: every affect/diary write goes through atomic txn; revert before any new-field write = no orphan rows.

## 8. Sequencing inside this batch

> Single commit if possible; one logical unit. Order matters for test-green.

1. Add `marrow/storage.py:_migrate_to_v2()` — 3 ALTER TABLEs + `PRAGMA user_version`.
2. Create `marrow/prompts.py` — lift L4/L5/L7, add L1/L2 fields, append L3 EXCLUDE line, add L6 (Lumi-filled or empty placeholder).
3. Create `marrow/extract.py` — lift constants + helpers + parse + row build (with new fields + clamp + 6AM).
4. Create `marrow/rollup.py` — `run_day` single-path.
5. Create `marrow/catchup.py` — scan/dispatch/lock/main.
6. Rewrite `marrow/diary.py` to thin shim re-exporting `run / run_day / main`.
7. Update `tests/test_diary.py` imports (`from marrow import diary` still works via shim; deeper tests may need `from marrow.extract import ...` for new fields).
8. Apply P8 ollama deletes (`llm.py`, `tests/test_llm.py`, `config.default.toml`, doc trims).
9. Run `pytest` — expect 274 minus 3-4 ollama tests = ~270 passing.
10. Spot-check on a real session run (`python -m marrow.diary --day 2026-05-22`) — confirms new fields surface in affect table.

## 9. Out of scope (this batch)

- launchd plist time realignment (2.5b deploy work).
- `handover_render.py` + `dashboard_watch.py` Pending tick path (2.5b, P5).
- threads → tasks rename (2.5c Window 1 step 3).
- ===DIGEST=== runtime caller (2.5c Window 2 step 6; this rewrite only adds the prompt slot).
- ===NARRATIVE=== async segment (2.5c Window 3).
- 04:00 → 07:00 nightly demote (2.5c Window 3).

## 10. Open questions for Lumi

1. **L3 (P7 EXCLUDE line)** — CN wording for the coding-arg filter. Suggested slot: append to (不写) list. Lumi authors.
2. **L6 (DIGEST prompt)** — full prompt body. Required scope listed in checklist; Lumi authors. BLOCKER for 2.5c step 6, not for this rewrite if shipped as empty placeholder.
3. **L2 (reconcile_prev EN wording)** — Stellan-drafted, Lumi confirms or polishes.
4. **Plist shim vs rename** — keep `marrow/diary.py` thin re-export shim (default plan), or update both `.plist` files to `python -m marrow.catchup` and delete `diary.py` entirely? Recommend keep-shim — minimal deploy churn.
5. **DIARY_PROMPT vs DIARY_PROMPT_FULL naming** — is the AFFECT contract always glued (single-call path always emits both), or do we need separate exports for the (future) 07:00 read-only roll-up that reads pre-extracted DIGEST? Recommend: keep `DIARY_PROMPT_FULL` as the single-call constant; introduce `DIARY_PROSE_ONLY` later only when DIGEST path ships.
6. **Historical importance backfill** — leave 5/22's 5 affect rows untouched, OR run a one-shot `UPDATE affect SET importance = max(1, min(5, importance))` at migration time? Recommend leave untouched (zero risk; current rows are already 1-5 per Lumi-locked single-call output).
