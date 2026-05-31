# Marrow — MAP

> This doc produced by agents based on code. It's not SoT. If unsure, go back to code.

## 0. Contents

- §1  System map — 1.1 data-flow diagram · 1.2 hooks registry
- §2  Write path — 2.1 session capture · 2.2 sessionend extraction (STATE/NARRATIVE) · 2.3 daily candidate
- §3  Read path — injection back to CC (hooks + daemon)
- §4  Storage & retrieval — 4.1 schema · 4.2 embedding · 4.3 recall fusion
- §5  Surface — 5.1 dashboard · 5.2 subpage catalog (11) · 5.3 sync machinery · 5.4 write arbitration
- §6  Handover subsystem
- §7  Scheduled jobs (launchd, 7 plists)
- §8  Alerts
- §9  Catchup & self-heal
- §10 Cleanup / aging
- §11 Infra — 11.1 daemon/MCP · 11.2 LLM provider · 11.3 config/paths · 11.4 popen/backup/migrate
- §12 Addons — 12.1 daily/day-plan · 12.2 buddy/goose-bites
- §13 Invariants & current status

> Each sub-section is self-contained — grep `## N` / `### N.M` anchor + Read offset+limit for partial load.

## 1. System map

### 1.1 Data-flow diagram + 3 runtimes

```
   ┌─────────────────────────────────────────────────────────────┐
   │                      CC session                             │
   │                                                             │
   │     transcript.jsonl                  injected context      │
   │           │                                  ▲              │
   │           │ SessionEnd                       │              │
   └───────────┼──────────────────────────────────┼──────────────┘
               │                                  │
               ▼                       ┌──────────┴──────────────┐
      ┌──────────────────┐             │   · hooks (auto)        │
      │ events           │             │   · daemon (MCP, on-    │
      └────────┬─────────┘             │     demand)             │
               │ popen_detach          └──────────┬──────────────┘
               ▼                                  │
      ┌─────────────────────────┐                 │
      │ sessionend_async        │                 │
      │ STATE | NARRATIVE       │                 │
      └──────────┬──────────────┘                 │
                 │                                │
   daily.py 7AM ─┤                                │
                 │                                │
                 ▼                                │
   ╔═══════════════════════════════════════════════════════════════╗
   ║              DB (SQLite) — Memory core                        ║
   ║                                                               ║
   ║  events · tasks · affect · entities · milestones · memes      ║
   ║  diary · digests · alerts · audit_log · pit · stickers        ║
   ║  atlas · goose_bites · md_index · memes_reject_log            ║
   ║                                                               ║
   ║  bge-m3 + 6 vec lanes + recall fusion                         ║
   ╚═══════════════════╤═══════════════════════════════════════════╝
                       │ watcher + sync_loop (5s)│ 
                       ▼                         ▲ user edits md
   ╔═══════════════════════════════════════════════════════════════╗
   ║                       Surface (md)                            ║
   ║                                                               ║
   ║   dashboard.md     Main Entrance                              ║
   ║   db-pages/        11 subpage (see §5.2)                      ║
   ║   handover.md      + symlink × 3 root                         ║
   ╚═══════════════════════════════════════════════════════════════╝
   ╔═══════════════════════════════════════════════════════════════╗
   ║   Addon contract                                              ║
   ║                                                               ║
   ║   planned  :  WeChat bridge (Phase 4)                         ║
   ║               stellan_wallet (Phase 5)                        ║
   ║               external MCPs (.mcp.json)                       ║
   ╚═══════════════════════════════════════════════════════════════╝
```

- **daemon (MCP)** — stdio MCP server exposing recall / atlas_lookup / embed_pending; launched on-demand by CC via .mcp.json, persistent for the session lifetime.
- **watcher (launchd)** — long-running file-system watcher; detects md edits, updates md_index, triggers reconcile + render; supervised by launchd with KeepAlive=true.
- **hooks (one-shot)** — CC lifecycle callbacks (SessionStart / SessionEnd / UserPromptSubmit / PreToolUse); spawn as a child process per event and exit immediately after injecting context or firing popen_detach.

### 1.2 Hooks registry

- SessionStart: every session open · injects open tasks + alerts + affect backdrop as additionalContext; spawns sessionstart_catchup detached · marrow/hooks.py:205
- SessionEnd: every session close · archives transcript events, writes lifecycle:end, idempotent spawn gate compares current user_count vs last ok row to skip popen when no new events, then spawns sessionend_async detached · marrow/hooks.py:272 · marrow/hooks.py:306
- UserPromptSubmit: every user turn · handles mm-/mm+ control prefixes + injects recall-fusion cards as additionalContext · marrow/hooks.py:482
- PreToolUse (Write/Edit/Bash): every file-write or file-op · emits atlas placement guidance for the target path's ancestor chain; global hooks also apply prompt-guard (CJK/table lint) and prompt-lint (haiku trim) · marrow/hooks.py:541 · ~/.claude/hooks/prompt-guard.py · ~/.claude/hooks/prompt-lint.py

