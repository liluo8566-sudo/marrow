# Marrow handover

> CRITICAL: entrypoint-marker false — `entrypoint=="sdk-cli"` ≠ headless. Bleed-stopped; blocker.

## Blocker: entrypoint signal wrong

Census (653 jsonl): sdk-cli 389 / cli 259 / vscode 4 / desktop 1. Assumption in `acafd60`+`8f2747f`+`b46deb1` wrong. ~51 min live damage: real `.jsonl` at risk.

Bleed-stop (`24830a3`): launchctl bootout, `is_headless()` hard-False, cleanup verified no-op (del 0 / kept 653). Previous purge (518→464) used same flawed judge — 54 rows may be recoverable from `marrow.db.bak-20260518-220058`.

## State (ALL PUSHED — marrow main @ c4a12b4)

- `c4a12b4` docs(notes): parse-bug two layers + re-pin 2.1.142
- `33d6393` docs: bleed-stop handover + PROGRESS delta
- `24830a3` fix!: BLEED-STOP — is_headless hard-False, entrypoint signal wrong
- `8f2747f`+`b46deb1` cleanup.py reaper + launchd (premise reversed)
- `acafd60` fix(transcript): entrypoint headless marker (REVERSED by 24830a3)
- `~/.claude`: prompt-lint.py `-p`→stream-json — unpushed

pytest 91/91. events 479 (backup `marrow.db.bak-20260518-220058`, was 518).

## Done & verified (both windows)

- Alert #11 fake-warn: `transcript.clean` FileNotFound → []; stale alert resolved; 15 lesson-type alerts deleted; dashboard regenerated clean.
- Stitch cross-04:00 ordering: `_local_md()` adds date to span tag; verified on real 5-17 data.
- 5-17 diary overwritten with reviewed dry-run narrative (4 kept sessions). Dry-run script kept at `/tmp/mw_dryrun_diary.py` (no DB write; arg=date).
- `CLAUDE.md` rule added: push full DB-only body to Lumi after a run.

## Open — Lumi prompt-tuning (diary.py prompts are hers)

1. `DIARY_PROMPT` "no study/coding detail" was too weak (sonnet dropped all work). `f08e08e` added strict-discard + banned-phrase — confirm with Lumi whether this is now resolved or needs the "one line: what done + outcome" rule.
2. `DIGEST_SHORT` mis-SKIP concern — **CLOSED**: this window empirically confirmed `8a9d1efd` (5-turn /schedule discussion, no outcome) is a *correct* SKIP, not a miss. Not a bug.
3. `DIGEST_LONG` haiku still wraps a meta shell ("per diary compress rules…"). Prompt wording fix, core craft unaffected.

Test loop: clear `diary` row for a date in `~/.config/marrow/marrow.db`, `diary.run(day=…)`, show diary text + KEEP/SKIP/DROP. (llm.py records no usage — FUTURE.)

## Open — next window

1. **Recover wrongly-purged events (review window owns).** Previous 518→479 purge used the flawed entrypoint judge — ~39+ rows may be REAL conversation. Backup `~/.config/marrow/marrow.db.bak-20260518-220058`. Review window verifies which rows are real and restores.
2. **Real headless signal — step4 / ADR-0003.** cleanup delete-behaviour stays no-op until a true headless signal exists; rewrite cleanup.py + test_cleanup.py against it then. Do NOT re-introduce any sdk-cli→delete logic before that.
3. **2.1.142 takes effect on next session restart** (this window ran 2.1.143). After restart, watch whether parse failure drops past ~100k context; if it still bleeds, decoder layer confirmed (version-independent).

## Shipped (done, keep)

- `~/.claude/hooks/prompt-lint.py`: `-p`→stream-json + isolation, system in user content, off 6/15 pool, dependency-free. Live-verified 4.5s correct. Unaffected by the cleanup mess. (local commit, unpushed per rule)
- ADR-0003 scheduling: 3 jobs + bootstrap-from-repo wording.
- `cleanup.py` + `deploy/mw-jsonl-cleanup.plist` (com.marrow.jsonl-cleanup) — harmless no-op under is_headless hard-False; left in place.

## Don't redo / decided

- sdk-cli `entrypoint` is NOT a headless marker — it covers real clawbot/Task-agent/worktree human sessions. Pollution-drop is REVERSED (`24830a3`). Wait for step4 real signal.
- `CLAUDE_CONFIG_DIR` redirect rejected: fresh config dir loses OAuth/keychain auth (three-way verified). Don't reopen.
- Cleanup must be a standalone job, never inside diary routine/catchup.
- diary.py prompts (DIGEST_SHORT/LONG, STITCH, DIARY_PROMPT) Lumi-owned; restore from `f08e08e`/`bcde095` if reverted; don't rewrite.
- prompt-lint stream-json migration verified correct; don't revert to `-p`.
- /clear does NOT change session_id (same jsonl). lessons removal intentional (Lumi).

## Suggested skills

- `/loop` — if Lumi re-enters diary prompt-tuning.
