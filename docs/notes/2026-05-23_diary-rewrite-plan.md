# 2.5c daily rewrite — execution runbook (2026-05-23)

> Source of decisions: `handover.md` (this same date).
> This file = step-by-step recipe. Next window: read `handover.md` + this file + `docs/notes/lumi-prompt-source.md`, then execute top-to-bottom.
> Old plan (rejected blockers + interim double-write + rollup/extract/prompts naming + diary.py shim) discarded — see handover §"plan §0 4 blockers" + §"Naming overhaul".

## Goal — single commit lands all of below

- new `daily.py` + `daily_catchup.py` (replace 900-LoC `diary.py`)
- `sessionend_async.py` grows 7 sonnet segments + 7 prompt bodies + parse + DB writes
- migration: affect 3 new cols + threads→tasks rename + new `session_digests` table
- ollama tier stripped
- 3 plist Hour + path updates + launchctl reload
- day boundary 6AM

## Pre-flight

- `cd /Users/Gabrielle/cc-lab/marrow`
- `git status` clean
- Read: `docs/notes/lumi-prompt-source.md` (Unresolved field spec, verbatim)
- Read: `docs/notes/2026-05-23_sessionend-llm-pipeline.md` §2.3 (segment topology), §4.2 (narrative atomic append), §16 (DIGEST density rules)
- Read: `marrow/diary.py:55-194` (DIGEST_SHORT + DIGEST_LONG + DIARY_PROMPT — sources for paste)

## Step 1 — Schema migration (~50 LoC)

File: `marrow/storage.py`

Add `_migrate_to_v2(conn)`:
- guard: `PRAGMA user_version` ≥ 2 → return
- transactional executescript:
  - `ALTER TABLE affect ADD COLUMN unresolved INTEGER DEFAULT 0`
  - `ALTER TABLE affect ADD COLUMN reconcile_ref INTEGER REFERENCES affect(id)`
  - `ALTER TABLE affect ADD COLUMN resolved_at TEXT`
  - `DROP TABLE IF EXISTS threads` (0 rows, no backfill)
  - `CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','done','archived')), due TEXT, completed_at TEXT, ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)`
  - `CREATE TABLE IF NOT EXISTS session_digests (sid TEXT PRIMARY KEY, date TEXT NOT NULL, text TEXT NOT NULL, ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)`
  - `CREATE INDEX IF NOT EXISTS idx_session_digests_date ON session_digests(date)`
  - `PRAGMA user_version = 2`

Call from `storage.connect()` after existing CREATE block. Update `affect_live` view if it does `SELECT col1, col2, ...` (likely `SELECT *` → auto-inherits).

Test: `tests/test_storage.py::test_migrate_v2` — fresh DB → connect → version=2 + 3 new affect cols + tasks + session_digests exist + threads gone. Re-connect → no-op (version guard).

Verify: `pytest tests/test_storage.py tests/test_repo.py` green before next step.

## Step 2 — sessionend_async.py 7 segments (~600 LoC total)

File: `marrow/sessionend_async.py`

### 2a Structure

Replace ping-pong block (current L82-107) with:
- 7 module-top prompt constants: `_PROMPT_AFFECT`, `_PROMPT_ENTITY_CAND`, `_PROMPT_THREAD_CAND`, `_PROMPT_MILESTONE_CAND`, `_PROMPT_MEMES_CAND`, `_PROMPT_DIGEST`, `_PROMPT_NARRATIVE`
- `_TX_OPEN` / `_TX_CLOSE` / `_fence` transcript fence helpers (port from `diary.py:32-42`)
- `_events_text(conn, sid)` → fenced sessions string
- 7 segment functions: `_seg_affect(conn, client, sid, text)` etc — each: prompt call → parse → DB write → returns (ok, err_str)
- main loop: for each segment in order, try → on fail keep going, collect failures
- final audit_log: `action='sessionend_extract' summary='ok'` or `'partial:<seg1,seg2>'` or `'fail:all'`
- one segment fail does NOT block others (each is independent extract)

### 2b Per-segment specs

