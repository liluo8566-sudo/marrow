# 2026-06-11
# Full-system review — marrow + synapse-wx

> Method: 15 sonnet module mappers + 1 alert-chain auditor (fact cards) → fable synthesis + deep-read of sessionend/catchup/alert path → 11 sonnet adversarial verifiers. Every finding below carries a verify verdict.

## Verdict in one line

Not a shit mountain. Real src = marrow core 25.9k + synapse-wx 7.5k ≈ 33.5k lines; ~1,100 deletable (~3%); architecture is sound. The systemic illness is the alert chain (silent first failures + a catchup deadlock), not code volume.

## P0 — confirmed bugs (fix first)

1. **Catchup P5 deadlock** · sessionstart_catchup.py `_classify` P5
   - Any `sessionend_extract:start` row newer than lifecycle:end ⇒ classified in-flight ⇒ skip, forever. Start rows are never deleted; no terminal-row check, no time limit.
   - Consequence chain: first failure writes `partial:`/`fail:` → catchup never re-spawns → `prior_fails >= 1` never satisfied → `sessionend_async_retry_failed` alert mathematically unreachable. This is the "digest missing, no alert ever" symptom.
   - Fix: P5 in-flight only if (start newer than end) AND (no terminal row after that start) AND (start age < grace, e.g. 15 min).
   - Verified: confirmed (no deleting path for start rows anywhere; mm+ reset doesn't clear them either).

2. **Strike two is unreachable** · sessionend_async.py `_write_final_audit`
   - The `prior_fails >= 1` gate (silent first failure, alert on retry-failure) is Lumi's intended two-strike design, NOT a bug. The bug is that P5 (#1) kills the retry, so the second strike never happens and the gate never opens. Fix #1 and the gate works as designed. Exception: digest "ok:0" counts as ok → never enters the retry chain at all (plan A-2 folds it into partial).

3. **reconcile_ref resolves the wrong episode** · sessionend_writers.py `seg_affect` reconcile lookup
   - `SELECT id FROM affect_live WHERE unresolved=1 ... ORDER BY created_at DESC LIMIT 1` — no sid/date/text filter. Any truthy reconcile_prev from one session can resolve an unrelated session's episode.
   - Fix: scope candidate by date (arg already available) or session.
   - Verified: confirmed; existing test seeds only one row so collision untested.

## P1 — confirmed, fix in alert batch

4. **add_alert has no fallback sink** · repo.py `add_alert`
   - DB locked/unavailable ⇒ alert raises; call sites swallow with `except: pass` ⇒ alert lost, alerting failure itself silent. Fix: stderr/file fallback inside add_alert.
5. **aging pending_alerts lost on audit failure** · aging.py `main`
   - audit_log INSERT raising inside `with conn:` skips the post-txn flush; eviction already committed, alerts vanish. Fix: flush in finally.
6. **High-cardinality fingerprints** · hooks.py session_end spawn / atlas hook / main catch-all (3 sites)
   - Exception text used as fingerprint ⇒ new row per unique message. Correct pattern already exists at hooks.py catchup spawn site. Fix: stable fingerprint + exception into message=.
7. **wx fake-death swallow** · synapse_wx/loop.py `_handle_provider_dead`
   - session_id set ⇒ alert + bubble suppressed; lazy-respawn failure is caught by _flush_run wrapper and logged only. Genuinely unrecoverable cc ⇒ messages silently stop. Fix: consecutive-death counter, alert after N.
8. **Stub diary '—' permanently blocks backfill** · daily.py `run_day` + daily_catchup.py `pending_days`
   - Empty day writes '—' row; has_diary then True forever; digests arriving later (catchup completing after 07:00 run) never backfill. Fix: pending_days excludes stub rows.

## P2 — confirmed, low urgency

- digest_zero_write can false-fire on genuinely sparse sessions (no content lower bound).
- daily_catchup_overflow fingerprint never auto-resolves (4-day gap alarms forever until manual resolve).
- iCloud offsite backup alert fires on first transient failure, no retry.
- drift dangling-delete alert can false-fire on slow iCloud sync (>30 min cross-root pairing TTL).
- wx media chain: decrypt/upload failures are log-only (no AlertSink); non-16-multiple ciphertext written to disk as success.
- wx tracker._save_locked propagates OSError through RLock; naive datetime.now() in media/inbound.py + sessionend/idle.py (violates project tz rule).
- subpages project child pages: unsanitized title becomes file path.
- drift cross-root inference matches on basename+size only.

## Refuted / corrected by adversarial verify (do NOT act on these)

- **"Switch DELETE → WAL"** — REFUTED. WAL→DELETE was deliberate (2026-05-28 SIGBUS, APFS mmap kernel bug on .db-shm with 3+ threaded connections; docs/archives/PROGRESS.md:368-373). Aging db-lock bug was code-level (second conn inside txn), already fixed in Batch 2. Recorded in DECISIONS.md. backup.py "WAL" docstring is stale text — update wording only.
- **"Dormant revive uses stale importance"** — refuted; the UPDATE's WHERE already targets the live row, importance unchanged. The "re-read" comment at recall.py:1353 is dead — delete the comment, not add code.
- **"Events double min_score gate is a bug"** — intentional; inner gate events-only, unified gate covers anchor/diary/task lanes. Harmless.
- **"wx _last_from_wxid bug breaks media send"** — production always passes to_user_id explicitly (loop.py); the getattr fallback on ILinkClient is dead code, delete it, but no live failure.

## Bloat inventory (~1,100 lines, by payoff)

- subpages_render.py legacy renders (~200) — verified unreachable even on inserter failure; delete render_diary/milestone/memes/study_index/projects_index/profile/stickers/wallet + imports. Keep render_pit (cli), render_cheatsheet (read_only), per-child render_project_page/study_unit.
- hooks.py (~300) — relocation not deletion: recall render helpers → recall.py; pretool atlas closures → atlas.py. Makes both testable.
- sessionend_prompts.py parse_doing_diff cluster (~90) — zero callers, dead.
- recall.py (~95) — memes/milestones merge blocks near-verbatim duplicate (~50), 5 thin embed_* wrappers only tests call (~30), _vec_score_map/_vec_cards unify (~15).
- watcher/sync (~86) — _LazyLog could inline (~50), _warmup_imports (~22, keep if SIGBUS paranoia preferred), TombstoneStore Protocol single-impl (~14).
- wx commands (~70) — _handle_clear/_handle_cwd duplicate 6-step body; _handle_thinking/_handle_quote identical shape; /info 5 single-use helpers.
- candidates group (~60) — two warn_embedder_missing impls; bump_use_counts mirrors bump_mention_counts.
- reconcile (~60) — duplicated _ANCHOR_RE/_scan_anchored_ids; dead delegate shims; _scrub_affect_pollution runs forever (demote to migration).
- storage (~35) — empty migration sentinels v5/v7/v8/v9.
- misc (~150) — wx utility duplicates (_jsonl_path ×2, text extractors ×2), llm.py _extract_usage overlap, atlas triple import-fallback, RETRY_LIMIT dead const, etc.

## Design vs the 6 DESIGN goals

- Goal 1 portability: holds. llm.py intent+tier abstraction clean; synapse stays import-free of marrow (direct sqlite by design).
- Goal 2 memory/recall: holds. Fusion architecture mature; thresholds live in config.
- Goal 3 continuity: holds at write path; broken at failure path (P0 #1/#2 — a missed digest is invisible).
- Goal 4 affect continuity: bug #3 directly threatens correctness (wrong episode resolved).
- Goal 5 "alerts surface what broke": the one systematically failed goal — see alert redesign plan.
- Goal 6 addon base: holds.
- Structural notes, no action needed: events table is the only unaged table that grows unbounded (vec window handles vectors; raw rows fine for years at current volume); subpage legacy/inserter dual path should converge on inserter (delete legacy).

## MAP rewrite

Done this session for both repos (see commits). Key format change: references are file:function (grep-able, rot-proof) instead of line numbers; one mechanism per line with concrete params. Confirmed drift fixed: memes gate 7d→14d window + all-6-types, vec-only floor 0.40→0.55, dormant importance ≤3→≤2, §4.4 described a deleted mechanism (replaced by FTS5 trigram + strong-hit scan), SessionStart injection list, hooks.py all line refs, wx poll_messages name, thinking-bubble truncation claim, SessionTracker schema overclaim.
