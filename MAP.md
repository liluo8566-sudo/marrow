# Marrow — MAP

> Projection of marrow's ACTUAL implementation. Read this + DESIGN and any session
> knows how the product works without reading source code. Code is SoT; MAP follows it.

## 1. System map

### 1.1 Data-flow diagram + 3 runtimes

```
transcript (jsonl) → hooks.session_end → archive events → SQLite DB
                                        ↓ popen_detach
                               sessionend_async (LLM extraction)
                                        ↓
                          STATE + NARRATIVE → DB tables (tasks/milestones/affect/…)
                                        ↓
                              watcher / sync_loop → render → db-pages/*.md
                                        ↑
                              user edits md → reconcile → DB writeback
```

- **daemon (MCP)** — stdio MCP server exposing recall / atlas_lookup / embed_pending; launched on-demand by CC via .mcp.json, persistent for the session lifetime.
- **watcher (launchd)** — long-running file-system watcher; detects md edits, updates md_index, triggers reconcile + render; supervised by launchd with KeepAlive=true.
- **hooks (one-shot)** — CC lifecycle callbacks (SessionStart / SessionEnd / UserPromptSubmit / PreToolUse); spawn as a child process per event and exit immediately after injecting context or firing popen_detach.

### 1.2 Hooks registry

- SessionStart: every session open · injects open tasks + alerts + affect backdrop as additionalContext; spawns sessionstart_catchup detached · marrow/hooks.py:205
- SessionEnd: every session close · archives transcript events, writes lifecycle:end, spawns sessionend_async detached · marrow/hooks.py:272
- UserPromptSubmit: every user turn · handles mm-/mm+ control prefixes + injects recall-fusion cards as additionalContext · marrow/hooks.py:482
- PreToolUse (Write/Edit/Bash): every file-write or file-op · emits atlas placement guidance for the target path's ancestor chain; global hooks also apply prompt-guard (CJK/table lint) and prompt-lint (haiku trim) · marrow/hooks.py:541 · ~/.claude/hooks/prompt-guard.py · ~/.claude/hooks/prompt-lint.py
- Non-marrow CC hooks (claude-buddy react/comment, codebar telemetry, permission proxy) live in ~/.claude/settings.json

---

## 2. Write path

### 2.1 Session capture

### session-capture
- **What**: Converts the CC transcript JSONL into cleaned event rows stored in the events table, used as the primary recall source.
- **Why**: The raw JSONL contains tool calls, thinking blocks, sidechain spawns, and buddy comments that must be stripped before any LLM or recall step sees the data.
- **How**: SessionEnd hook reads the transcript path from hook stdin, passes it through transcript.clean() (code-only, no LLM) to keep only user/assistant text blocks with source_hash dedup, then calls repo.archive_events() which inserts idempotently. Headless spawns (detected by matching known prompt heads) are dropped entirely.
- **Where**: marrow/hooks.py:272 · marrow/transcript.py:1 · marrow/storage.py:22

### 2.2 Sessionend extraction (STATE / NARRATIVE)

### sessionend-extraction
- **What**: Detached async process that makes two sequential Sonnet calls on the archived transcript and runs four segment writers: handover, task_cand (STATE call), affect, digest (NARRATIVE call).
- **Why**: The session needs both structured state updates (task ticks, handover diff) and free-text narrative (affect episodes, diary digest) extracted from the same transcript without blocking CC session close.
- **How**: The hook spawns sessionend_async with popen_detach after writing the lifecycle:end audit row. STATE and NARRATIVE both begin with the byte-identical _TRANSCRIPT_BLOCK fence; NARRATIVE's cache read comes from that shared prefix. Writers for each call run immediately after their respective LLM response and are independent.
- **Serial-not-parallel**: Cache-prefix reuse is the benefit, not a data dependency — both calls share the byte-identical transcript fence. They run in one process with no async runtime, so serial is the simplest safe path. If STATE fails, NARRATIVE still runs; partial:<failed_writers> is written when 1–3 of 4 writers fail, fail:all only when all four fail.
- **Where**: marrow/sessionend_async.py:423 · marrow/sessionend_prompts.py:24 · marrow/sessionend_writers.py:63 · marrow/hooks.py:320

### 2.3 Daily aggregation (candidate extraction)

