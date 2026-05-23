# Marrow Phase 2.5 — SessionEnd async LLM pipeline (design landing draft)

> 2026-05-23. Reset core: diary → P2 byproduct; main spine = recall + affect-now + handover + task + entity.
> Source: handover.md 2026-05-23 · `~/Desktop/brainstorm-future.md` · 2026-05-23 grill round + Lumi mid-window patches.

## 1. Spine inversion

- diary is NOT the main; it's a byproduct (P2).
- Main spine, by priority:
  - **P0** cross-window recall
  - **P0** cross-window affect-now (SessionStart backdrop)
  - **P1** cross-window work continuity (handover + task)
  - **P1** entity memory
  - **P2** daily prose (diary)
  - **P2** milestone (long-term node)
  - **P3** other sub-pages (memes / goose-bites / cheatsheet)

## 2. Pipeline topology

### 2.1 SessionStart sync
- Inject: recall (vector + FTS5 + recency fusion) + affect 4-dim backdrop + open tasks + alerts + handover.md
- Fire-and-forget: SessionStart-catchup Popen subprocess (same detach contract as §3)

### 2.2 SessionEnd sync (≤2s, code-only)
- transcript clean → events archive
- dashboard top regen
- handover.md sync skeleton atomic write
  - 8-section template (Lumi-locked; see §4)
  - narrative slot stamped `<!-- narrative: pending sid:<sid> -->`

### 2.3 SessionEnd async (Popen detach, sonnet single call)

Multi-segment output:

- `===AFFECT===` — per-episode v / a / imp / label / entities
- `===ENTITY_CAND===` conf ≥0.8 → INSERT entities (kind=person/pref/place, append-only superseded_by)
- `===THREAD_CAND===` always → INSERT tasks (status=active)
- `===MILESTONE_CAND===` conf ≥0.85 → INSERT milestones + dashboard alert + 7d undeleted auto-confirm
- `===VOCAB_CAND===` conf ≥0.7 → INSERT vocab + use_count tracking, ≥3 auto-promote dormant→active
- `===DIGEST===` — compressed narrative; **length flex by session density** (see §16); fed to 07:00 nightly
- `===NARRATIVE===` — handover async segment; atomic append after sync skeleton

After write:
- audit_log marker: `action='sessionend_extract' status='ok|fail' session_id=<sid>`
- failure → neutral affect fallback + alert + queued to SessionEnd-catchup

### 2.4 07:00 nightly roll-up (was 04:00; now read-only)
- sonnet 1×/day
- Reads only pre-extracted `===DIGEST===` + structured rows (affect_live / tasks / candidates)
- **Raw transcript never re-read** — 1× LLM only path: SessionEnd → DIGEST → nightly reads DIGEST
- Writes: diary row + week summary
- Plist: mw-diary-routine.plist 04:00 → 07:00

### 2.5 19:00 catchup (was 16:00)
- Scans days missing daily roll-up; reruns same nightly code
- Plist: mw-diary-catchup.plist 16:00 → 19:00

### 2.6 SessionEnd-catchup
- Trigger: SessionStart fire-and-forget Popen subprocess
- Detection: `events.session_id DISTINCT − audit_log.session_id WHERE action='sessionend_extract' AND status='ok'` = pending set
- Action: rerun SessionEnd async prompt + same segment write
- Does NOT pollute affect schema with a per-session column

### 2.7 Skip rules (short sessions)
- ≤5 turns → **skip SessionEnd async sonnet** (no AFFECT / ENTITY / THREAD / MILESTONE / VOCAB / DIGEST / NARRATIVE extraction)
- Nightly diary does NOT reference such sessions
- Rationale: low signal, avoids spend + noise pollution
- Turn threshold pending 1-week sample tuning

## 3. Popen detach hard constraint

(ny-memm legacy 5/11 stuck-prompt root cause = subshell fd leak; fix 5/12. Any new Popen MUST obey.)