---

## 2. Write path

### 2.1 Session capture

### session-capture
- SessionEnd hook reads transcript path from stdin → `transcript.clean()` (code-only, no LLM) strips tool calls, thinking blocks, sidechain spawns, and buddy (铁锅) end-of-turn HTML comments at marrow/transcript.py:40 — keeps only user/assistant text with source_hash dedup → `repo.archive_events()` idempotent insert.
- Headless spawns dropped via known-prompt-head matching.
- **Where**: marrow/hooks.py:272 · marrow/transcript.py:1 · marrow/storage.py:22

### 2.2 Sessionend extraction (STATE / NARRATIVE)

### sessionend-extraction
- Detached `sessionend_async` (spawned after lifecycle:end commits). Two sequential Sonnet calls on the archived transcript:
  - **STATE** call → writers: `handover` · `task_cand`
  - **NARRATIVE** call → writers: `affect` · `digest`
- Both calls open with the byte-identical `_TRANSCRIPT_BLOCK` fence → NARRATIVE's prompt-cache read hits STATE's prefix. Serial in one process, no async runtime.
- **Failure isolation**: STATE failure does not block NARRATIVE. `partial:<failed_writers>` logged when 1–3 of 4 writers fail; `fail:all` only when all four fail.
- **Stale-skip recovery**: if cc fires session_end mid-flush, skip:short_session is cleared once event count grows past threshold so the rerun completes (marrow/sessionend_async.py:128).
- **Task-extraction gate**: sessions with ≤ 3 user turns skip the task writer entirely (sessionend_async.py:353); affect / handover / digest still run. Tasks have no count-based ingestion gate beyond this — new titles land directly subject to cosine dedup (0.85) against active tasks + done tasks in last 24h.
- **Where**: marrow/sessionend_async.py:423 · marrow/sessionend_prompts.py:24 · marrow/sessionend_writers.py:63 · marrow/hooks.py:320

### 2.3 Daily aggregation (candidate extraction)

### daily-aggregation
- **What**: Nightly Sonnet call per day → entity / milestone / memes candidates, one writer per block (`===BLOCK===` / `===END===` markers). Block parse failure isolates to that block; the other two still land.
- **Per-table ingestion gates** (where new candidates can actually land):
  - **entities**: LLM confidence ≥ 0.8 → land directly. Cosine 0.85 hit against same-kind active entity → **merge** new name+aliases into the matched row (auto-learn, never blocks insert). · marrow/candidates.py:148 · marrow/candidates.py:186
  - **milestones**: LLM confidence ≥ 0.85 → land directly. Cosine 0.85 against existing titles blocks. Force rule: any affect episode with `importance=5` must emit a milestone. · marrow/candidates.py:219 · marrow/daily_prompts.py:37
  - **memes** (types paw / meme / news / event — NOT fact / others): frequency gate = **key substring must appear on ≥ 3 distinct calendar days in last 7d of events** (counted by `DATE(timestamp)` distinctions in `_events_like_count_7d`, not row count). Cosine 0.85 vs existing memes blocks. paw / fact auto-pinned (never age). · marrow/candidates.py:275 · marrow/candidates.py:427
- **Shared dedup config** (all four candidate tables, config.toml `[*_dedup]`):
  - `cosine_threshold = 0.85` · `fast_skip_count = 3` (3+ persistent rejects on the same key short-circuits future LLM dedup checks) · marrow/config.default.toml:83
- **Where**: marrow/daily.py:150 · marrow/daily_prompts.py:28 · marrow/candidates.py:38 · marrow/semantic_dedup.py

---

## 3. Read path (what gets injected, when)

- **SessionStart injects**: affect heartbeat warning (gap day in last 7d with events but no affect), open tasks + open alerts from repo.handoff(), affect backdrop (top_sections mood band), Note reminder pointing to ## Lumi's Note.
- **UserPromptSubmit injects**: top-K recall fusion hits (vec + bm25 + recency + affect) as additionalContext labelled "Recall (auto) — passive context, do not answer"; also handles mm-/mm+ control prefixes.
- **entity force-include**: Bypasses FTS5 via reverse-substring match (name.lower() in query.lower()) so 2-char CN names that fall below the trigram tokenizer's 3-char floor (e.g. 南南) are still surfaced; for names ≥3 chars FTS5 is tried first, LIKE-scan as fallback.
- **Where**: marrow/hooks.py:205 · marrow/hooks.py:482 · marrow/entity_recall.py:73 · marrow/recall.py:1027

---

## 4. Storage & retrieval

### 4.1 Schema + table catalog

Schema version: 13. Migrations: idempotent numbered functions (_migrate_to_v2…v13) run in sequence on every init_db call, guarded by PRAGMA user_version; column ALTERs swallow duplicate-column errors silently.