### daily-aggregation
- **What**: Nightly job that runs one Sonnet call on aggregated session_digests for a day, extracting entity, milestone, and memes candidates into their respective candidate tables.
- **Why**: Candidate extraction was moved out of sessionend (per-session) to daily (per-day) so duplicate inserts are reduced and the cost is one LLM call per day instead of N per N sessions.
- **How**: daily.py reads all session_digests + affect_live rows for the target date, assembles them into one digest aggregate, calls DAILY_CAND_PROMPT (mid-tier Sonnet), then runs three independent block writers (entity / milestone / memes), each parsing its own ===BLOCK===/===END=== marker. One block failing to parse does not block the others.
- **Where**: marrow/daily.py:150 · marrow/daily_prompts.py:28 · marrow/candidates.py:38

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
- memes: concept glossary / lore entries · status = active/dormant · pinned=0 + last_seen 90d → dormant; 1y → DELETE · marrow/storage.py:57
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
- **What**: Converts text rows into 1024d float vectors using BAAI/bge-m3 via ONNX runtime, stored in six cross-table vec lanes for semantic search.
- **Why**: Local-only bge-m3 at 1024d is a Lumi-locked constraint — no cloud API call, no dimension change without a full re-embed cycle.
- **How**: Lazy-loaded singleton loads model.onnx + tokenizer.json from the HuggingFace hub snapshot; CLS-pool output is L2-normalised to 1024d. embed_pending() iterates all six lanes up to batch rows each per call so a large events backlog cannot starve cross-table lanes.
- **Vec lanes**: events · memes · entities · milestones · diary · tasks
- **Where**: marrow/recall.py:59 · marrow/recall.py:399 · marrow/recall.py:132 · marrow/storage.py:224

### 4.3 Recall fusion (scoring)

### recall
- **What**: Fuses FTS5 keyword, bge-m3 vector, recency decay, and affect importance into a single scored ranked list of memory cards.
- **Why**: No single signal covers all retrieval — vec alone misses exact name matches; FTS alone misses semantic drift; recency + affect weight live context.
- **How**: FTS5 (BM25-normalised) and vec (cosine) candidates merge by event id, scored with weighted sum (vec=0.55, bm25=0.30, recency=0.15, affect=0.10). Milestones/memes/diary/tasks lanes add vec-only or substring candidates with reserved slot caps so anchor rows are not starved. Entity force-include prepends entity-card + linked events for any name found in the query, bypassing FTS to handle 2-char CJK names.
- **Key thresholds**: min_score=0.35 (events only; milestones/memes/entities skip this gate) · vec-only floor=0.40 (cross-table lanes below excluded) · dormant rule: importance≤3 and age>90d excluded unless FTS keyword hit revives
- **Where**: marrow/recall.py:693 · marrow/recall.py:433 · marrow/recall.py:444 · marrow/recall.py:455 · marrow/entity_recall.py:73

---

## 5. Surface (DB ↔ md)

### 5.1 Dashboard render contract

### dashboard
- **What**: The main user-visible file (dashboard.md) that aggregates all marrow state into one Obsidian note.
- **Why**: Reconcile-before-render ensures Lumi's ticks, votes, and emoji-decisions flow back to DB before fresh content overwrites them; the block-hash gate then skips blocks the user has hand-edited, so free-form text survives auto-writes.
- **How**: write_dashboard runs three reconcile passes (milestone candidates, tasks, affect) to absorb md edits into DB first. It then calls top_sections.iter_top_blocks to render fresh block bodies and passes them through _resolve_blocks, which reads per-block hashes from md_index to decide: overwrite (hash matches baseline), skip (user-edited), or omit (tombstoned). The assembled top region is written atomically; hashes are recorded only after the write succeeds.
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
- **What**: study.md index with Obsidian links per unit; child study/<unit>.md per task group.
- **Status**: wip — index inserter; child pages read_only legacy render, skipped by watcher
- **Where**: marrow/subpages.py:164 · Write: index=inserter / children=legacy read_only · Direction: DB→md

#### projects (index + per-project pages)
- **What**: projects.md index, plus per-project detail pages under projects/.
- **Status**: wip — index inserter; child pages read_only legacy (Phase E will add file-per-project + frontmatter)
- **Where**: marrow/subpages.py:211 · Write: index=inserter / children=legacy read_only · Direction: DB→md

#### cheatsheet
- **What**: Hand-written reference sheet; disk is SoT.
- **Status**: done (read_only, no DB backing, only subpage with no InserterSpec — full file overwrite)
- **Where**: marrow/subpages.py:329 · Write: legacy read_only · Direction: read_only (disk SoT)

#### atlas
- **What**: Directory heading tree for AUTHORIZED_ROOTS, with description and naming hints.
- **Status**: done (inserter + reconcile_atlas bidirectional + atlas_sweep_fs)
- **Notes**: respect_tombstones=False (depth changes must resurface tombstoned paths); ATLAS_ROOT_ORDER decoupled from AUTHORIZED_ROOTS order.
- **Where**: marrow/subpage_specs.py:406 · marrow/atlas.py:358 · marrow/atlas.py:442 · Write: inserter · Direction: bidirectional

