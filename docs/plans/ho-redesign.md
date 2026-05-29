# HO redesign — single all-in-one handover, diff-based

> 2026-05-30 · design locked with 念念. Replaces the earlier 3-file / scope-isolation
> draft (that whole approach is dropped).
> Template: ~/Desktop/handover-template.md · Prompt: ~/Desktop/handover-prompt-draft.md (STATE v2).

## Goal
Handover is context for the NEXT window (the AI = 金鱼), not a dashboard for 念念.
It tells a new session what each thread is mid-doing + how to continue — no bloat,
no clobber, hand-editable. The task table stays the human todo (dashboard); HO is
the AI's continuation memory.

## Locked decisions
- **ONE handover file, all-in-one.** No per-scope files, no scope isolation/routing.
  念念 crosses scopes freely (discusses uni / new projects inside ny); injection shows
  everything at once, never scope-filtered. It's a few lines — no attention cost.
- **scope = a label only.** DOING bullets carry `[Marrow]/[Study]/[Daily]/…` tags for
  visual grouping; sonnet picks the tag from content. No code routes on it.
- **task table unchanged & universal.** A GP appointment raised in a study session
  still lands as Appointment — scope NEVER discards a task. The existing dashboard
  render + sort (due group / no-due group by insert order, category priority) is
  ALREADY BUILT — DO NOT touch it. sonnet only ticks/adds; code renders + sorts.
- **task tick BY ID.** active_tasks fed to sonnet WITH db id; sonnet emits
  `{id, status:"done"}`; code flips `WHERE id=?`. Kills the verbatim-title miss.
  New adds (no id) keep going through the existing cosine dedup.
- **STATE prompt = one judgement, two segments.** Judge "what happened today" once,
  then split: SEGMENT A → task (coarse milestone, json), SEGMENT B → DOING diff
  (fine continuation). Never decide completion twice. The ~/Desktop draft is the spec;
  keep 念念's examples verbatim.
- **DOING = open+plan merged**, one bullet per thread:
  `[scope] title / current state / next step / reference`. Diff verbs
  CLOSE/UPDATE/KEEP/ADD against the current Doing ids.
- **Done = rolling 24h**, not next-day wipe. CLOSE moves a bullet to `## Done`; code
  drops entries older than 24h (so a morning open isn't blank).
- **Lumi's Note = hands off.** sonnet never appends/rewrites it; only removes a note
  line that is clearly done. The Note is 念念's task list FOR the next window's AI.
- **sessionstart injects one line**: "do not ignore Lumi's Note — it's your to-do."
- **`[N]` concept dropped** for now — 念念 wants to see behaviour without it first.

## Sections per file (template)
`## Done` (rolling 24h) · `## Doing` (open+plan merged, scope-tagged) · `## Lumi's Note`.

## Module changes
1. **sessionend_prompts.py** — replace STATE_PROMPT body with v2 (the draft). New
   parser for SEGMENT A (task json) + SEGMENT B (DOING diff). NARRATIVE untouched.
2. **sessionend_async.py**
   - `_session_events_text:169` — splice `[HH:MM]` (local) into each line prefix.
   - `_load_active_tasks_for_sonnet:226` — add id → `- [#12] <title> (<category>)`.
   - `_load_prior_handover_for_sonnet:238` — read the single file's `## Doing` bullets
     WITH ids; feed as `{doing}`. Replace the 4-section reader.
   - new `_load_git_log(since_ts)` — `git log --since=<last HO ts> --format=%s` when in a
     repo; '' otherwise. Evidence for CLOSE, not dumped into the file.
3. **sessionend_writers.py**
   - `seg_task_cand:131` — id branch: `{id,status}` → `UPDATE … WHERE id=?`; no id →
     INSERT + existing cosine dedup. DO NOT change sort/render.
   - `seg_handover` — switch from 4-section full-write to diff-apply on the single file.
   - stop `append_progress` (no Done block to mirror). PROGRESS.md frozen, not deleted.
4. **handover apply** (likely new `handover_diff.py`, keep render <300 LOC)
   - `apply_diff(diff)`: CLOSE → move id-line to `## Done` (stamp ts); UPDATE → replace
     text; KEEP → no-op; ADD → append with fresh id.
   - port tombstone (hand-delete stays gone) + user_added (hand-add gets an id) from
     `handover_render.py:196-241`.
   - blocking flock w/ timeout so concurrent closes serialize (diff is additive, no clobber).
   - `## Done` cleanup: drop entries older than 24h, on write or sessionstart.
   - Note: only remove note lines that are done; never touch the rest.
5. **injection — keep `@import`, no hook cap.** The single file is pulled into context
   by the existing global-CLAUDE.md `@~/.config/marrow/handover.md` line (same as now).
   `@import` is not hook stdout, so the 10000-char limit doesn't apply; the file is a
   few lines anyway. hooks.py `_handoff_text` keeps the task/alert/affect block as today
   and ADDS one line: "don't ignore Lumi's Note — it's your to-do." No @import removal.
6. **symlink, NO migration**
   - seed ONE empty file from template at `~/.config/marrow/handover/handover.md`.
   - symlink into CC-Lab root, Study dir, Desktop/NY so it opens anywhere — same file.
   - current handover is stale/empty (no update since ~20:00 yesterday) — start fresh,
     no migration. retire the old file + `.bak`.

## Test
- dry-run sessionend per kind (project w/ commits, study, mixed ny chat) → inspect
  task ticks (by id) + DOING diff apply + Done 24h roll-off.
- tick robustness: a reworded title still ticks via id.
- hand-edit survival: delete a Doing line + add one by hand → next write respects both.
- Note: a done note line gets removed; the rest untouched; injection still shows it.

## Open (decide at implementation)
- cwd in hook input — now only needed for git_log repo detection, not routing.
- git log window — `--since last HO ts`; double-count harmless (CLOSE idempotent by id).