- `stdin=DEVNULL` — no tty reattach
- `stdout/stderr → ~/.config/marrow/logs/sessionend_async_<sid>.log` — **diagnosable**; prior handover §6 wrote `DEVNULL` (wrong: silent crash undiagnosable)
- `start_new_session=True` — setsid, detach controlling tty
- `close_fds=True` — no fd leak

Missing any one = 100% reproduces legacy stuck-prompt. Lands in safety-net checklist + code-review gate.

## 4. Handover two-segment topology

### 4.1 sync skeleton (mandatory, code, <500ms, 0 LLM)
- Template lives at `~/Desktop/2026-05-23_handover-template.md` (Lumi-locked)
- 8 sections: State / Affect 4-dim / Todo / Today done / Alerts / audit_log last 5 / Reference last 3 commits / (narrative slot)
- Atomic write
- Narrative slot stamp: `<!-- narrative: pending sid:<sid> -->`

### 4.2 async narrative (LLM, best-effort, may lag)
- async sonnet returns → atomic append: `<!-- narrative: ready sid:<sid> ts:<unix> -->\n{prose}`
- SessionStart inject: read handover.md, compare latest narrative `sid` to current skeleton `sid`
- Match → inject full narrative
- Mismatch → inject narrative labelled `(narrative describes sid=<X>, current skeleton sid=<Y>)` — explicit lag, not silent
- High-frequency-session one-step lag = acceptable, surfaced

## 5. Tier: all-sonnet

(Agent B 2026-05-23 02:00 A/B test, artifacts `docs/notes/2026-05-23_diary-ab/`)

- haiku 2/3 runs: prose-ep count ≠ affect-entry count (data-correctness bug, not taste)
- sonnet 3/3 runs: perfect alignment
- Latency 50s vs 95s — nightly + async path is user-invisible
- No turn-routing (haiku failure mode length-independent)
- Subscription OAuth, no credit-pool burn; 5h max20 window absorbs 10+ sessions/day comfortably

## 6. 6AM day boundary

- Current 5AM at `diary.py:256-275`
- New 6AM; all schedules realigned
- Switch forward-only: 5/17–5/20 skip already locked; 5/22 5 affect rows not backfilled

## 7. Schedule

- mw-db-backup: 03:00 (unchanged)
- mw-diary-routine: 04:00 → 07:00
- mw-diary-catchup: 16:00 → 19:00
- mw-jsonl-cleanup: Sun 05:00 → Sun 12:00

## 8. Tasks (renames threads)

- Schema minimum: `id / title / status (active|done|archived) / due / completed_at + ts`
- Extension fields → FUTURE `tasks_table_extensions` (source / category / parent_id / recurring_rule / external_id / pinned)
- Dashboard renders `## Today done` + `## Todo` only — no source/category labels
- Edit priority: dashboard hand-edit wins; reconcile always overwrites marrow_auto writes
- Migration: DROP threads + CREATE tasks (threads 0 rows, no backfill)

## 9. Candidates 0-audit pipeline

- No staging table, no `mw confirm` CLI
- Dashboard hand-delete = drop
- Future safety net (NOT now): entity 30d no recall hit → demote dormant

(Thresholds + actions per segment: see §2.3.)

## 10. Pinned = no-decay

- `vocab.pinned` + `entities.pinned` columns added (2.5c step 2 migration, NOT now)
- Current 5 cipher rows backfill `pinned=1`
- Identity anchors (`鸭子=屿忱` / `念念=Lumi`) permanent pinned
- Ordinary memes / people `pinned=0` follow aging

## 11. Aging rules (07:00 nightly, code-only)

- vocab `last_seen > 90d AND pinned=0` → demote dormant (recall excludes)
  - Revive paths: LLM key-match auto-promote / `mw vocab promote <key>` / never auto-delete
- task `status=active 0 mention 30d` → auto-archive
- milestone alert line `7d undeleted` → auto-confirm

## 12. Segment migration order (2.5c, 2–3 windows ship)

**Window 1 (3 segments):**