**`_seg_affect`** — per-episode affect + Unresolved + reconcile_prev
- Prompt body: include
  - field spec for valence / arousal / importance / label / entities / event_hint (style from `diary.py:267-279`)
  - `unresolved` field block: **paste verbatim from `docs/notes/lumi-prompt-source.md` §Unresolved**
  - `reconcile_prev` field block: Stellan drafts following the Unresolved Include/Exclude/N/A structure
  - EXCLUDE rule (relocated L3): do NOT record affect for coding/debug arguments — Lumi's debugging frustration + Stellan's tech responses are noise, not signal
- Output marker: `===AFFECT===\n[{...},{...}]\n===END===`
- Parse: extract JSON between markers
- DB write to `affect`:
  - `importance = max(1, min(5, int(raw.importance)))`
  - populate `unresolved` (0/1) + `reconcile_prev` (used at write time, not stored)
  - if `reconcile_prev == True`: `SELECT id FROM affect WHERE unresolved=1 AND resolved_at IS NULL ORDER BY ts DESC LIMIT 1` → set `reconcile_ref` on new row + `UPDATE affect SET resolved_at = ? WHERE id = reconcile_ref` (atomic txn)
- Audit row: `action='sessionend_extract' segment='affect' summary='ok|fail:<E>'`

**`_seg_entity_cand`** — entities conf ≥ 0.8
- Prompt body: extract people / preferences / places mentioned. Output JSON `[{name, kind, conf, note}]`. kind ∈ {person, pref, place}.
- Parse: JSON list
- DB write: `entities` table — INSERT WHERE conf ≥ 0.8. Dedup by (name, kind). Reuse pattern from `diary.py:659-697`.

**`_seg_thread_cand`** — work threads → tasks (relies on step 1 tasks table)
- Prompt body: extract active work threads / TODOs mentioned. Output JSON `[{title, status, due, completed_at}]`. status ∈ {active, done}.
- DB write: `tasks` — INSERT new rows; update existing tasks if (title) matches and status changed.

**`_seg_milestone_cand`** — milestones conf ≥ 0.85
- Prompt body: extract life-shaping events (graduation, breakup, job change, major move, family death). Output JSON `[{title, ts, conf, note}]`.
- DB write: `milestones` — INSERT WHERE conf ≥ 0.85. Add dashboard alert via `repo.add_alert(priority='warn', source='milestone_candidate', label=title)`. Auto-confirm 7d undeleted lands in Phase 5 aging code — out of scope here.

**`_seg_memes_cand`** — memes conf ≥ 0.7 + use_count
- Prompt body: extract memes / inside jokes / coined terms / persona shorthand. Output JSON `[{key, definition, conf}]`.
- DB write: `memes` — INSERT IF NOT EXISTS; if exists increment `use_count`. Auto-promote dormant→active on use_count ≥ 3.

**`_seg_digest`** — flexible-length narrative per §16.1
- Prompt body: Stellan drafts by **merging old `diary.py:55-85` `DIGEST_SHORT` + `diary.py:87-116` `DIGEST_LONG`**, replace haiku-specific lines, apply §16.1 density:
  - task-heavy session → compress ≥80% (output ≤20% chars; outline form OK)
  - daily-chat session → preserve ~80% (output ~80% chars; verbatim dialogue retained, no work-style collapse)
  - sonnet decides ratio per turn-by-turn work-vs-chat density
  - second person (你/老婆) preserved; (心理活动) blocks do NOT proliferate
  - No SKIP path — `_user_event_count ≤ skip_turn_threshold` upstream guard already prevents short sessions
- Parse: plain text body
- DB write: `session_digests (sid, date, text)` — INSERT OR REPLACE keyed on sid. date = session start date by 6AM boundary.