### 5.3 Sync machinery

### md_index
- **What**: Tracks per-block content hashes for every id-marked block across all watched md files, enabling the inserter to distinguish user edit from auto-write identical content.
- **Why**: Without a baseline hash, the inserter cannot tell whether a diverging block body is a user edit to preserve or an auto-write to replace.
- **How**: MdIndex wraps the md_index SQLite table. sync_file does full overwrite; sync_file_observe leaves baseline frozen when a user edit is detected — only new/tombstoned blocks are written. The watcher always calls observe to keep the "last auto-write" signal intact.
- **Where**: marrow/md_index.py:107 · marrow/md_index.py:171 · marrow/md_index.py:228

### reconcile
- **What**: Absorbs user edits from md files back into the DB before each render.
- **Why**: Without md→DB pass, inserter would re-emit prior DB state, silently overwriting anything Lumi typed.
- **How**: Each route parses the relevant md section by id anchors or heading markers, diffs against DB rows, and applies INSERT/UPDATE/DELETE with audit log entries. Fail-soft: error logs alert and falls through to render.
- **Routes (all complete, no half-built)**: reconcile_milestones (bidirectional) · reconcile_milestone_candidates (✅/❌/row-delete) · reconcile_tasks (tick/untick/archive/insert) · reconcile_affect (ep-segment + pending label/desc edit) · reconcile_atlas (heading tree upsert/delete)
- **Where**: marrow/reconcile.py:162 · 397 · 595 · 1000 · marrow/atlas.py:358

### watcher
- **What**: Long-running launchd process that watches dashboard.md, handover.md, and db-pages/ for FS events, updating md_index on each change.
- **Why**: The inserter needs near-real-time tombstone and hash signals; polling would miss rapid edits or add unacceptable latency.
- **How**: On boot, observe-only full_scan to cover crash gap, then watchdog.Observer on three roots. Events debounce 200ms per path before calling sync_file_observe, which updates tombstones and new-block records without touching the auto-write baseline. SyncLoop (5s tick) and AtlasSweepLoop (60s tick) run inside the same process.
- **Where**: marrow/watcher.py:285 · marrow/watcher.py:318 · marrow/watcher.py:31

### sync_loop
- **What**: Periodic 5s mtime-comparison loop that triggers reconcile+render for each subpage and dashboard when DB or md has changed.
- **Why**: Watcher only updates md_index; the actual db↔md sync (subpage render, reconcile write-back) needs a timer that won't block the event thread.
- **How**: Each tick compares md mtime vs max DB updated_at for the target's tables. md newer → write_subpage (reconcile then render). DB newer → write_subpage / write_dashboard. USER_ACTIVE_WINDOW_S = 3.0 guard skips render if md was touched within 3s, preventing rewrites under the cursor.
- **Where**: marrow/sync_loop.py:135 · marrow/sync_loop.py:180 · marrow/sync_loop.py:26

### drift_sweep
- **What**: Detects file renames/moves across AUTHORIZED_ROOTS and updates path references in text files to prevent dangling links.
- **Why**: Without this, any rename silently breaks every md/py/config reference to the old path.
- **How**: DriftWatcher receives watchdog events. Same-root moves trigger immediately (Trigger A); cross-root moves are inferred from delete+create with matching basename+size within 30s (Trigger B). Refs found via ripgrep, classified safe vs unsafe, auto-applied for safe with info alert, held in pending JSON for `mw drift apply <pid>` for unsafe.
- **AUTHORIZED_ROOTS**: ~/CC-Lab · ~/.config · ~/.claude · ~/Desktop/NY · ~/Library/Mobile Documents/com~apple~CloudDocs/Study (identical to atlas seed roots)
- **Where**: marrow/drift_sweep.py:32 · marrow/drift_sweep.py:704 · marrow/drift_sweep.py:635

### atlas
- **What**: Maintains a directory-tree subpage (atlas.md) seeded from AUTHORIZED_ROOTS, with user-editable description and naming hints per directory.
- **Why**: CC needs a structured map to locate files and name new artifacts correctly without asking.
- **How**: seed_atlas_from_roots inserts one stub row (depth=1) per AUTHORIZED_ROOT (idempotent INSERT OR IGNORE). atlas_sweep_fs depth-walks each row with depth > 0, stubs new subdirs, deletes vanished ones. reconcile_atlas reads atlas.md heading markers back to DB. Seed roots come from drift_sweep.AUTHORIZED_ROOTS; ATLAS_ROOT_ORDER controls display order independently. Rendered file at ~/Desktop/NY/db-pages/atlas.md is the same file referenced from ~/.config/marrow/db-pages.
- **Where**: marrow/atlas.py:642 · marrow/atlas.py:442 · marrow/atlas.py:55 · marrow/subpage_specs.py:406

