# HO redesign — implementation brief (self-contained for a worktree agent)

> 2026-05-30. Design locked in `docs/plans/ho-redesign.md`. Spec drafts:
> `~/Desktop/handover-prompt-draft.md` (STATE v2 prompt) + `~/Desktop/handover-template.md` (new file format).
> THIS doc resolves every ambiguity the agent needs. Where this doc and ho-redesign.md
> disagree, THIS doc wins (it was written after reading the actual code).

## What you are building
The sessionend handover pipeline currently writes a 4-section file
(`## Done / ## Open / ## Plan / ## Reference`) by full-rewrite each session. Replace it
with a **diff-based, 3-section** file (`## Done` rolling-24h / `## Doing` open+plan merged /
`## Lumi's Note` hand-managed). One sonnet call judges "what happened this session" ONCE
and emits two segments: a TASK board update (id-based tick) and a DOING diff
(CLOSE/UPDATE/KEEP/ADD against existing thread ids).

Goal of the file: it is **continuation memory for the next AI window**, not a dashboard.
The task table stays the human todo (untouched render/sort). HO tells a fresh session what
each thread is mid-doing and how to continue.

## HARD BOUNDARIES — do not cross (read first)
- **Worktree only.** First action: `pwd && git rev-parse --show-toplevel` — confirm you are
  under `.claude/worktrees/agent-*`. If not, STOP and report.
- **Never touch production data files.** Do NOT read/write/seed/symlink anything under
  `~/.config/marrow/` (that is the live runtime: real db, real handover.md). Tests already
  redirect `config.DATA_DIR` to a tmp dir (see `tests/conftest.py`), so all dry-runs are safe.
- **Path not migrated; symlinks are the main session's job.** `_RENDERED_PATH` stays
  `config.DATA_DIR / "handover.md"` — the `@~/.config/marrow/handover.md` import already points
  here. 3 symlinks (Desktop/NY, iCloud Study, CC-Lab/marrow) → the real file are a deploy step
  the MAIN session does after merge — NOT in your scope. They touch production paths AND must
  first clear stale real files (NY + marrow already hold old handover.md; marrow's is git-tracked
  + gitignored and needs `git rm --cached`). Do not create or touch them.
- **git_log IS built this round (M2/M4).** `_load_git_log(cwd, since_ts)` shells
  `git -C <cwd> log` and degrades to `""` off-repo (study/daily sessions have no commits → fall
  back to sonnet reading the transcript). cwd flows hook→async via a new `--cwd` arg.
- **Lumi's Note: remove-done ONLY.** Code NEVER appends/rewrites/reorders Note. The ONE allowed
  edit: delete a Note line that sonnet flags as clearly completed (a commit, or an explicit
  completion in the transcript). No evidence → leave the line. Everything else is verbatim
  passthrough. This is NOT hands-off — done lines must get cleared so they don't pile up.
- **Do not touch task render/sort.** `seg_task_cand` only ticks/inserts; dashboard render +
  category-priority sort are already built and correct. Don't reorder/format the task list.
- Commit per module on your branch. No push, no merge to main. The main session merges.
- Standard subagent contract (see `.claude/rules/agent-dispatch.md`): cite file:line, no
  guessing, report "could not verify X" rather than inventing.

## Ground facts (verified)
- `config.DATA_DIR == ~/.config/marrow`; `handover_render._RENDERED_PATH == DATA_DIR/handover.md`.
- pytest baseline on `main` (this commit): **811 passed, 1 skipped** via `.venv/bin/python -m pytest -q`.
  Use that interpreter. Some 4-section tests WILL be rewritten — that's expected. Net count
  should stay ≥ baseline (you add more than you remove). Never leave a red suite.
- Cache contract: `STATE_PROMPT` and `NARRATIVE_PROMPT` both start with byte-identical
  `_TRANSCRIPT_BLOCK`. Both calls use the SAME `events_text`. If you add `[HH:MM]` prefixes,
  add them to `events_text` once (both calls inherit it) — the prefix stays identical, cache holds.

