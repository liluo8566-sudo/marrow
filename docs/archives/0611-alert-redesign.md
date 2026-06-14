# 2026-06-11
# Alert redesign — first-failure-visible, dedup-stable, no silent layer

> Root findings: docs/notes/0611-system-review.md (P0/P1). This plan is the build spec; each item = one logical commit. No schema change needed — alerts table + (type, fingerprint, resolved=0) dedup stays.

## Principles (Lumi's contract — minimal disturbance)

- **Record everything, alert rarely.** Every failure lands in audit_log. An alert exists only when self-heal already lost.
- **Two-strike by design (KEEP the prior_fails>=1 gate).** One transient failure (Anthropic overload etc.) → silent record, catchup retries. Retry also fails → alert. The review bug is NOT the gate — it's that P5 kills the retry, so strike two never happens. Fix the chain, keep the gate.
- Fingerprint = stable low-cardinality token; exception text/sid/paths go in message=. One dashboard row per cause (hit_count counts repeats) — a deduped row is not disturbance, a flood is.
- The alert pipeline itself must not fail silently (fallback sink) — losing a record is worse than a quiet day.
- Catchup must never permanently park a sid (no terminal-less skip states). Skips are terminal, never alerted.

## Batch A — unbreak the chain (P0) — DONE 06/11 (merged f8b0c33; plus follow-up fix: final-audit failures scoped to current run's start stamp, so a successful retry no longer re-reads stale fail rows as partial)

1. catchup P5 fix · sessionstart_catchup.py `_classify`
   - in-flight iff: start newer than end_row AND no sessionend_extract terminal row (ok/skip/fail/partial/reset) with id > that start id AND start age < 15 min.
   - terminal row after start → fall through to states 2-5 (so fail/partial sids re-spawn, capped by MAX_FIRE).
   - stale start (>15 min, no terminal) → treat as died mid-run → spawn.
   - Tests: partial:digest sid re-spawns; in-flight within grace skips; stale start spawns; ok sid still skips.
2. make strike-two reachable · sessionend_async.py `_write_final_audit`
   - KEEP prior_fails>=1 gate (two-strike is the design). With P5 fixed, the chain works: fail → catchup respawn → second fail → alert. No behaviour change needed beyond A-1; just add a test proving the second failure alerts.
   - digest ok:0 joins the retry chain: count digest's "ok:0" as a partial (it currently counts as ok → never retried). First 0-row digest → silent record + catchup retry; second → the existing two-strike alert. Then DELETE the immediate digest_zero_write alert (it was a workaround for the dead chain, and it violates the disturbance contract).
3. add_alert fallback sink · repo.py `add_alert`
   - Wrap body; on any exception append JSON line to DATA_DIR/alerts-fallback.jsonl (mkdir ok) + stderr. Never raise.
   - sessionstart_catchup.main: on boot, drain alerts-fallback.jsonl back into alerts table (best-effort, then truncate).
4. aging flush in finally · aging.py `main`
   - Move pending_alerts flush into finally before conn.close(); audit INSERT failure no longer eats alerts.

## Batch B — correctness + coverage (P1) — DONE 06/15 (6a69709)

5. reconcile_ref scoping · sessionend_writers.py `seg_affect`
   - Candidate SELECT adds `AND date = ?`; if no same-day unresolved row, skip resolve + audit_log note (no cross-day guessing).
6. stable fingerprints · hooks.py 3 sites
   - sessionend_spawn_failed / atlas_hook_error / hook_dispatch_failed:{event}; exception → message=.
7. wx death escalation · synapse_wx/loop.py
   - Consecutive provider-death counter on MainLoop (reset on successful recv). >=3 with session_id set → AlertSink critical + one user bubble (provider.dead). _ensure_provider spawn failure routes the same counter.
8. stub diary unblock · daily_catchup.py `pending_days`
   - done-set excludes rows where content is the stub; daily re-runs day when digests exist.
9. missing-alert coverage adds (all stable-fingerprint deduped = one row max, no flood)
   - sync_loop._process exception → warn sync_loop_tick_failed:{target}, only after 3 consecutive failing ticks (a single bad tick self-heals 5s later) — closes MAP known-gap.
   - watcher SyncLoop/AtlasSweepLoop start failure → critical watcher_thread_start_failed (sync layer silently gone = actionable).
   - wx media: decrypt/upload/pdf failure → AlertSink warn media_{in|out}_failed, only on 2nd consecutive failure per kind.
   - seg_task_cand embedder-absent path → reuse semantic_dedup.warn_embedder_missing alert (currently audit-only).

## Batch C — false-positive diet (P2) — DONE 06/15 (6a69709)

10. digest_zero_write: superseded by A-2 (deleted; 0-row digest rides the two-strike retry chain).
11. daily_catchup_overflow: auto-resolve when pending_days() <= CATCHUP_MAX on a later run (aging pass or daily tail).
12. offsite backup: 1 retry after 30s before warn (iCloud mount latency).
13. drift dangling-delete: only warn after pairing TTL expiry AND path still absent.
14. backup.py docstring: drop stale "WAL" wording (DELETE mode is deliberate — DECISIONS.md).

## Lifecycle semantics (unchanged, documented)

- resolve = acknowledge, not fix: same (type,fingerprint) re-inserts on recurrence by design (anti-mute). Dashboard row shows hit_count so repeat offenders are visible.
- aging auto-resolve only for milestone_added (7d). No generic TTL — alerts represent unfixed state.

## Acceptance

- Kill sessionend_async mid-LLM-call → next SessionStart re-spawns it; second kill → critical alert on dashboard.
- Force digest LLMError once → NO alert, audit row only; catchup respawn fails again → warn alert (two-strike proven).
- Lock DB, fire add_alert → line lands in alerts-fallback.jsonl, drained next session.
- Legit short session → skip, zero alerts, catchup stays quiet.
- pytest green; live dry-run: `python -m marrow.sessionstart_catchup` against real DB shows expected spawn list only.