### tombstone
- **What**: Marks blocks a user deleted from handover.md so re-renders never re-emit them.
- **Why**: Without tombstones, every sessionend would re-grow bullets Lumi explicitly cleared.
- **How**: Two stores share the TombstoneStore protocol. AuditLogTombstoneStore (live) writes tombstone records into audit_log (target_table='handover'). MdIndexTombstoneStore is the planned Phase-F replacement routing the same calls through md_index with content_hash as block_id. Phase F = the binding flip in storage_for_tombstone() that swaps callers without changing their interface. Currently AuditLog is live; MdIndex implemented but not bound.
- **Where**: marrow/tombstone.py:26 · marrow/tombstone.py:68 · marrow/md_index.py:51

### 5.4 Concurrency & write-arbitration

Three writers touch the dashboard top region:
1. **watcher** (observe-only) fires sync_file_observe on debounced FS events. It only updates md_index hashes/tombstones — never calls write_dashboard. Pure observer.
2. **sync_loop** (renderer, 5s) is the sole timed renderer. Each tick compares mtimes and calls write_dashboard when DB is newer. Both watcher and sync_loop run in the same process.
3. **sessionend-tail** (one-shot renderer) calls write_dashboard directly at the end of each sessionend run, outside the sync_loop cycle, to flush newly-written affect/task/digest rows before next session.

write_dashboard runs reconcile (absorbs md edits) then renders atomically. sync_loop and sessionend-tail call the same function; a race produces two successive atomic writes — second wins, but both first ran reconcile (idempotent), so no DB edit is lost. USER_ACTIVE_WINDOW_S = 3.0 guard skips sync_loop render if md was touched within 3s; sessionend-tail has no such guard (runs after session ends, not during active editing).

---

## 6. Handover subsystem

- **3-section model**: `## Done` (CLOSEd threads rolling off after 24h, each stamped with `<!-- done:EPOCH -->`); `## Doing` (open threads keyed by `<!-- id:N -->` — code-managed, hand-edit reconciled); `## Lumi's Note` (freeform, Lumi-owned — code only removes lines she clearly completed, never adds or rewrites).
- **diff-apply engine**: SessionEnd's STATE call emits a ===DOING_DIFF=== block with four verbs: CLOSE (move thread to Done, stamp epoch), UPDATE (rewrite thread body, keep id), KEEP (no-op), ADD (assign fresh id). apply_diff reads the current file under flock, reconciles hand-edits vs last snapshot (ids vanished → tombstoned, never revived; new no-id blocks → fresh id), applies the diff, rolls off Done entries older than 24h (_DONE_MAX_AGE = 86400), and removes Note lines whose hash_bullet matches a NOTE_DONE entry. Write is atomic temp-replace, fallback to .partial.<sid> on lock-loss.
- **single-writer rule**: seg_handover in sessionend_writers.py is the sole writer, only called from sessionend_async. session_start() in hooks.py reads `## Lumi's Note` as context but never calls apply_diff or touches the file — read-only by design so a concurrent or resumed session cannot clobber an open thread.
- **dual identity**: handover.md is a standalone subsystem — NOT in the subpage registry. It has its own render path (handover_render.py, handover_diff.py) separate from the subpage inserter/reconcile cycle. The watcher watches it for md_index updates only. Peer of dashboard.md, not a db-page.
- **Where**: marrow/handover_diff.py:1 · marrow/handover_render.py:28 · marrow/sessionend_writers.py:256 · marrow/hooks.py:175 · marrow/handover_template.md:1

---

## 7. Scheduled jobs (launchd)

- com.marrow.watcher: persistent · RunAtLoad + KeepAlive=true · auto-restarted on crash · marrow/watcher.py
- com.marrow.dashboard-tick: daily 06:01 · force-render dashboard at startup · deploy/mw-dashboard-tick.plist
- com.marrow.goose-bites: daily 06:30 · distil best-of-day quote from goose pipeline · deploy/mw-goose-bites.plist
- com.marrow.daily-routine: daily 07:00 · full candidate extraction + diary write for yesterday · deploy/mw-daily-routine.plist
- com.marrow.daily-catchup: daily 19:00 · backfill missing diary days in last 7d, cap 3/run · deploy/mw-daily-catchup.plist
- com.marrow.db-backup: daily 03:00 · VACUUM INTO local + iCloud offsite, keep newest 14 · deploy/mw-db-backup.plist
- com.marrow.aging: weekly Sun 12:00 · five-pass cleanup (memes/tasks/milestone alerts/goose blocks/md_index tombstones) · deploy/mw-aging.plist
- com.marrow.jsonl-cleanup: weekly Sun 05:00 · runs marrow.cleanup --apply (⚠ module not found in repo — orphan plist) · ~/Library/LaunchAgents/mw-jsonl-cleanup.plist