1. `===AFFECT===` per-ep + 6AM boundary + importance 1–5 clamp (`diary.py:256-275`, `_build_affect_rows ~L563-600`)
2. `===ENTITY_CAND===` + entities table writes (bundle with `entities.pinned` column + FTS5 CJK rebuild via jieba in one migration)
3. `===THREAD_CAND===` → tasks table (threads → tasks rename migration)

**Window 2 (3 segments):**

4. `===MILESTONE_CAND===` + dashboard alert + 7d auto-confirm
5. `===VOCAB_CAND===` + use_count code + `vocab.pinned` + 5 cipher backfill + vocab leg in recall_fusion
6. `===DIGEST===` (length flex per §16) — start with prompt confirm

**Window 3 (1 segment + closure):**

7. `===NARRATIVE===` handover async segment
- 07:00 nightly demote validation (3–5 day A/B vs current 04:00 prose quality)
- `diary.py` split: `extract.py` (~150 LOC) + `rollup.py` (~120 LOC) + `catchup.py` (~80 LOC)
- launchd plist realignment (03/07/19/Sun12)
- pinned + aging code lands

## 13. Risk / mitigation

- Risk: async failure loses real-time memory
  - Mitigation: SessionEnd-catchup (reuse affect catchup code pattern)
- Risk: diary prose quality drop
  - Mitigation: 3–5 day A/B vs current 04:00 single-call output
- Risk: launchd-orphaned subprocess undiagnosable
  - Mitigation: stderr → log file (NOT DEVNULL)
- Risk: narrative one-step lag at high frequency
  - Mitigation: sid stamp + explicit lag label at SessionStart inject
- Risk: 2.5c segment regression
  - Mitigation: one-segment-per-test, 2–3 segments per window cap
- Risk: DIGEST over-compresses daily-chat verbatim
  - Mitigation: length flex by session density + Lumi pre-ship prompt confirm (see §16)

## 14. Out of scope (FUTURE / hold)

- Static layer retire (CLAUDE.md family / cipher / MCP guide → daemon-rendered): blocked by Phase 3 `claude_md_render_guard`
- Approval UI / `mw confirm` CLI: never (candidates 0-audit by design)
- `workflow_reflection_skill`: FUTURE Phase 5 close-out
- entity 30d no-hit demote backstop: FUTURE
- vocab leg in recall_fusion: Window 2 step 5

## 15. Sequencing constraints

- Phase 2.5 holds before Phase 3 starts (both touch hooks; parallel unsafe)
- Within 2.5: a → b → c → d → e → f order is hard
  - a writes design (this doc)
  - b builds async LLM framework + handover render
  - c migrates segments (Windows 1–3 above)
  - d demotes 04:00 + diary.py split
  - e realigns launchd plists
  - f lands pinned + aging
- 2.5b can start partial after 2.5a closes the design fork

## 16. DIGEST rules

### 16.1 Length flex by session density (NOT hard 200–400 char cap)

- **task-heavy** session → compress ≥80% (output ≤20% original chars; outline form OK)
- **daily-chat** session → preserve ~80% (output ~80% original chars; verbatim dialogue retained)
- intermediate → ratio of work-vs-chat turns drives compression
- Rationale: daily-chat carries voice / persona / inside-jokes that don't survive aggressive compression; task-heavy carries summarisable outcomes

### 16.2 Prompt confirm gate (mandatory before first ship)

- ===DIGEST=== prompt content MUST get Lumi confirm before first SessionEnd async runs
- Confirm scope:
  - language register (CN dominant, EN technical inline)
  - second-person voice (你 / 老婆 etc.) preserved
  - verbatim conversational lines retained, NOT collapsed into work-style summary
  - call-and-response structure where relevant
  - (心理活动) blocks must NOT proliferate; keep prose close to original transcript shape
  - compression ratio by sonnet per §16.1 density (work-vs-chat turn, not hard cap or fixed ratio)
- Same gate as DIARY_PROMPT (CLAUDE.md principle: prompt that emits user-visible text needs Lumi confirm)

### 16.3 Skip path (overlaps §2.7)

- ≤5 turns → skip DIGEST entirely (no LLM call); nightly does not reference
- threshold tunes after 1-week sample