- events: per-turn transcript rows (role/content/session_id/channel) · primary recall surface · no aging · marrow/storage.py:21
- tasks: active work items (study + project) · status = active/done/archived · active with 0 mentions in 30d → archived · marrow/storage.py:32
- milestones: identity/life anchors (scope/date/title) · pinned=1 exempts from aging · no expiry · marrow/storage.py:45
- memes: concept glossary / lore entries · status = active/dormant · pinned=0 AND last_seen > 90d → DELETE (no dormant intermediate step) · marrow/storage.py:57
- stickers: asset attachments linked to a meme row · no aging · marrow/storage.py:70
- pit: project idea backlog · status = idea/active/done · no aging · marrow/storage.py:79
- diary: one row per date (TEXT PK) · no aging; daily.py rewrites by DELETE+INSERT · marrow/storage.py:89
- goose_bites: per-session distilled goose (铁锅) quotes · best=1 flags top picks · no aging · marrow/storage.py:97
- alerts: cross-system signal rows · severity = info/warn/critical · resolved by manual flag or aging · marrow/storage.py:106
- audit_log: action history (target_table/target_id/action/summary) · no aging · marrow/storage.py:116
- affect: per-episode emotion (V/A/importance/label) · superseded_by=NULL = live; affect_live view filters · marrow/storage.py:124
- entities: named people/places/concepts with fact + aliases · kinds = person/preference/place/event/concept · superseded_by=NULL = live · marrow/storage.py:140
- session_digests: one DIGEST text per sessionend run · keyed by session id · no aging · marrow/storage.py:382
- md_index: per-block content hash per subpage file (path, block_id) · tombstone_at non-null = user deleted · marrow/storage.py:525
- memes_reject_log: persistent dedup rejection counter (key, type, reason) · marrow/storage.py:554
- atlas: directory heading tree (path/description/naming_hint/depth) · stale rows deleted on sweep · marrow/storage.py:579
- *_vec / *_vec_meta: six sqlite-vec virtual tables (events, memes, entities, milestones, diary, tasks) + companion meta tracking embedder_id and dim · marrow/storage.py:151

### 4.2 Embedding & vec lanes

### embedding
- BAAI/bge-m3 via ONNX runtime → 1024d float vectors. Lazy-loaded singleton (model.onnx + tokenizer.json from HuggingFace snapshot). CLS-pool output L2-normalised.
- Six vec lanes: **events · memes · entities · milestones · diary · tasks**. `embed_pending()` iterates all six per call, capped batch each, so a huge events backlog can't starve cross-table lanes.
- **Where**: marrow/recall.py:59 · marrow/recall.py:399 · marrow/recall.py:132 · marrow/storage.py:224

### 4.3 Recall fusion (scoring)