## New file format (deploy to `marrow/handover_template.md`)
```
# Handover — {{YYYY-MM-DD HH:MM}}

## Done
> Resolved this session, rolling 24h. Code-managed — don't hand-edit.
- N/A

## Doing
> Open threads (open+plan merged). The <!-- id:N --> comment is code-managed — don't hand-write it.
> Format per thread:
>   N. [scope] - <title>
>     - Current: <state>
>     - Next: <next step / N/A>
>     - Reference: <path / url / N/A>
- N/A

## Lumi's Note
> Freeform, yours. Auto-write only removes lines you've clearly completed — never adds/rewrites.
- N/A
```
- `N.` is a DISPLAY ordinal — code re-numbers 1,2,3… on every render. The stable identity is
  `<!-- id:N -->` appended to each thread block.
- A rendered thread looks like:
  ```
  1. [Marrow] - Auto handover feature
    - Current: prompt + plan + template done
    - Next: py + test
    - Reference: sessionend_prompts.py:30
  <!-- id:7 -->
  ```
- `## Done` entries are single lines stamped with epoch: `- [scope] <title> — <current> <!-- done:1717000000 -->`.

## The id-based diff design (core — replaces hash tombstone for Doing)
Each Doing thread carries a stable `<!-- id:N -->`. Identity = the id, NOT a text hash. This
replaces the old `handover_norm` hash + `tombstone` machinery FOR THE DOING SECTION.

**id allocation:** inside the flock, scan all `<!-- id:N -->` in BOTH `## Doing` and `## Done`,
new ids = `max(N)+1` (start at 1 if none). Monotonic, never reuse a closed id.

**`{doing}` fed to sonnet** (in `_load_prior_handover_for_sonnet`): each thread prefixed with
its id so sonnet can reference it:
```
[#7] [Marrow] - Auto handover feature
  - Current: ...
  - Next: ...
  - Reference: ...
```

**`apply_diff(conn, sid, diff)` — inside flock, atomic write:**
1. Read current file → parse: `doing: dict[int, block_text]`, `done: list[(line, epoch)]`,
   `note: raw passthrough`.
2. Hand-edit reconciliation vs last snapshot (`_load_last_snapshot_body`):
   - id present in snapshot's Doing but absent in current file → user hand-deleted →
     remember it; sonnet's KEEP/UPDATE must NOT revive it (id-tombstone).
   - thread block in current Doing with NO `<!-- id -->` → user hand-added → assign a fresh id, keep.
3. Apply verdicts:
   - `CLOSE id` → remove from doing; emit a `## Done` line `- [scope] <title> — <current> <!-- done:NOW -->`.
   - `UPDATE id <block>` → replace that id's block text (id unchanged). id missing → ignore.
   - `KEEP id` → no-op. id missing → ignore.
   - `ADD <block>` → assign fresh id, append to doing.
   - **Any existing id the diff did NOT mention → keep it** (defensive: a sonnet omission must
     never silently drop an open thread).
4. `## Done` 24h roll-off: drop entries with `done:EPOCH` older than `now-24h`. Run on every
   write (and it's idempotent on sessionstart-time reads — but you do NOT need a sessionstart
   hook for this round; write-time cleanup is enough).
5. Note: verbatim passthrough EXCEPT delete the lines sonnet listed in `NOTE_DONE` (see M1).
   Match each `NOTE_DONE` line against current Note lines via `handover_norm.hash_bullet`
   (tolerant of rephrase/punctuation); drop matches, keep the rest byte-for-byte. Empty
   `NOTE_DONE` → Note untouched. Code never adds or rewrites a Note line.
6. Concurrency: `CLOSE` is idempotent by id; `UPDATE/KEEP` of an already-closed/deleted id is
   a no-op. flock serializes concurrent sessionend writers. Reuse `_acquire_flock` /
   `_release_flock` / `_atomic_write` and the snapshot-audit pattern from `handover_render.py`.