**`_seg_narrative`** — handover async narrative per pipeline §4.2
- Prompt body: Stellan drafts. Tone: continuation of the day's voice for next session start; mention salient unfinished threads + emotional arc; CN dominant; second person.
- Parse: plain text body
- File write: read `~/.config/marrow/handover.md` (sessionend hook output, NOT this repo's handover.md). Locate `<!-- narrative: pending sid:<sid> -->` stamp. Atomic append below: `<!-- narrative: ready sid:<sid> ts:<unix> -->\n{prose}\n`. Pipeline §4.2 sid-mismatch handling: if current skeleton sid differs from this segment's sid, append with explicit lag label `(narrative describes sid=<X>, current skeleton sid=<Y>)`.

### 2c Verification per segment

After EACH segment ship: paste full prompt body + `marrow/sessionend_async.py:L<start>-L<end>` into chat for Lumi.

Live test after all 7 wired:
- Pick latest real sid: `sqlite3 ~/.config/marrow/marrow.db "SELECT DISTINCT session_id FROM events ORDER BY ts DESC LIMIT 1"`
- `python -m marrow.sessionend_async --sid <sid>` → check audit row `'ok'` or `'partial:<segs>'`
- Spot check: `sqlite3 ~/.config/marrow/marrow.db "SELECT * FROM session_digests WHERE sid='<sid>'"` and similar for affect/entities/tasks/milestones/memes

## Step 3 — daily.py (~150 LoC)

File: `marrow/daily.py` (NEW)

- `DIARY_PROMPT` = verbatim copy of old `diary.py:137-194` (no edit; Lumi-owned text)
- `_TZ = ZoneInfo("Australia/Melbourne")`, `_CUTOFF_H = 6` (was 4)
- `_to_local(ts)` / `_diary_day(date)` / `_routine_target()` — port from `diary.py:328-349`, swap cutoff
- `read_affect(conn, date)` → list of dicts from `affect_live WHERE date(ts) = ?`
- `read_digests(conn, date)` → list of texts from `session_digests WHERE date = ? ORDER BY ts`
- `run_day(conn, date, llm, *, force=False) -> bool`:
  - guard: if `diary` row exists for date and not force → return False
  - read affect rows + digest texts
  - assemble: f"{digests joined with `\\n\\n---\\n\\n`}\\n\\nAFFECT summary: {labels list}"
  - sonnet call: `llm.call(role='daily', body=DIARY_PROMPT.format(date=date, digest=material), tier='mid')`
  - atomic txn: DELETE WHERE date + INSERT diary row + audit_log marker
  - returns True
- `run(conn, llm, *, day=None, catchup=False, force=False) -> list[str]`:
  - day=None → `_routine_target()` (yesterday by 6AM boundary)
  - catchup=True → loop `daily_catchup.pending_days(conn, 7)` capped at `CATCHUP_MAX=3`
  - return list of dates written
- `main(argv) -> int`:
  - flags: `--day YYYY-MM-DD`, `--catchup`, `--force`
  - acquire `daily_catchup._app_lock`
  - construct `LLMClient` + `storage.connect` + call `run`
  - log + exit code

Tests: `tests/test_daily.py` — copy + rewrite from old `tests/test_diary.py`. Fixtures: relative dates, mock LLM returning fixed prose. Verify: idempotency, force, catchup loop, lock contention.

## Step 4 — daily_catchup.py (~100 LoC)

File: `marrow/daily_catchup.py` (NEW)

- `CATCHUP_WINDOW_DAYS = 7`, `CATCHUP_MAX = 3`
- `pending_days(conn, window_days) -> list[str]` — days in [today-window, today-1] with events but no diary row (port from `diary.py:372-405`)
- `day_events(conn, date) -> list[dict]` — events filtered by 6AM boundary
- `_has_diary(conn, date) -> bool`
- `_app_lock(path=None, *, blocking=True)` — fcntl flock context manager, port from `diary.py:352-369`. Default path `~/.config/marrow/daily.lock`.

Tests: `tests/test_daily_catchup.py`.

## Step 5 — Delete diary.py + reference migration

- `grep -rn "from marrow.diary\\|from .diary\\|import marrow.diary\\|marrow\\.diary\\b" marrow/ tests/ deploy/ docs/`
- For each:
  - `tests/test_diary.py` → rename `tests/test_daily.py`, fix imports (`from marrow import daily`)
  - `marrow/sessionstart_catchup.py` — change `from .diary import ...` → `from . import daily, daily_catchup`
  - `marrow/dashboard.py` / `marrow/top_sections.py` / `marrow/cli.py` if referenced → swap
  - `deploy/mw-diary-*.plist` — handled in step 8
- `git rm marrow/diary.py`

## Step 6 — P8 ollama strip

`marrow/llm.py`:
- delete `_MUTE_OLLAMA` flag (~L56-61)
- delete chain-build ollama-filter branch (~L90-94)
- delete `if kind == "ollama": return self._run_ollama(...)` dispatch (~L138-139)
- delete `_run_ollama` method body (~L330-347)
- module docstring (L2): drop ` → fallback → emergency` → `default` only

`marrow/config.default.toml`:
- L30: `emergency = "ollama"` → `emergency = ""` (keep key for OSS fork)
- L39-41: delete `[llm.ollama]` block

`tests/test_llm.py`:
- delete tests at L64 / L117 / L132
- trim fixture L12-14 (drop `"emergency": "ollama"` + `"ollama": {...}` lines)
- edit `test_multi_tier_all_fail_last_alert_critical_exhausted` (L98) if it monkeypatches `_MUTE_OLLAMA` — remove monkeypatch line, keep rest

Doc trims:
- `DESIGN.md:80` drop ` → local Ollama (emergency)`
- `DESIGN.md:85` drop ` or local Ollama`
- `DECISIONS.md:10` drop ` / Ollama emergency`
- `marrow/CLAUDE.md` line 16 ollama caveat — KEEP (Lumi may rotate providers in OSS forks)

Verify: `pytest tests/test_llm.py -k "ollama"` returns 0 tests. `grep -rn "ollama" marrow/ tests/ DESIGN.md DECISIONS.md` returns only empty-config-key line + historical PROGRESS entries.

## Step 7 — Full pytest green

- `pytest -q`
- Expected: ~298-302 passing (301 baseline − 3 ollama tests + new test_daily / test_storage / test_daily_catchup tests)
- Fix every red before step 8

## Step 8 — Plist edits

`deploy/mw-diary-routine.plist`:
- L13 ProgramArguments: `python -m marrow.diary` → `python -m marrow.daily`
- L27 `<integer>4</integer>` (Hour) → `<integer>7</integer>`

`deploy/mw-diary-catchup.plist`:
- L13 ProgramArguments: → `python -m marrow.daily --catchup` (note: split args properly in plist `<array>`)
- L27 `<integer>16</integer>` → `<integer>19</integer>`

`deploy/mw-jsonl-cleanup.plist`:
- Sunday Hour entry `<integer>5</integer>` → `<integer>12</integer>`

Consider rename of plist files themselves (`mw-diary-*` → `mw-daily-*`) — only if it does not break existing `launchctl list` labels in user's LaunchAgents directory. Default: keep plist filenames (label-stable), update only contents.

## Step 9 — launchctl reload

For each of the 3 plists in `~/Library/LaunchAgents/`:
- `launchctl unload ~/Library/LaunchAgents/<name>.plist`
- `launchctl load ~/Library/LaunchAgents/<name>.plist`

Verify: `launchctl list | grep mw-` shows all 3 loaded.

## Step 10 — Commit + PROGRESS + push

- `git add -A`
- Commit message:
  ```
  feat(2.5c): daily rewrite + sessionend 7-seg + threads→tasks + ollama strip + 6AM + plist realign

  - daily.py (read-only diary writer) + daily_catchup.py replace 900-LoC diary.py
  - sessionend_async.py: 7 sonnet segments (AFFECT/ENTITY_CAND/THREAD_CAND/MILESTONE_CAND/MEMES_CAND/DIGEST/NARRATIVE) with per-seg parse + DB write + idempotent audit
  - storage._migrate_to_v2: affect +3 cols (unresolved/reconcile_ref/resolved_at) + threads→tasks rename + session_digests table
  - 6AM day boundary, importance 1-5 clamp, ollama tier removed
  - 3 plists Hour 4/16/Sun5 → 7/19/Sun12 + path → marrow.daily
  ```
- `PROGRESS.md` append one line: `[2026-05-24] phase 2.5c daily rewrite done | <commit-sha> | verify: pytest <N>/<N>, sessionend_async live ok against sid <X>, launchctl list mw-* loaded`
- `git push origin main`

## Reporting checklist (during execution)

- After step 1: report `marrow/storage.py:<line>` migration block + `pytest tests/test_storage.py` result
- After EACH of step 2's 7 segments: paste prompt body verbatim + `marrow/sessionend_async.py:L<start>-L<end>`
- After step 2c live test: paste audit row + sample new DB rows
- After step 3: report DIARY_PROMPT paste source confirmation + `marrow/daily.py:L<line>`
- After step 5: report grep output + files touched
- After step 7: report pytest count
- After step 8: report 3 plist diffs (`git diff deploy/`)
- After step 9: report `launchctl list | grep mw-` output
- After step 10: commit sha + PROGRESS delta line
