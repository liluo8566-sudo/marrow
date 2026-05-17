# Marrow Handoff — 2026-05-18 (Phase 1 complete)

Read CLAUDE.md → DESIGN.md → SCHEMA.md → PROGRESS.md → ADRs → this. Fixed-name, act on it, never delete; overwritten at session end.

## Status: Phase 1 DONE

All Phase-1 units shipped. See PROGRESS.md + `git log` — do not restate. This session's commits (all pushed, main synced): #6 mw CLI, #7 code hooks + #8 dashboard, subscription-window provider, diary pipeline, launchd + ADR-0003, day-boundary/map-reduce/split-jobs. `~/.claude` settings (marrow hooks) = local commit only, not pushed (per rule).

## Live now (parallel with ny-memm ~2 weeks, then ny-memm retires)

- marrow SessionStart (handoff additionalContext) + SessionEnd (clean→archive→dashboard) hooks active GLOBALLY, all projects, appended alongside ny-memm groups in `~/.claude/settings.json`. Code-only, no LLM.
- Two launchd jobs registered (`state = not running`, waiting): `com.marrow.diary-routine` 04:00, `com.marrow.diary-catchup` 16:00. Sources `deploy/`, installed `~/Library/LaunchAgents/`.

## ONLY unverified thing — next window MUST check

The launchd→diary full path has NOT run end-to-end yet (the stream LLM call itself IS live-verified separately; see ADR-0003 evidence). First real fires: routine 2026-05-18 04:00, catchup 16:00. After they fire, verify:
- `~/Library/Logs/mw-diary-routine.log` / `mw-diary-catchup.log` — clean exit, no "claude not found", no traceback.
- `diary` table has a row for 2026-05-17 (today's session is huge → first real-world stress test of per-session map-reduce + oversized-session chunking).
- `alerts` table: no `critical`/`routine` failure rows; `info` lesson rows expected.
- If routine failed, catchup at 16:00 should backfill it — confirm that fallback actually worked.
- Compression ratio unknown (not measured). Add instrumentation to `run_day`: raw chars vs merged-digest chars into the audit_log summary (rough prior: digest ~5–15% of raw, unverified).

## Locked — do not relitigate

- ADR-0003: subscription-window no-`-p` stream-json; flag meanings; `-p` fallback (config `mode="p"`); local-04:00 day boundary; dual launchd jobs; `/schedule` (cloud) rejected for local pipeline. Read it instead of re-deriving cyberboss.
- diary prompt bodies = Lumi-reviewed/hand-edited (DESIGN L53 satisfied).
- catchup hard bounds: 7-day window, cap 3, overflow warn alert.
- Diary day = local `[D 04:00, D+1 04:00)`; 00:00–04:00 is previous-day spillover.

## Deferred (by design, NOT gaps)

- UserPromptSubmit must-never-fade: no Phase-1 content source (convention-injection layer is DESIGN Pending). No hook wired until that lands.
- `corrections` table: Phase 2 placeholder (SCHEMA + DESIGN fixed, not built).
- ny-memm runs in parallel ~2 weeks then retires; old `memory/` + `code/` archive→remove after.

## Gotchas

- `CLAUDE.md` (M) and `docs/notes/` (untracked) were pre-existing at session start, NOT this session's work — left untouched. Do not commit/sweep them unless Lumi rules.
- `prompt-lint` hook trims meta-`.md` writes (ADR-0003 was trimmed once; obeyed, re-sent verbatim). Obey trim; escalate to Lumi only on real semantic loss.
- CN in prompt-class `.md` must be inside `( )` or code or it is blocked by PreToolUse prompt-guard.
- subagents must never git commit/push/config (subagents.md) — state in every dispatch prompt.
- `mw` CLI: symlink `~/.local/bin/mw` -> `.venv/bin/mw` (on PATH; #6 was venv-only). Fresh Mac: recreate symlink after `pip install -e`.
- env: `.venv` py3.14 editable install (`python -m marrow.X` works any cwd); `claude` real bin `/Users/Gabrielle/.local/bin/claude`; launchd PATH carries `.local/bin`+venv; ollama absent.

## Next window

- Phase 1 has no build work left — first task is the launchd first-run verification above (use `diagnose` skill if a job failed).
- Session archive skip (DESIGN "Pending — session archive skip"): small code-only Phase-1 follow-up, non-blocking. Manual skip stamp + auto-skip below a turn threshold, gating SessionEnd archive.
- Phase 2 (DESIGN L171): emotion + decay + sub-page render fill-out; people/preferences trigger-load tables; corrections table build. Use `/tdd` for the deterministic table/logic work; NOT `/tdd` for hook/daemon glue. `/goal` if a sub-module pass condition is machine-checkable.