Keep `handover_diff.py` (or whatever you name the apply module) under 300 LOC. You MAY keep it
inside `handover_render.py` if it stays under the soft cap; a new module is cleaner.

## Modules

### M1 — prompt + parsers (`marrow/sessionend_prompts.py`)
- Replace the ENTIRE `STATE_PROMPT` body with the v2 spec in `~/Desktop/handover-prompt-draft.md`
  (one judgement → SEGMENT A TASK json + SEGMENT B DOING_DIFF). Keep `{sid} {events}` and add
  format fields `{active_tasks} {doing} {git_log} {note}`. Keep 念念's examples verbatim.
- **Note section in the prompt — override the draft's "HANDS OFF" wording.** Feed the current
  Note via a `{note}` input block (`===NOTE===`/`===END===`). Sonnet emits a `===NOTE_DONE===`
  segment: one VERBATIM Note line per row that it judges clearly completed this session
  (evidence = a commit in `{git_log}` or an explicit completion in the transcript). No evidence
  / any doubt → omit it. Sonnet may ONLY list lines to delete — never add, rewrite, or reorder.
  Empty → `N/A`. Make this rule explicit and tight in the prompt so误删 needs hard proof.
- `git_log` is real input now (see M2). The prompt already uses it as CLOSE evidence; keep that.
- `NARRATIVE_PROMPT` is UNCHANGED.
- New parsers:
  - `parse_task_rows(raw) -> list[dict]` — JSON list between `===TASK===`/`===END===`.
    Reuse `candidates.extract_block(raw, "TASK")`.
  - `parse_doing_diff(raw) -> dict` — slice `===DOING_DIFF===`/`===END===`, parse the
    `CLOSE:`/`KEEP:`/`UPDATE:`/`ADD:` sub-blocks into
    `{"close": [int], "keep": [int], "update": [{"id": int, "block": str}], "add": [str]}`.
    Be tolerant: missing sub-block → empty. Bad id token → skip it, don't crash.
  - `parse_note_done(raw) -> list[str]` — lines between `===NOTE_DONE===`/`===END===`; drop
    `N/A` / empty. These are the Note lines to remove.
  - DELETE `parse_handover_output` (4-section) and its `_slice` 4-section callers, OR keep
    `_slice` if still used; remove the 4-section semantics.

### M2 — sonnet loaders + cwd arg (`marrow/sessionend_async.py`)
- `_load_active_tasks_for_sonnet:226` → line form `- [#{id}] {title} ({category})` (add id).
- `_load_prior_handover_for_sonnet:238` → read the single file's `## Doing`, parse thread blocks
  WITH their `<!-- id:N -->`, return the `[#id] …` form above. Replace the 4-section reader.
- NEW `_load_note() -> str` → return the `## Lumi's Note` body verbatim (for the `{note}`
  prompt input). Empty/missing → `N/A`.
- NEW `_load_git_log(cwd, since_ts) -> str` — REAL impl this round:
  - `if not cwd: return ""`.
  - `git -C <cwd> log --since=@<since_ts> --format=%s` via `subprocess.run(..., timeout=5,
    capture_output=True, text=True)`. Non-zero return (off-repo) or any exception → `""`.
  - `since_ts` = the `ts:` epoch in the current handover.md `<!-- handover: ready ... ts:N -->`
    stamp (last HO write). No stamp → default to `now - 24h`. Double-counting is harmless
    (CLOSE is idempotent by id).
- `main()` → parse a new optional `--cwd <path>` arg alongside `--sid`. Thread it into
  `_run_extraction` → `_load_git_log`.
- `_session_events_text:169` → prefix each line with local `[HH:MM]` derived from the row
  timestamp (Australia/Melbourne). One transform on `events_text` (both calls inherit it).
- `_run_extraction`: pass `doing=`, `note=`, and `git_log=_load_git_log(cwd, since_ts)` into
  `STATE_PROMPT.format(...)`. Drop the `append_progress` writer call + its `parse_handover_output`
  done_block extraction. Remove `"progress"` from the writer/failure bookkeeping. PROGRESS.md is
  FROZEN (file + function stay; just no longer invoked).