### recall
- FTS5 (BM25-normalised) ∪ vec (cosine) candidates merge by event id → weighted sum: **vec 0.55 · bm25 0.30 · recency 0.15 · affect 0.10**.
- Milestones / memes / diary / tasks lanes contribute vec-only or substring candidates with reserved slot caps (anchor rows can't be starved by events flood).
- Entity force-include: any entity name found in the query prepends its entity-card + linked events, bypassing FTS5 (handles 2-char CJK names that fall below the trigram tokenizer's 3-char floor).
- **Thresholds**: `min_score=0.35` for events (milestones / memes / entities skip this gate) · vec-only floor `0.40` (cross-table) · dormant rule: importance ≤ 3 AND age > 90d excluded unless FTS keyword hit revives
- **Where**: marrow/recall.py:693 · marrow/recall.py:433 · marrow/recall.py:444 · marrow/recall.py:455 · marrow/entity_recall.py:73

---

## 5. Surface (DB ↔ md)

### 5.1 Dashboard render contract

### dashboard
- Top region written atomically: alert, tasks, milestone candidates, affect.
- Contents: Links to all subpages
- `write_dashboard` flow: reconcile absorbs md edits into DB) → `top_sections.iter_top_blocks` (render fresh block bodies from DB) → `_resolve_blocks` per-block decision against md_index hash:
  - hash == baseline → **overwrite** (auto-write replaces auto-write)
  - hash != baseline → **skip** (user hand-edited, preserve)
  - tombstoned → **omit** (user deleted, don't re-emit)
- **Where**: marrow/dashboard.py:142 · marrow/dashboard.py:96 · marrow/top_sections.py:448 · marrow/dashboard.py:31

### 5.2 Subpage catalog

Total pages: 11. Registry: subpages._REGISTRY (marrow/subpages.py:306). Config groups: [subpages] top/bottom/hidden in config.toml; defaults at marrow/subpages.py:339.

All subpages share: DB is SoT, reconcile runs before render, atomic write, `<!-- marrow:key:start/end -->` markers. Inserter (md-as-SoT block-level upsert via md_index) is the current standard; legacy full-render is the fallback.

#### profile
- **What**: Entity rows (person/pref/place) from entities_live, grouped by kind.
- **Status**: done (inserter wired) · empty until Phase 2 entity render populates rows
- **Where**: marrow/subpage_specs.py:58 · marrow/subpages.py:307 · Write: inserter · Direction: DB→md

#### milestone
- **What**: Pinned milestones (pinned=1) grouped by scope Us/Me, H5 format.
- **Status**: done (inserter + reconcile_milestones, bidirectional)
- **Where**: marrow/subpage_specs.py:98 · marrow/reconcile.py:162 · Write: inserter · Direction: bidirectional

#### diary
- **What**: Daily diary entries (date→content→mood), grouped by year then month.
- **Status**: done · block_id = date; no reconcile callback, DB is SoT
- **Where**: marrow/subpage_specs.py:141 · Write: inserter · Direction: DB→md

#### memes
- **What**: Meme rows grouped Personal (paw/fact) vs Public (meme/event/news/others).
- **Status**: done · Where: marrow/subpage_specs.py:188 · Write: inserter · Direction: DB→md

#### stickers
- **What**: Sticker gallery — one bullet per asset, grouped by linked meme key.
- **Status**: stub (inserter wired but empty — auto-describe ingest not shipped)
- **Where**: marrow/subpage_specs.py:224 · Write: inserter · Direction: DB→md

#### wallet
- **What**: Placeholder for bank-statement render (Phase 5 stellan_wallet).
- **Status**: stub (inserter wired, fetch always returns empty; transactions table not shipped)
- **Where**: marrow/subpage_specs.py:261 · Write: inserter · Direction: DB→md

#### goose-bites
- **What**: Best goose quote per day, grouped by year and month.
- **Status**: done · registry key = "goose" but filename overridden to goose-bites.md
- **Where**: marrow/subpage_specs.py:285 · marrow/subpages.py:424 · Write: inserter · Direction: DB→md

#### study (index + per-unit pages)
- Only index bothway; unit pages hand manage [NOT DONE]
- **What**: study.md index with Obsidian links per unit; child study/<unit>.md per task group.
- **Status**: wip — index inserter; child pages read_only legacy render, skipped by watcher
- **Where**: marrow/subpages.py:164 · Write: index=inserter / children=legacy read_only · Direction: DB→md

#### projects (index + pit + per-project pages)
> index & pit will need both way db-md [NOT DONE]
> other pages no need db? pure hand-edit/grep reading
- **What**: projects.md index, plus per-project detail pages under projects/.
- **Status**: wip — index inserter; child pages read_only legacy (Phase E will add file-per-project + frontmatter)
- **Where**: marrow/subpages.py:211 · Write: index=inserter / children=legacy read_only · Direction: DB→md

#### cheatsheet
- **What**: Hand-written reference sheet; disk is SoT.
- **Status**: stub (file empty — Lumi hand-writes when ready; no DB backing, no InserterSpec, legacy full-overwrite render)
- **Where**: marrow/subpages.py:329 · Write: legacy read_only · Direction: read_only (disk SoT)

#### atlas
- **What**: Directory heading tree for AUTHORIZED_ROOTS, with description and naming hints.
- **Status**: done (inserter + reconcile_atlas bidirectional + atlas_sweep_fs)
- **Notes**: respect_tombstones=False (depth changes must resurface tombstoned paths); ATLAS_ROOT_ORDER decoupled from AUTHORIZED_ROOTS order.
- **Where**: marrow/subpage_specs.py:406 · marrow/atlas.py:358 · marrow/atlas.py:442 · Write: inserter · Direction: bidirectional

### 5.3 Sync machinery

### md_index
- Per-block content hash per (path, block_id) across all watched md files. Baseline hash = "last auto-write" — divergence = user edit.
- `sync_file` does full overwrite; `sync_file_observe` leaves baseline frozen when a user edit is detected, only new / tombstoned blocks are touched. Watcher always calls observe.
- **Where**: marrow/md_index.py:107 · marrow/md_index.py:171 · marrow/md_index.py:228

### reconcile
- Each route parses its md section by id anchors / heading markers, diffs against DB, applies INSERT/UPDATE/DELETE with audit_log entries. Fail-soft: error logs alert, render proceeds.
- **Routes** (all complete): `reconcile_milestones` (bidirectional) · `reconcile_milestone_candidates` (✅/❌/row-delete) · `reconcile_tasks` (tick/untick/archive/insert) · `reconcile_affect` (ep-segment + pending label/desc edit) · `reconcile_atlas` (heading tree upsert/delete)
- **Shared dedup**: tasks reconcile + all candidate writers gate inserts through `semantic_dedup` (cosine vs existing rows; per-table threshold + fast_skip in config.toml). See §2.3 for thresholds.
- **Where**: marrow/reconcile.py:162 · 397 · 595 · 1000 · marrow/atlas.py:358

### watcher
- launchd-supervised process. Watches dashboard.md, handover.md, db-pages/ via `watchdog.Observer`. Boot: observe-only `full_scan` to cover crash gap. Events debounce 200ms per path → `sync_file_observe` (md_index hash/tombstone update only, never renders).
- Hosts two timer threads in the same process: **SyncLoop (5s)** + **AtlasSweepLoop (60s)**.
- **Where**: marrow/watcher.py:285 · marrow/watcher.py:318 · marrow/watcher.py:31

### sync_loop
- 5s tick. Compares md mtime vs `max(updated_at)` of the subpage's source tables. md newer → `write_subpage` (reconcile then render). DB newer → `write_subpage` / `write_dashboard`.
- `USER_ACTIVE_WINDOW_S = 3.0`: skip render if md was touched within 3s (no rewrites under the cursor).
- **Where**: marrow/sync_loop.py:135 · marrow/sync_loop.py:180 · marrow/sync_loop.py:26

### drift_sweep
- Detects rename/move via watchdog. **Trigger A** = same-root move (immediate). **Trigger B** = cross-root inferred from delete+create with matching basename+size within 30s.
- Refs found via `rg`, classified safe / unsafe. Safe auto-applied with info alert. Unsafe held in pending JSON, await `mw drift apply <pid>`.
- **AUTHORIZED_ROOTS**: ~/CC-Lab · ~/.config · ~/.claude · ~/Desktop/NY · ~/Library/Mobile Documents/com~apple~CloudDocs/Study (identical to atlas seed roots).
- `refresh_dir_tree` (legacy): regenerates ~/.config/marrow/dir_tree.md on every apply_confirm; not user-facing, kept for back-compat. · marrow/drift_sweep.py:461
- **Where**: marrow/drift_sweep.py:32 · marrow/drift_sweep.py:704 · marrow/drift_sweep.py:635

### atlas
- `seed_atlas_from_roots` inserts one stub (depth=1) per AUTHORIZED_ROOT (idempotent INSERT OR IGNORE). `atlas_sweep_fs` depth-walks each row with depth > 0, stubs new subdirs, deletes vanished ones. `reconcile_atlas` reads atlas.md heading markers back to DB.
- Seed roots = drift_sweep.AUTHORIZED_ROOTS. `ATLAS_ROOT_ORDER` controls display order independently.
- Single canonical render at `~/Desktop/NY/db-pages/atlas.md` (no copy under ~/.config/marrow/db-pages; other docs just hyperlink the canonical path).
- **Where**: marrow/atlas.py:642 · marrow/atlas.py:442 · marrow/atlas.py:55 · marrow/subpage_specs.py:406

### tombstone
- Marks blocks Lumi deleted so re-render doesn't re-emit them. Three live paths, none of them retired:
  1. **md_index `tombstone_at`** — subpage / dashboard blocks. Watcher sees a block vanish from md → `MdIndex.tombstone(path, block_id)`. dashboard.py:112 / inserter.py:137 read via `is_tombstoned()` / `list_tombstones()`. Aging 30d → DELETE. · marrow/md_index.py:60-63 · marrow/aging.py:162
  2. **audit_log `action='tombstone'` / `action='handover_tombstone'`** — milestone candidate rejects + handover thread deletes. `candidates.py:241` queries this before re-emitting a milestone. No aging, permanent. · marrow/candidates.py:241
  3. **handover_diff.py:191 in-memory diff** — `snap_ids - doing.keys()` against the last `handover_snapshot` row in audit_log. Snapshot itself persisted; the tombstone set is recomputed each sessionend.
- `marrow/tombstone.py` defines `AuditLogTombstoneStore` + `MdIndexTombstoneStore` + `TombstoneStore` Protocol — early abstraction layer, **never imported by any caller**. Dead code; safe to delete, no effect on the three live paths above.
- **Where**: marrow/md_index.py:60 · marrow/handover_diff.py:191 · marrow/dashboard.py:112 · marrow/inserter.py:137

### 5.4 Write arbitration

Three writers touch the dashboard top region:
1. **watcher** — observe-only, never renders (md_index hash/tombstone update only)
2. **sync_loop** — sole timed renderer (5s tick, same process as watcher)
3. **sessionend-tail** — one-shot renderer at end of each sessionend, outside the 5s cycle, to flush newly-written affect/task/digest

Both 2 and 3 call `write_dashboard` which runs reconcile (idempotent) then atomic write. A race = two successive atomic writes, second wins, no DB edit lost because reconcile ran in both. sync_loop guards with `USER_ACTIVE_WINDOW_S = 3.0` (skip if md touched within 3s); sessionend-tail has no guard (session is over, no editing).

---

## 6. Handover subsystem

- **3-section model**: `## Done` (CLOSEd threads rolling off after 24h, each stamped with `<!-- done:EPOCH -->`); `## Doing` (open threads keyed by `<!-- id:N -->` — code-managed, hand-edit reconciled); `## Lumi's Note` (freeform, Lumi-owned — code only removes lines she clearly completed, never adds or rewrites).
- **diff-apply engine**: SessionEnd's STATE call emits a ===DOING_DIFF=== block with four verbs: CLOSE (move thread to Done, stamp epoch), UPDATE (rewrite thread body, keep id), KEEP (no-op), ADD (assign fresh id). apply_diff reads the current file under flock, reconciles hand-edits vs last snapshot (ids vanished → tombstoned, never revived; new no-id blocks → fresh id), applies the diff, rolls off Done entries older than 24h (_DONE_MAX_AGE = 86400), and removes Note lines whose hash_bullet matches a NOTE_DONE entry. Write is atomic temp-replace, fallback to .partial.<sid> on lock-loss.
- **single-writer rule**: seg_handover in sessionend_writers.py is the sole writer, only called from sessionend_async. session_start() in hooks.py reads `## Lumi's Note` as context but never calls apply_diff or touches the file — read-only by design so a concurrent or resumed session cannot clobber an open thread.
- **dual identity**: handover.md is a standalone subsystem — NOT in the subpage registry. It has its own render path (handover_render.py, handover_diff.py) separate from the subpage inserter/reconcile cycle. The watcher watches it for md_index updates only. Peer of dashboard.md, not a db-page.
- **normalisation**: handover_norm.py provides bullet normalisation + sha1 hash used by Note-line tombstone matching so re-rendered Notes match Lumi's hand-edits character-for-character · marrow/handover_norm.py:1
- **mm-/mm+ control plane**: UserPromptSubmit accepts `mm-` (manual skip current session) and `mm+` (force sessionend rerun) prefixes that bypass the spawn gate · marrow/hooks.py:416
- **Where**: marrow/handover_diff.py:1 · marrow/handover_render.py:28 · marrow/sessionend_writers.py:256 · marrow/hooks.py:175 · marrow/handover_template.md:1

---

## 7. Scheduled jobs (launchd)

- com.marrow.watcher: persistent · RunAtLoad + KeepAlive=true · auto-restarted on crash · marrow/watcher.py
- com.marrow.dashboard-tick: daily 06:01 · force-render dashboard at startup · deploy/mw-dashboard-tick.plist
- com.marrow.goose-bites: daily 06:30 · distil best-of-day quote from goose pipeline · deploy/mw-goose-bites.plist
- com.marrow.daily-routine: daily 07:00 · full candidate extraction + diary write for yesterday · deploy/mw-daily-routine.plist
- com.marrow.daily-catchup: daily 19:00 · backfill last 7d event-days with no diary, cap 3/run · deploy/mw-daily-catchup.plist
- com.marrow.db-backup: daily 03:00 · VACUUM INTO local + iCloud offsite, keep newest 14 · deploy/mw-db-backup.plist
- com.marrow.aging: weekly Sun 12:00 · five-pass cleanup (memes/tasks/milestone alerts/goose blocks/md_index tombstones) · deploy/mw-aging.plist
Total: 7 plists; watcher is the only persistent process, the other 6 are scheduled jobs. MCP daemon has no plist — CC launches it on-demand via .mcp.json. (CC's own jsonl-cleanup lives in ~/.claude/settings.json, separate from marrow.)

---

## 8. Alerts

- Stored in `alerts` table via `repo.add_alert(severity, type, message, source)`; idempotent on (severity, type, message, source) unresolved row. Severity: info / warn / critical.
- Surface: dashboard `## Alerts` (top_sections.render_alerts) + SessionStart handoff payload. Resolve: `mw resolve <id>` manual; aging auto-resolves `milestone_added` > 7d.
- **Where**: marrow/storage.py:106 · marrow/repo.py:68 · marrow/top_sections.py:88 · marrow/aging.py:99

### 8.1 Scenarios

- backup · critical/warn · local VACUUM or iCloud offsite copy failed · backup.py:127,140
- daily routine · critical/warn · daily run aborted / diary write failed / candidate parse or per-day partial fail · daily.py:167,228,258,322
- daily subroutine · warn · daily subpage render or goose-bites pick failed · daily.py:301,313
- dashboard reconcile · warn · milestone/task/affect reconcile raised during dashboard render · dashboard.py:150,158,166
- dashboard write · warn · sessionend-tail dashboard render failed · sessionend_async.py:486
- subpage db_pages · warn · subpage render / sync / reconcile / inserter / md_index path failed · subpages.py:118,129,136,378,388
- atlas sweep · warn · atlas_sweep_fs via subpage path failed · subpages.py:293
- atlas hook · info · PreToolUse atlas guidance raised · hooks.py:679
- hook main · warn · top-level hook crash · hooks.py:704
- hook spawn · warn · catchup or sessionend_async popen failed · hooks.py:211,333 + sessionstart_catchup.py:330
- catchup retry · critical/warn · sessionend_async catchup respawn outcome · sessionend_async.py:233
- catchup silent death · critical · sessionstart_catchup found a session vanished mid-flight · sessionstart_catchup.py:273
- embed lane · warn · embed_pending raised (alert #169 site) · sessionend_async.py:496
- unanchored task · warn · task line in md with no DB id · reconcile.py:794
- drift sweep · info/warn/critical · move/rename apply paths (dynamic via _emit_alert) · drift_sweep.py:452

### 8.2 Known gaps

- watcher crash · no alert · watchdog.Observer dying silently kills the sync layer
- embed_pending UNIQUE · DB-level UNIQUE collisions bypass the try/except, alert #169 hides root cause
- sync_loop reconcile exception · raised tick re-tries forever, no alert surfaces
- atlas_sweep_fs standalone · launchd path skips the subpages.py:293 alert wrap

---

## 9. Catchup & self-heal

- sessionstart_catchup: fires at every SessionStart; checks all sids seen in last 24h; max 2 spawns per run · marrow/sessionstart_catchup.py:9
  silent_death alert: if start marker is ≥30 min old, ppid is dead, and no lifecycle:end row exists, log a critical alert before classifying · marrow/sessionstart_catchup.py:273
  Seven classifier states:
  1. ppid live → skip (active session still running)
  2. lifecycle:end + ok,user_count=N + events.user_count > N → spawn (session resumed and grew)
  3. lifecycle:end + ok,user_count=N + events.user_count ≤ N → skip (extraction covers it)
  4. lifecycle:end + no ok + elapsed < 5 min → skip (async still in grace window)
  5. lifecycle:end + no ok + elapsed ≥ 5 min → spawn (async died mid-run)
  6. no lifecycle:end + ppid dead → spawn (end hook never fired)
  7. no marker rows + sid in 24h events → spawn (cc died before hooks)
- daily_catchup: timed (mw-daily-catchup 19:00) · scans last 7d for event days with no diary, backfills cap 3/run · marrow/daily_catchup.py:55
- affect-heartbeat: at SessionStart, if any day in last 7d had events but no affect_live row, injects warning into session-start payload · marrow/hooks.py:123
- dormant-revive: during recall scoring, events with importance≤3 and age>90d are excluded unless an FTS keyword hit revives them (clears superseded_by) · marrow/recall.py:914
- diary-orphan: on every embed_pending diary lane call, sweeps diary_vec/diary_vec_meta rows whose rowid no longer maps to a diary table row (daily DELETE+INSERT reassigns rowids) · marrow/recall.py:340

---

## 10. Cleanup / aging

All five passes run inside `com.marrow.aging` weekly Sun 12:00.

**Tables that age**:
- **memes**: `pinned=0 AND last_seen < 90d` → **DELETE**. NULL last_seen or pinned=1 (paw / fact auto-pinned, plus any hand-pinned) never touched. · marrow/aging.py:48
- **tasks**: `status=active` with 0 FTS phrase-match hits of the title in events over last 30d → `status=archived`. · marrow/aging.py:63
- **milestone alerts** (type=milestone_added only): `resolved=0 AND age > 7d` → `resolved=1` (treated as auto-confirmed if Lumi didn't reject). · marrow/aging.py:99
- **goose_log md blocks**: `### YYYY-MM-DD` blocks older than 7d deleted from the monthly md; empty monthly files removed. · marrow/aging.py:116
- **md_index tombstones**: `tombstone_at IS NOT NULL AND age > 30d` → **DELETE**. · marrow/aging.py:162

**Tables with NO automatic retirement**:
- affect (rows stay indefinitely; `resolved_at` marks reconcile completion, not aging)
- entities (live until manually superseded; `superseded_by IS NOT NULL` = dormant)
- milestones (rows stay; only the milestone_added *alert* ages, the milestone row itself doesn't)
- diary, events, audit_log, session_digests, atlas, stickers, pit (no aging logic)

---

## 11. Infra

### 11.1 daemon / MCP

### daemon
- Persistent stdio MCP server (FastMCP) exposing three tools: `recall` · `atlas_lookup` · `embed_pending`. Holds bge-m3 model weights in memory across calls.
- CC spawns + owns the process for the session lifetime via `.mcp.json`. No launchd plist.
- **Where**: marrow/daemon.py:14 · ~/CC-Lab/marrow/.mcp.json

### 11.2 llm provider

### llm
- **Abstraction**: `LLMClient.call(role, body, tier)` — caller passes intent + tier only; provider/model resolved internally via config. New provider = add `[llm.X]` block + `_run` kind branch + edit `default` key.
- **Provider**: `claude_cli` stream-json.
- **Chain (dormant)**: default.toml sets default="claude_cli", emergency="", no fallback → no rotation.
- **Isolation**: spawned `claude` gets `_ISOLATION = ["--setting-sources", "", "--strict-mcp-config"]` to block persona/MCP bleed.
- **Tier dispatch**: caller passes intent + tier (cheap/mid/top) → resolved to model via `config.toml [tiers]`.
- **Refusal detection**: `stop_reason=="refusal"` primary; fingerprint scan (`_REFUSAL_FINGERPRINTS`) when `is_error==false`.
- **Failure**: 1 retry/provider → `LLMError` + critical alert; no in-process fallback. Recovery: sessionstart_catchup (async re-spawn), daily_catchup (diary backfill).
- **Cost log**: successful calls write `llm_call_cost` to audit_log (model, tokens). Best-effort, never raises.
- **Where**: marrow/llm.py:76 · marrow/llm.py:95 · marrow/llm.py:123 · marrow/llm.py:292 · marrow/config.default.toml:25

### 11.3 config / paths / on-disk layout

On-disk layout:
- DATA_DIR = ~/.config/marrow · marrow/config.py:8
- dashboard.md = ~/Desktop/NY/dashboard.md · marrow/paths.py:20
- handover.md = ~/.config/marrow/handover.md (canonical); ~/Desktop/NY/handover.md is a symlink to it · marrow/paths.py:21
- db-pages/ = ~/Desktop/NY/db-pages/ · marrow/config.py:56

config.toml catalog:
- [paths]: overrides for db, backup_dir, offsite_backup_dir, dashboard, db_pages, db_pages_state
- [backup]: keep count (flat daily retention, default 14)
- [llm]: provider chain + per-provider sub-tables [llm.claude_cli] / [llm.ollama]
- [tiers]: intent-to-model mapping (cheap/mid/top)
- [embedding]: bge-m3 model id, dim=1024, provenance tag
- [recall]: vector flag, fusion weights (w_vec/w_bm25/w_recency/w_affect + per-lane), min_score
- [sessionend]: skip_turn_threshold
- [memes_dedup] / [tasks_dedup] / [milestones_dedup] / [entities_dedup]: cosine_threshold + fast_skip_count per table
- [subpages]: top/bottom/hidden render order lists
- [transcript]: worker_models list for headless-spawn detection

### 11.4 backup / popen_detach / migrate

### popen_detach
- Fire-and-forget subprocess launcher. Mandatory 4-flag combo (any one missing reproduces the ny-memm stuck-prompt hang 100%): `stdin=DEVNULL` · `stdout/stderr → log fd (append, child owns)` · `start_new_session=True` · `close_fds=True`.
- **Where**: marrow/popen_detach.py:14 · marrow/hooks.py:208 · marrow/hooks.py:326

### backup
- `VACUUM INTO` temp file → `os.replace` into `~/.config/marrow/backup/marrow-YYYY-MM-DD.db`. Offsite leg copies same pattern to iCloud; offsite failure → `warn` alert but local leg still succeeds.
- Flat daily retention: keep newest N each side (default 14). Scheduled 03:00 via `com.marrow.db-backup`.
- **Where**: marrow/backup.py:59 · marrow/backup.py:100 · deploy/mw-db-backup.plist

---

## 12. Addons

> wallet is covered in §5.2 (subpage catalog) — not duplicated here.

### 12.1 daily / day-plan
Two unrelated systems sharing the "daily" name:
- **daily.py** — automated pipeline, launchd-scheduled (07:00 writes yesterday's diary + candidate extraction; 19:00 catchup backfills up to 3 missing days). Two Sonnet calls per run: one for candidates, one for diary prose.
- **day-plan** — interactive CC skill at `.claude/skills/day-plan/SKILL.md`, Scan → Brainstorm → Self-grill → Plan loop, saves plan files to `docs/plans/`.
- **Where**: marrow/daily.py:1 · marrow/daily_prompts.py:1 · marrow/daily_catchup.py:3 · .claude/skills/day-plan/SKILL.md

### 12.2 buddy / goose-bites
Two unrelated systems both centred on the goose (铁锅) persona:
- **buddy** — external claude-buddy MCP at `CC-Lab/external/claude-buddy/`, renders status-line persona. End-of-turn comments are HTML, stripped by `transcript.py:40` before sessionend extraction.
- **goose_bites** — `select_quote_for_date` parses monthly `YYYY-MM.md` log, Haiku (`tier=cheap`) picks best line, upserts into `goose_bites` table. Runs inside the 19:00 daily catchup; no independent plist.
- **Where**: marrow/goose_bites.py:1 · marrow/subpage_specs.py:285 · marrow/transcript.py:40 · CC-Lab/external/claude-buddy/

---

## 13. Invariants & current status

**Invariants** (rules that must hold):
- single-writer on handover.md (sessionend_async only; SessionStart is read-only)
- flock on every md write (handover_render.py, sessionend_writers.py, inserter.py)
- lifecycle:end must commit to audit_log before the LLM popen is spawned
- byte-identical transcript fence = shared prompt-cache prefix across STATE + NARRATIVE
- 4-flag detach on every popen_detach (DEVNULL + log fd + start_new_session + close_fds)
- DB is SoT: subpage renders never trust md free-form text inside rendered blocks

**Current status**:
- stub: wallet (transactions table not shipped) · profile (entity Phase 2 not wired) · stickers (auto-describe ingest not shipped) · cheatsheet (file empty, hand-written when ready)
- wip: study/projects child pages on legacy read_only render (no inserter) · candidate pin/drop/edit HTML buttons designed but not built
- dead code (safe to delete, no functional impact): `marrow/tombstone.py` TombstoneStore Protocol + AuditLog/MdIndex store classes — never imported. The real tombstone paths live in md_index + audit_log + handover_diff (see §5.3 tombstone).
- unwired: bridge (Phase 4 WeChat socket) · affect emotion backdrop in SessionStart (Phase 2)