Daemon (MCP server) is NOT launchd-supervised — started on-demand by CC as a stdio subprocess via .mcp.json (command: python -m marrow.daemon). Watcher is the only launchd-supervised persistent process.

---

## 8. Alerts

### alerts
- **What**: Persistent signal table for cross-system failures and warnings; written by backup, drift, subpages, llm, sessionstart_catchup, daily, sessionend, and hooks; surfaced on the dashboard and injected at session start.
- **Why**: Each subsystem runs detached or async — alerts are the only shared channel that survives process boundaries and survives until a human acknowledges.
- **How**: Any writer calls repo.add_alert(severity, type, message, source); insert is idempotent — duplicate (severity, type, message, source) on an unresolved row returns the existing id. Severity = info / warn / critical (rendered in priority order). Dashboard renders open alerts as `## Alerts` via top_sections.render_alerts; SessionStart injects them into the handoff text block. Manual resolve: `mw resolve <id>`. Aging auto-resolves milestone_added alerts older than 7d.
- **Where**: marrow/storage.py:106 · marrow/repo.py:68 · marrow/repo.py:39 · marrow/top_sections.py:88 · marrow/aging.py:99

---

## 9. Catchup & self-heal

- sessionstart_catchup: fires at every SessionStart; checks all sids seen in last 24h; max 2 spawns per run · marrow/sessionstart_catchup.py:9
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

- memes: DELETE pinned=0 AND last_seen < 90d (NULL last_seen or pinned=1 never touched) · weekly Sun 12:00 · marrow/aging.py:48
- tasks: status=active with 0 FTS mentions in last 30d → status=archived · weekly Sun 12:00 · marrow/aging.py:63
- milestone alerts: type=milestone_added AND resolved=0 AND age > 7d → resolved=1 · weekly Sun 12:00 · marrow/aging.py:99
- goose_log blocks: ### YYYY-MM-DD blocks older than 7d deleted from md; empty monthly files removed · weekly Sun 12:00 · marrow/aging.py:116
- md_index tombstones: tombstone_at IS NOT NULL AND age > 30d → DELETE · weekly Sun 12:00 · marrow/aging.py:162

---

## 11. Infra

### 11.1 daemon / MCP

### daemon
- **What**: Persistent stdio MCP server that exposes three tools — recall, atlas_lookup, embed_pending — to the active CC session.
- **Why**: Hooks are one-shot and cannot hold model weights in memory; the daemon keeps a persistent connection so recall and atlas lookups avoid cold-start overhead.
- **How**: FastMCP registers the three tools over stdio; CC connects via .mcp.json. No launchd plist — CC spawns and owns the process for the duration of each session.
- **Where**: marrow/daemon.py:14 · marrow/daemon.py:22 · marrow/daemon.py:35 · marrow/daemon.py:46 · ~/CC-Lab/marrow/.mcp.json

### 11.2 llm provider chain

### llm
- **What**: Unified LLM call layer that routes pipeline calls through a configured provider chain and maps caller intent (tier) to a concrete model.
- **Why**: Callers must never hard-code a model or know the channel — provider rotation, isolation flags, and usage logging must be in one place.
- **How**: LLMClient reads the chain (default → fallback → emergency) from config.toml; each call passes a tier that resolves to a model id from [tiers]. Providers retry once before rotating; chain exhaustion raises LLMError and fires a critical alert. All spawned claude processes receive _ISOLATION flags (--setting-sources "" --strict-mcp-config) to prevent persona/MCP bleed.
- **Where**: marrow/llm.py:76 · marrow/llm.py:95 · marrow/llm.py:23
- **Chain**: default=claude_cli (stream-json, OAuth 5h window) · fallback=— (none in default.toml) · emergency=ollama (qwen2.5:7b, local, live config only)
- **Tiers**: mid=claude-sonnet-4-6 · cheap=claude-haiku-4-5-20251001

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
- **What**: Fire-and-forget subprocess launcher that applies four mandatory detach flags.
- **Why**: The 4-flag combo is a hard constraint from the ny-memm stuck-prompt incident — missing any one flag reproduces the hang 100%.
- **How**: Opens log in append mode, Popens with stdin=DEVNULL, stdout/stderr→log, start_new_session=True, close_fds=True; child owns the log fd so parent can exit without tearing down the detached process.
- **Where**: marrow/popen_detach.py:14 · marrow/hooks.py:208 · marrow/hooks.py:326