### M3 — writers + diff-apply (`marrow/sessionend_writers.py` + new diff module)
- `seg_task_cand:131` → id branch FIRST:
  - row has `"id"` and `status == "done"` → `UPDATE tasks SET status='done', updated_at=? WHERE id=?`.
  - row has a `title`, no id → existing INSERT + cosine-dedup path (unchanged).
  - Keep `_normalise_category`, dedup, 24h-window logic for new adds.
- `seg_handover` → parse DOING_DIFF + NOTE_DONE, call `apply_diff(conn, sid, diff, note_done)`.
  Drop the 4-section `write_handover_full` path.
- Implement `apply_diff` per the design above. Port the snapshot/flock/atomic-write plumbing
  from `handover_render.py:295` (`write_handover_full`) and the snapshot-audit helpers.
- `append_progress` stays defined (frozen), just not called.

### M4 — hooks.py: cwd→async + Note reminder (`marrow/hooks.py`)
- `session_end`, popen at line 319: add `"--cwd", inp.get("cwd") or ""` to the
  `sessionend_async` argv so git_log can locate the repo. cc's hook input carries `cwd`; if
  absent, the empty string makes `_load_git_log` return `""` — safe. (Quick first-check: confirm
  the field by logging `inp` once; fall back to `""` regardless.)
- `session_start` payload (~line 244, `_handoff_text` or assembly): append ONE English line
  reminding that `## Lumi's Note` is the new window's to-do, don't ignore it. Don't touch the
  task/alert/affect blocks or the 6000-char cap.

## Tests (TDD — write/adjust before or alongside each module)
- Rewrite 4-section assumptions in `tests/test_handover_render.py` + `tests/test_sessionend_async.py`
  to the 3-section model. The old Done/Open/Plan/Reference structure is gone.
- New coverage (add these):
  - **task tick by id**: a reworded active title still ticks via `{id,status:done}` → `WHERE id=?`.
  - **diff apply**: CLOSE moves an id to `## Done`; UPDATE replaces text keeping id; KEEP is no-op;
    ADD assigns a fresh monotonic id; an unmentioned existing id survives.
  - **Done 24h roll-off**: an entry with `done:` epoch > 24h ago is dropped on next write; a fresh
    one stays.
  - **hand-edit survival**: hand-delete a Doing block (id gone) → not revived; hand-add a no-id
    block → gets an id and survives the next write.
  - **Note remove-done**: a line in NOTE_DONE is removed; lines NOT listed stay byte-identical;
    empty NOTE_DONE leaves Note untouched; code never adds/rewrites a Note line.
  - **git_log loader**: off-repo cwd → ""; in-repo → commit subjects since the ts stamp;
    bad/missing cwd → "" (no crash).
  - **doing diff parser**: malformed/missing sub-blocks degrade gracefully.
- Final gate: `.venv/bin/python -m pytest -q` green, count ≥ 811. Fix every failure before
  reporting — do not report a red suite as done.

## When stuck
- If a design point is genuinely ambiguous beyond this brief, pick the SAFER option (no data
  loss, no production touch), implement it, and flag the choice in your final report.
- If you cannot get green after a real attempt, commit progress, and report exactly which test
  fails + your last diagnosis. Do not thrash or invent a "lazy alternative".

## Final report (≤ what's needed)
- Per module: done / partial / blocked, with the commit sha.
- Test delta: baseline 811 → final N (passed/skipped), and which tests you rewrote vs added.
- Any decision you made under "When stuck".
- Confirm you BUILT: id-based task tick, DOING diff-apply (CLOSE/UPDATE/KEEP/ADD), Done 24h
  roll-off, hand-edit survival, git_log loader + `--cwd` arg, Note remove-done, sessionstart
  Note reminder, frozen PROGRESS.
- Confirm you did NOT touch: `~/.config/marrow/*` production files, the 3 symlinks, task
  render/sort — those are the MAIN session's post-merge deploy step. All dry-runs used the
  conftest tmp dirs.
