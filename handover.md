# Marrow handover — 2026-05-24 01:35
> Fixed-name overwrite, never delete. Keep points not touched this window.

## State
- pytest 291/291 + 1 manual-skip (5.88s)
- branch worktree `worktree-agent-a60306eb1281ec48e`, **uncommitted** — 23 files vs main, never merged
- channel cc / opus-4.7 (1M)
- 7-segment sessionend pipeline + AFFECT/DIGEST/HANDOVER prompts locked

## This window — phase 2.5c segment ship + prompt restoration + narrative→handover rename

### Done (worktree, NOT merged)

- schema v2: `affect` +unresolved/reconcile_ref/resolved_at/reconcile_prev_text; `session_digests` new; threads RENAME→tasks via `_pre_v2_rename` pre-`_TABLES`
- `sessionend_async.py` 7 segments (AFFECT/ENTITY_CAND/TASK_CAND/MILESTONE_CAND/VOCAB_CAND/DIGEST/HANDOVER), each independent
- `sessionend_prompts.py` (275 LoC): all 7 prompts; `===SESSION===` marker every prompt; persona rule on narrative segments
- AFFECT: Lumi Unresolved + reconcile_prev verbatim from `docs/notes/lumi-prompt-source.md`; importance 1-5 anchor + 9 main-tones byte-verbatim from old `diary.py:266-280`
- DIGEST: byte-verbatim restore of old `diary.py:87-116` + 2 Lumi tuning bullets (保留承载情绪的原句 / 只留 subject+did+outcome 丢过程细节)
- VOCAB_CAND: DESIGN line 47 spec (inside-jokes + viral quotes + topical news/event mentions); type meme/cipher/nickname/phrase/quote/news
- MILESTONE_CAND description: 2-3 sentences (50-100w)
- **NARRATIVE → HANDOVER rename** (full chain): `HANDOVER_PROMPT` / `_seg_handover` / `_parse_handover_blocks` / `_SEGMENTS` / stamp `<!-- handover: pending sid -->`. Produces `## This Session` + `## Next Session` bullet sections, markers `===THIS_SESSION===` / `===NEXT_SESSION===` / `===END===`
- `handover_render.py`: `_last_3_commits` (git log -3 marrow repo, 2s, fail-soft) + `_inject_reference_commits` fills `## Reference (last 3 commits)` at skeleton (code, not LLM)
- new `marrow/daily.py` (~234 LoC): `main(--catchup)`, DIARY_PROMPT byte-verbatim from old `diary.py:137-194`, reads session_digests + affect_live, atomic txn
- new `marrow/daily_catchup.py` (~95 LoC): pending_days / day_events / has_diary / app_lock fcntl, `_CUTOFF_H=6`
- deleted `marrow/diary.py` (900 LoC); renamed test_diary→test_daily; added test_daily_catchup
- `top_sections.py:render_tasks`: `next_step` rendered as `: <detail>` across Today/Next 7 Days/Later; Later bucket split due / no-due with `---` separator
- P8 ollama strip: `_MUTE_OLLAMA` / chain filter / `_run_ollama` removed from llm.py; `[llm.ollama]` dropped from config; 4 ollama tests deleted; DESIGN/DECISIONS cleaned; marrow/CLAUDE.md ollama-caveat KEPT
- plist edits + renames (NOT yet launchctl loaded; LaunchAgents empty): `mw-diary-routine.plist`→`mw-daily-routine.plist` 4→7h + `python -m marrow.daily` + Label `com.marrow.daily-routine`; `mw-diary-catchup.plist`→`mw-daily-catchup.plist` 16→19h + `--catchup` + Label `com.marrow.daily-catchup`; `mw-jsonl-cleanup.plist` Sun 5→12h (retire next window)
- DECISIONS overwritten in place (4 lines): candidate split / vocab aging FTS5 reverse-scan / handover skeleton Reference=code / handover-segment stamp rename
- FUTURE +3: dashboard_v2_redo / milestone_format_unify / subpage_redo
- 7 new HANDOVER-segment tests + 3 reference-commits tests

## Next window — Lumi clarifications 2026-05-24

`~/Library/LaunchAgents/com.marrow.*` is empty — no `launchctl unload` needed.

1. **jsonl cleanup retire** — Lumi-confirmed: cc's `cleanupPeriodDays` handles jsonl. (a) add `"cleanupPeriodDays": 30` to `~/.claude/settings.json`; (b) delete `marrow/cleanup.py` + `tests/test_cleanup.py`; (c) delete `deploy/mw-jsonl-cleanup.plist`.

2. **aging job (consolidated weekly)** — new `marrow/aging.py`: vocab FTS5-reverse-scan (7d events, ≥3 hits → bump use_count + last_seen=now; hits=0 AND last_seen > 90d AND pinned=0 → demote); task status=active 30d no mention → auto-archive; milestone alert 7d undeleted → auto-confirm. new `deploy/mw-aging.plist` Sun 12:00 (DECISIONS:45: nightly → weekly).

3. **vocab pinned LLM field** — VOCAB_CAND adds `pinned: 0/1` output. pinned=1 for private anchors (鸭子 / 念念 / 老公 / cipher / Lumi / Stellan-internal); pinned=0 for public/viral/topical. Code hardcodes anchor list to force pinned=1.

4. **sessionend 7-call → 2-call refactor** — one sonnet call emits all 4 marker blocks (===AFFECT===/===TASK_CAND===/===DIGEST===/===HANDOVER===); JSON parse per-block (one fail doesn't block others). ~75% token cost reduction.

5. **candidate split sessionend → daily** — ENTITY_CAND / MILESTONE_CAND / VOCAB_CAND move to daily.py; one sonnet call emits 3 marker blocks on aggregated session_digests. TASK_CAND stays in sessionend. Strip 3 from `test_sessionend_async`, add 3 to `test_daily`.

### Open question
- handover-name collision: `~/.config/marrow/handover.md` (SessionStart inject) vs `~/cc-lab/marrow/handover.md` (dev brief). Rename one if needed.

### Open question (NOT a blocker)
- handover-name collision: `~/.config/marrow/handover.md` (runtime SessionStart inject target) vs `~/cc-lab/marrow/handover.md` (this file, dev brief). Two systems share filename. Rename one if it bites. Currently unambiguous by path.

## Pending — retained

### Lumi self-writes
- `~/.claude/CLAUDE.md` Affect quick-reference legend (P2). Lumi-owned.

### Phase 3 backlog (blocked by 2.5 close)
- writer_authority · drift_sweep · convention_injection · claude_md_render_guard
- static-layer retire (CLAUDE.md family / cipher / MCP guide → daemon-rendered); prereq = claude_md_render_guard

### Carryover scratch
- `~/Desktop/brainstorm-future.md` — 10-section future features (3 items Phase 5; 9 pending)
- 9 old worktree branches dangling

## Reference (no commits this window — all worktree-local)
- 81fc532 docs(handover,plan,prompt-source): 2.5c plan rebuild + Lumi Unresolved spec (parent main)
- 1e4d308 feat(dashboard): drop hand-edit backup+alert; render overwrites silently
- b8cf11c feat(subpages): drop hand-edit backup+alert; DB is SoT, render overwrites silently
