# Marrow handover

> Phase-1 DONE. blocker root-fixed; #3/#4/#5/#12 shipped; #7 not-a-bug; #2 restored; #6 deferred to Phase 2. Next = Phase 2. Only residual: #8 timeout minor + launchd jobs awaiting Lumi load-gate. pytest 128 green, pushed.

## Phase 2 entry
- DESIGN Phase 2 = emotion + decay + sub-page render + people/preferences trigger-load; recall-module (vector + RRF fusion + embedder) first-built here.
- #6 events_vec embedder-id/dim provenance: add WITH embedder at recall-module build — see FUTURE `events_vec_embedder_provenance`. Fusion refs (urls there + `~/Desktop/NY/CLAUDE.md:10`): Ombre-Brain (DESIGN:229 weight-pool), claude-imprint (RRF vector/FTS/recency, DESIGN:259), cyberboss.
- embedder itself = fork #1 (still open).

## Residual (non-blocking)
- #8 timeout not process-group kill: `llm.py` `threading.Timer` kills main `claude`, orphan children possible — own focused TDD round.
- #8 lessons 2 stale rows — safe, leave (Lumi-intentional).
- All marrow launchd jobs (diary routine/catchup, jsonl-cleanup, db-backup) NOT launchctl-loaded — Lumi gate.

## Phase-1 shipped (verified on main, pushed)
- blocker: `is_headless` = assistant model-set ⊆ config `worker_models` (ADR-0004); `entrypoint` abandoned; `cleanup.py` follows.
- #3 diary same-day `--force` overwrite + `fcntl.flock` app-lock (DESIGN L188 net).
- #4 `backup.py` atomic `VACUUM INTO` + iCloud offsite + keep=14; `mw-db-backup.plist` daily 03:00.
- #5 `archive_events` mirrors one batch `audit_log` row.
- #12 session_end dashboard `PermissionError` → skip this regen (lossless, alert#11 sibling); alert#12 resolved.
- alert / thread / handoff id moved to line-front (was buried at line-end).
- #2 restored ids 453-456 (2 real hello sessions); 48 spawn rows correctly stayed purged.

## Don't redo / decided
- `entrypoint` NOT a headless marker — ADR-0004 supersedes.
- #7 `_routine_target` correct under the 04:00 boundary — do not "fix" it.
- #6 waits for embedder — never add an empty provenance column to the Phase-1 schema.
- `isolation:"worktree"` subagents branch from the origin baseline → always cherry-pick to main and real-run pytest there; never trust the worktree's own count.
- dashboard lives in `~/Desktop/NY` (Obsidian) — cannot move out of the TCC zone; EPERM degrade is the fix, not relocation.