### backup
- **What**: Daily atomic DB snapshot to local backup dir and iCloud offsite, with flat retention pruning.
- **Why**: "DB never lost" invariant — WAL live DB cannot be copied raw; VACUUM INTO produces a consistent read without destructive locks.
- **How**: VACUUM INTO writes temp file, then os.replace into ~/.config/marrow/backup/marrow-YYYY-MM-DD.db; offsite copy uses same pattern to iCloud. Offsite failure raises warn alert but does not fail local leg. Keeps newest N (default 14) each side; scheduled 03:00 via com.marrow.db-backup.
- **Where**: marrow/backup.py:59 · marrow/backup.py:100 · deploy/mw-db-backup.plist

### migrate
- **What**: One-shot historical importer that parses legacy NY md files (events, timeline, cipher memes, pit projects, goose bites) and inserts into the DB with idempotent source_hash guards.
- **Why**: Phase 1 backfill only — loads curated history that predates the live capture pipeline; not part of the ongoing write path.
- **How**: Per-source parsers produce row dicts; _insert checks source_hash before writing and checks audit_log tombstones to prevent reviving rows Lumi has dropped. import_timeline is a separate idempotent path keyed on (scope, date, title) safe for re-runs.
- **Where**: marrow/migrate.py:197 · marrow/migrate.py:161 · marrow/migrate.py:224

---

## 12. Addons

### 12.1 wallet
- **What**: Reserved subpage mount for a bank-statement-style transactions ledger; placeholder rendered until Phase 5.
- **Why**: Slot secured in the subpage registry now so its render position survives future phases without a registry change.
- **How**: InserterSpec fully wired; fetch() returns empty because transactions table has not shipped. Md bootstrapped with "Bank-statement render lands with Phase 5 stellan_wallet."
- **Where**: marrow/subpage_specs.py:261 · marrow/subpages.py:323 · marrow/subpages_render.py:176

### 12.2 daily / day-plan
- **What**: Two distinct systems sharing the "daily" name: daily.py is an automated pipeline writing diary + extracting candidates; day-plan is an interactive CC skill for morning planning and evening review.
- **Why**: The automated pipeline runs on a fixed schedule without user input; the interactive skill is a conversation-driven ritual that needs human judgment.
- **How**: daily.py invoked by launchd (07:00 writes yesterday; 19:00 catchup backfills up to 3 days). Two Sonnet calls per day: one for candidate extraction, one for diary prose. The day-plan skill at .claude/skills/day-plan/SKILL.md runs a Scan → Brainstorm → Self-grill → Plan loop inside a CC session, saving plan files to docs/plans/.
- **Where**: marrow/daily.py:1 · marrow/daily_prompts.py:1 · marrow/daily_catchup.py:3 · .claude/skills/day-plan/SKILL.md

### 12.3 buddy / goose
- **What**: Two unrelated systems: buddy is the claude-buddy MCP rendering the goose persona (铁锅) as a persistent status-line companion; goose_bites.py is a distill pipeline picking one best goose (铁锅) quote per day from the monthly reaction log into the goose_bites DB table.
- **Why**: Buddy provides real-time session presence; goose_bites extracts a shareable daily highlight for long-term storage and recall.
- **How**: Buddy runs as external MCP server at CC-Lab/external/claude-buddy; end-of-turn comments are HTML comments stripped by transcript.py before sessionend extraction. goose_bites.select_quote_for_date parses the monthly YYYY-MM.md log, calls Haiku (tier=cheap) to pick the best line, upserts the winner; runs inside the 19:00 daily catchup, no independent plist.
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
- stub: wallet (transactions table not shipped) · profile (entity Phase 2 not wired) · stickers (auto-describe ingest not shipped)
- wip: study/projects child pages on legacy read_only render (no inserter) · candidate pin/drop/edit HTML buttons designed but not built · MdIndexTombstoneStore implemented but not bound (Phase F)
- unwired: bridge (Phase 4 WeChat socket) · affect emotion backdrop in SessionStart (Phase 2)
- tests: 824 collected (marrow/.venv/bin/pytest --collect-only -q)

---

<!-- ================================================================= -->
## OPEN QUESTIONS
<!-- ================================================================= -->

**Q1.** sessionend's two sonnet calls run serial, not parallel — why?
A: Cache-prefix reuse, not data dependency — both calls share the byte-identical `_TRANSCRIPT_BLOCK` fence (marrow/sessionend_prompts.py:24, marrow/sessionend_async.py:437–476); serial in one process with no async runtime is the simplest safe path. If STATE fails, NARRATIVE still runs unconditionally; `partial:<failed_writers>` is logged when 1–3 of 4 writers fail, `fail:all` when all four fail (marrow/sessionend_async.py:503–542).

**Q2.** Full launchd plist list + each schedule.
A: 8 plists — com.marrow.watcher (RunAtLoad+KeepAlive, no schedule) · daily-routine 07:00 · daily-catchup 19:00 · db-backup 03:00 · goose-bites 06:30 · dashboard-tick 06:01 · aging weekly Sun 12:00 · jsonl-cleanup weekly Sun 05:00 (⚠ marrow.cleanup module missing — orphan plist). Sources: ~/Library/LaunchAgents/ + deploy/*.plist.

**Q3.** How many subpages total? Each: inserter-migrated or legacy?
A: 11 subpages (profile, milestone, diary, memes, stickers, wallet, goose, cheatsheet, study, projects, atlas). All except cheatsheet have inserter specs (marrow/subpage_specs.py:501). Cheatsheet is legacy read_only by design. study/projects index pages use inserter; their child detail pages are legacy-render read_only.

**Q4.** How many reconcile routes? Each complete or half-built?
A: 5 routes, all complete: reconcile_milestones (marrow/reconcile.py:162), reconcile_milestone_candidates (:397), reconcile_tasks (:595), reconcile_affect (:1000), reconcile_atlas (marrow/atlas.py:358). No half-built.

**Q5.** sessionstart_catchup 7-state classifier — list explicitly.
A: (1) ppid live → skip; (2) lifecycle:end + ok,user_count=N + events>N → spawn; (3) lifecycle:end + ok,user_count=N + events≤N → skip; (4) lifecycle:end + no ok + elapsed<5min → skip; (5) lifecycle:end + no ok + elapsed≥5min → spawn; (6) no lifecycle:end + ppid dead → spawn; (7) no marker rows + sid in 24h events → spawn. Source: marrow/sessionstart_catchup.py:9–16.

**Q6.** watcher vs sync_loop boundary — sessionend-tail third writer?
A: Watcher is observe-only — calls sync_file_observe to update md_index hashes/tombstones, never renders (marrow/watcher.py:318). sync_loop owns all timed renders at 5s intervals (marrow/sync_loop.py:135). No overlap. sessionend-tail (marrow/sessionend_async.py:482) is a third one-shot writer calling write_dashboard at end of each session, outside the sync_loop cycle.

**Q7.** handover template's 3 sections — exact names?
A: `Done`, `Doing`, `Lumi's Note` — confirmed in marrow/handover_template.md:3,8,16 and marrow/handover_diff.py:28–30 (`_DONE_HEADER`, `_DOING_HEADER`, `_NOTE_HEADER`).

**Q8.** config.toml — all sections + tunable keys.
A: 11 sections — see §11.3 catalog: [paths] [backup] [llm] [tiers] [embedding] [recall] [sessionend] [memes_dedup] [tasks_dedup] [milestones_dedup] [entities_dedup] [subpages] [transcript]. Source: marrow/config.default.toml.

**Q9.** FTS5 trigram tokenizer CJK behaviour — 2-char name dropped? Worked around?
A: Trigram tokenizer requires ≥3 chars, so a 2-char CN name like (南南) silently returns empty from FTS (marrow/storage.py:197). Live workaround: entity_force_include uses reverse-substring match (name.lower() in query.lower()) outside FTS5, fully CJK-safe for any length ≥1 (marrow/entity_recall.py:73, 164–184).

**Q10.** Why two tombstone stores? What is Phase-F?
A: AuditLogTombstoneStore (marrow/tombstone.py:26) is live — writes tombstones as audit_log rows (target_table='handover'). MdIndexTombstoneStore (marrow/tombstone.py:68) is the Phase-F replacement routing tombstones through md_index. Phase F is the binding flip in storage_for_tombstone() — callers unchanged, only the concrete store swaps. Currently AuditLog is live, MdIndex implemented but not bound.

**Q11.** CC hooks beyond SessionEnd / SessionStart / UserPromptSubmit?
A: Four marrow hooks total: SessionStart, SessionEnd, UserPromptSubmit, PreToolUse on Write/Edit/Bash (atlas placement guidance). Two additional global PreToolUse hooks run alongside on Write/Edit: prompt-guard.py (CJK/table lint) and prompt-lint.py (haiku trim) — not marrow-owned. marrow/hooks.py:686 lists the _EVENTS dict.

**Q12.** drift_sweep AUTHORIZED_ROOTS — coverage? Relation to atlas seeds?
A: AUTHORIZED_ROOTS = ~/CC-Lab · ~/.config · ~/.claude · ~/Desktop/NY · ~/Library/Mobile Documents/com~apple~CloudDocs/Study (marrow/drift_sweep.py:32). Identical to atlas seed roots — seed_atlas_from_roots iterates drift_sweep.AUTHORIZED_ROOTS directly (marrow/atlas.py:653).

**Q13.** daemon lifecycle — who launches it, restart policy?
A: Watcher (marrow.watcher) is launchd-supervised with KeepAlive=true; launchd restarts on crash. The MCP daemon (marrow.daemon) has no launchd plist — CC launches on-demand via .mcp.json and manages its lifetime. Source: ~/Library/LaunchAgents/com.marrow.watcher.plist:28, .mcp.json:3–7.

**Q14.** On-disk layout — DATA_DIR, dashboard.md, handover.md, db-pages, symlinks?
A: DATA_DIR = ~/.config/marrow (marrow/config.py:8); dashboard.md = ~/Desktop/NY/dashboard.md; ONE canonical handover.md at ~/.config/marrow/handover.md with ~/Desktop/NY/handover.md as symlink (not a copy); db-pages/ = ~/Desktop/NY/db-pages/. Source: marrow/paths.py:20–21 + ls confirmed.

**Q15.** atlas seed roots + default depth?
A: Seed roots = drift_sweep.AUTHORIZED_ROOTS, imported at marrow/atlas.py:653; each seeded with depth=1 by seed_atlas_from_roots() after v13 migration. ATLAS_ROOT_ORDER (marrow/atlas.py:55) controls display order independently. The rendered db-pages/atlas.md is the live atlas file — same path served via config from both ~/Desktop/NY/db-pages and ~/.config/marrow/db-pages references.

**Q16.** llm provider chain default / fallback / emergency + tier mid / cheap?
A: default = claude_cli (stream-json over OAuth 5h subscription window); fallback = none in config.default.toml; emergency = ollama (qwen2.5:7b, local, live ~/.config/marrow/config.toml only). Tier mid = claude-sonnet-4-6, cheap = claude-haiku-4-5-20251001. Source: marrow/config.default.toml [tiers].

**Q17.** Current status — stub / wip / unwired? Test count?
A: Stub: wallet (transactions table not shipped) · profile (entity Phase 2 not wired) · stickers (auto-describe ingest not shipped). Wip: study/projects child pages legacy read_only · cheatsheet legacy read_only by design · candidate HTML action buttons designed but not built. Unwired: bridge (Phase 4 WeChat socket) · affect backdrop in SessionStart (Phase 2). Tests: 824 collected.

**Q18.** handover dual identity — subpage or subsystem?
A: Standalone subsystem — no entry in subpages._REGISTRY, not rendered by subpages.py. Own render path (handover_render.py / handover_diff.py) writing to DATA_DIR/handover.md, outside the inserter/reconcile cycle. The watcher watches handover.md as a file-mode root (marrow/watcher.py:63) and md_index tracks its blocks, so it participates in the Surface layer's tombstone + hash machinery only as an observed file.

---

## Agent Notes

> Items found in code NOT covered by the §X sections above.

- AtlasSweepLoop: separate 60s-tick thread inside the watcher process running atlas_sweep_fs independently of the 5s SyncLoop · marrow/sync_loop.py:243
- drift_sweep.refresh_dir_tree: regenerates ~/.config/marrow/dir_tree.md as dirs-only skeleton on every apply_confirm — legacy artifact, not user-facing · marrow/drift_sweep.py:461
- semantic_dedup module: cosine-similarity dedup guard shared by tasks reconcile + memes/milestone/entity candidate writers · marrow/semantic_dedup.py
- handover_norm.py: bullet normalisation + sha1 hash for Note-line tombstone matching · marrow/handover_norm.py:1
- PROGRESS.md append (sessionend_writers.py:303): per-session DONE block flock-written to repo root PROGRESS.md
- mm-/mm+ control plane (hooks.py:416): UserPromptSubmit manual skip + sessionend rerun
- Stale-skip recovery (sessionend_async.py:128): clears skip:short_session when cc fires session_end mid-flush and event count grows past threshold
- Idempotent spawn gate (hooks.py:306): compares current user_count vs last ok row to skip popen when no new events
- silent_death alert (sessionstart_catchup.py:273): logs alert when start marker ≥30min old, ppid dead, no lifecycle:end
- transcript.py:40 strips buddy end-of-turn HTML comments before sessionend extraction (coupling between buddy MCP protocol and write path)
- Orphan plist: com.marrow.jsonl-cleanup references marrow.cleanup module not in repo · ~/Library/LaunchAgents/mw-jsonl-cleanup.plist
- Non-marrow CC hooks (codebar telemetry + permission proxy) live in ~/.claude/settings.json — listed in §1.2 but not detailed
