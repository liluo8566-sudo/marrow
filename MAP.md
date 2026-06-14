# Marrow — MAP

> Speed-read for a new session: how each board works, without opening code. Not SoT — code wins.
> Refs are `file:function` (grep them; line numbers rot). Params inline are the live defaults (config.toml can override).
> Rewritten 2026-06-11 from per-module fact cards + adversarial verify (docs/notes/0611-system-review.md).

## 0. Contents

§1 system map+hooks · §2 write path · §3 read path · §4 storage+recall · §5 surface sync · §7 scheduled jobs · §8 alerts · §9 catchup/self-heal · §10 aging · §11 infra · §12 addons · §13 invariants+status

## 1. System map

```
 CC session ── transcript.jsonl ──SessionEnd──▶ events ──popen──▶ sessionend_async
     ▲                                            │                (TASK_AFFECT + DIGEST)
     │ injected context                           ▼
 hooks (auto) / daemon (MCP) ◀────────── DB (SQLite) ◀── daily.py 07:00 (candidates+diary)
                                          │  events·tasks·affect·entities·milestones·memes
                                          │  diary·digests·alerts·audit_log·atlas·md_index…
                                          │  bge-m3 · 6 vec lanes · recall fusion
                                          ▼▲ watcher + sync_loop (5s) / user md edits
                                Surface: dashboard.md + db-pages/ (11 subpages)
```

Three runtimes:
- **hooks** — one-shot per CC lifecycle event, exit after injecting/spawning.
- **watcher** — launchd persistent (KeepAlive); hosts SyncLoop(5s) + AtlasSweepLoop(60s) threads.
- **daemon** — stdio MCP (recall / atlas_lookup / embed_pending / sticker_search / sticker_pick / sticker_ingest / sticker_update / sticker_delete / sticker_list_pending), spawned by CC via .mcp.json, no plist; holds bge-m3 in memory.

### 1.2 Hooks registry (all in marrow/hooks.py)

- SessionStart `hooks:session_start` — injects affect heartbeat (events-but-no-affect gap day in last 7d) + `timeline:render_timeline` (06/11; see §3). Hardcap 6000 chars. Does NOT inject tasks/alerts. Spawns sessionstart_catchup detached. Writes lifecycle:start marker (ppid+started_at).
- SessionEnd `hooks:session_end` — transcript.clean → repo:archive_events (idempotent by source_hash) → lifecycle:end commits BEFORE popen → idempotent spawn gate (skip popen when user_count ≤ last ok,user_count=N) → popen_detach_lazy sessionend_async. MARROW_BRIDGE=1 suppresses popen (bridge owns timing).
- UserPromptSubmit `hooks:user_prompt_submit` — mm-/mm+ control prefixes + recall fusion injection (params §3). Per-session recall_seen dedup state under DATA_DIR/state/recall_seen/<sid>.json (wiped at start+end).
- PreToolUse `hooks:pretool_use` — Write/Bash placement ops get atlas ancestor-chain guidance (desc + naming_hint); others get a literal path reminder.

## 2. Write path

### 2.1 session capture
- `transcript:clean` code-only strip: tool calls, thinking, sidechains; headless spawns dropped via `transcript:is_headless` (worker_models prefix match + 12 known prompt heads). `repo:archive_events` also bumps entity mention_count + memes use_count in the same txn.

### 2.2 sessionend extraction (sessionend_async.py)
- Skip rule: ≤3 user turns (`[sessionend].skip_turn_threshold`) → terminal `skip:short_session,user_count=N`. Stale-skip recovery `sessionend_async:_drop_stale_skip`: skip row dropped + reprocessed if count later grew past threshold.
- ONE merged sonnet call (replaced sonnet+haiku pair): TASK_AFFECT_DIGEST_PROMPT emits ===TASK===/===AFFECT===/===DIGEST=== fenced blocks → writers seg_task_cand + seg_affect + seg_digest. Per-writer audit rows; digest 0 rows → `fail:zero_rows` (rides the retry chain — immediate digest_zero_write alert deleted 06/11); final row rebuilt from THIS run's segment rows only (`_collect_run_failures`, after latest 'start' stamp): ok,user_count=N / partial:<writers> / fail:*. Transcript lines carry `[HH:MM] [name]` prefixes (Melbourne) — LIFE lines copy these timestamps.
- DIGEST segment is structured lines (KIND casual|task · TL 15-30 CN chars life-perspective · LIFE per-line `HH:MM detail` casual-only · VOICE verbatim casual-only · FACTS one phase-line task-only, TL+FACTS ≤60 words). Parser fullwidth-colon tolerant; parse fail → kind/tl_line/life_lines NULL + alert, body kept raw. AFFECT episodes carry `open` flag (unresolved emotion).
- seg_affect: event_hint resolved FTS→LIKE within same-session events; reconcile_prev resolves most-recent unresolved affect row — KNOWN GAP: lookup is global, not session/date-scoped (review P0-3).
- seg_task_cand: cosine dedup 0.85 vs active + 24h-done tasks; tick-by-id from sonnet `{"id":N,"status":"done"}`.
- Digest raw log → ~/.config/marrow/logs/digest/digest-YYYY-MM-DD.log (6AM cutoff, pruned >2.5d).
- Tail (fail-soft, alerted): `dashboard:write_dashboard` + `recall:embed_pending(batch=200)`.

### 2.3 daily candidates (daily.py 07:00, for yesterday)
- One sonnet call → 3 fenced blocks (entity/milestone/memes), block-isolated parse; second sonnet call → diary prose. Idempotent per date unless --force; serialised by `daily_catchup:app_lock` (fcntl).
- Ingestion gates (`candidates.py`):
  - entities: conf ≥0.8; cosine 0.85 same-kind hit → merge aliases into matched row (never blocks).
  - milestones: conf ≥0.85; cosine 0.85 blocks; tombstone anti-revive via audit_log sha256(scope|date|title); affect importance=5 force-emits a candidate.
  - memes: ALL six types gated by `candidates:_events_like_count_14d` — key on ≥3 distinct calendar days in last 14d; cosine 0.85 vs memes+milestones+entities blocks; paw/fact auto-pinned.
- Shared dedup config `[*_dedup]`: cosine_threshold 0.85 · fast_skip_count 3 (persistent rejects short-circuit via memes_reject_log).
- Empty day (no digests, no affect) writes stub diary row '—' — KNOWN GAP: stub blocks later backfill (review P1-8).

## 3. Read path (what gets injected)

- SessionStart: affect heartbeat warning (§1.2) + `## Timeline` merged affect+events view (`timeline:render_timeline`, ~1100ch): unresolved-episodes line (label (未解), 7d expiry) → Last 24h HH:MM film-strip (per-line real local datetimes w/ midnight-crossing, session first line `HH:MM【tone】` from per-session affect, `--- MM-DD ---` diary-day dividers at 6AM, cap 15) → today-1..-3 diary-date daily header + AM/PM/ND periods (window ends at 24h-strip start, no cross-zone repeat, cap 12) → today-4..-8 week tone+trend line + daily tone + diary.tl_line (one line/day). NULL/rendered-stub/blank tl_line → sanitised 60ch body fallback. Reconcile write-back strips HH:MM【tone】/Day-line prefixes; prefix-only lines = no-op (`reconcile:_extract_tl_text`). Manual notes: `+ [HH:MM] text` line → events channel='manual' (future HH:MM rolls back 1d — backdating), rendered into 24h strip w/ `<!-- tl:e:N -->`; line+anchor delete → tl_hidden=1 (session/diary) or manual-event hard-DELETE + vec cleanup, via `<!-- tl-rendered:s=..;d=..;e=.. -->` trail diff (emitted post budget-trim). No in-progress session line.
- UserPromptSubmit: recall fusion hits as passive context. Render shaping in `hooks:user_prompt_submit`: budget 800 chars · rank_caps [300,120,120,40,40] · rel_cutoff 0.6×top1 · only rank-1 event hit gets ±1 context turns (`recall:fetch_event_context`) · timestamps via `timeutil:format_recall_ts` · recall_seen dedup per session · post-injection `recall:bump_recall_counts` (best-effort).
- Time-lane (passive): `timecue:parse_time_cue` on prompt (昨天/前天/上周X/N天前/X月X号/EN equivalents → Melbourne natural-day → UTC window; future cues → None). Cue + substantive stripped text → windowed fusion takes TOP slots (budget min([recall].timelane_budget 400, budget/2)); stripped trivial → `recall:fetch_window_digests` lines `[MM-DD Day · digest]`, seen-key ("digest", sid). Semantic pool fills remainder, deduped vs windowed; rel_cutoff per-pool only.
- MCP `daemon:recall` — same fusion, exclude_kinds=() (hook excludes diary+task), optional context=bool for ±1 turns, `when` relative-time field. since/until params (Melbourne YYYY-MM-DD, converted via `timecue:melb_day_range`); empty query + window → window digests instead of fusion.

## 4. Storage & retrieval

### 4.1 schema (storage.py, v19)
- Migrations `storage:init_db` _migrate_to_v2…v19 idempotent, PRAGMA user_version guarded; v5/v7/v8/v9 are empty sentinels; v18 = tl_hidden on session_digests + diary; v19 = stickers C2 schema (path/sha256/phash/desc/source/last_used).
- Connection: journal_mode=DELETE (deliberate — DECISIONS.md, APFS SIGBUS; never WAL) · busy_timeout 30s · sqlite-vec loaded per conn. Rule: never open a second conn to the same DB inside a write txn.
- Tables: events (recall_count/last_recalled_at v16; never aged) · tasks (active→archived on 30d no-mention) · milestones (pinned exempt) · memes (pinned=0 + last_seen>90d → DELETE) · stickers · pit · diary (date PK, DELETE+INSERT rewrite; v17: +tl_line) · goose_bites (schema history only) · alerts · audit_log · affect (superseded_by NULL = live; affect_live view) · entities (entities_live view) · session_digests (v17: +kind/tl_line/life_lines; sid PK, date, text, ts) · md_index (block hash + tombstone_at) · memes_reject_log · atlas · 6×*_vec + *_vec_meta.

### 4.2 embedding (recall.py)
- bge-m3 ONNX CPU singleton, 1024d, CLS-pool L2-norm, max_length 512. `recall:embed_pending` iterates 6 lanes (events/memes/entities/milestones/diary/tasks), batch 50/lane, so events backlog can't starve others; diary lane sweeps orphaned vec rows (rowid reuse after DELETE+INSERT).

### 4.3 recall fusion (`recall:recall_fusion` / entry `recall:recall_with_config`)
- Events: FTS5 (phrase-quoted, BM25-normalised) ∪ vec cosine, merged by id. Weighted sum: vec .55 · bm25 .30 · recency .15 · affect .10. Recency exp(-days/30) with floors: imp 5 / override → 0.5 · imp 3-4 → 0.18 · imp ≤2 → 0.
- Anchor lanes (memes/milestones/entities): vec weight .60; diary/tasks .55; reserved slot caps so events can't starve them.
- Gates: min_score 0.35 · _VEC_ONLY_FLOOR 0.55 (cross-table vec-only adds) · _ANCHOR_VEC_FLOOR 0.50 (pre-gate, bypassed by strong-hit) · _ANCHOR_BIAS +0.10 (rows clearing floor or strong-hit) · cwd bucket bias ±0.10 (cc-lab→project, desktop/ny→daily, study→study).
- Strong-hit: full-table scan, two tiers — (name) = name/aliases/key/title needles, score floored to `_STRONG_NAME_FLOOR` 0.55; (body) = fact/value/description via `recall:_body_needles`, floored to `_STRONG_BODY_FLOOR` 0.45. Body 2-char cjk windows pass 3 filters: `_CJK_STOP_BIGRAMS` (如果/觉得) → `_CJK_FUNC_CHARS` (any of ~50 function chars in window kills it: 你说/可以/现在) → table DF < `_BODY_DF_MAX` 3. ASCII needles: whole tokens + runs inside mixed tokens ((马自达suv)→(suv)), letter-boundary matched via `recall:_needles_match` — digits transparent, (gpt) hits (gpt4画画), (nd) can't hit inside (handover). Entity force-include lives HERE, in recall.py; entity_recall.py only does mention-count bumps.
- Dormant: importance ≤2 AND age >90d excluded; FTS keyword hit revives (clears superseded_by). Adjacency dedup: same-session events with |id diff| ≤1 collapse to highest score. Double min_score gate (inner events + unified all-lanes) is intentional.
- Window (since/until UTC ISO, optional): events FTS gets SQL `timestamp >= ? AND < ?`; events vec fetches k×6 then Python-filters (KNN virtual-table WHERE unreliable); diary filtered by Melbourne-local dates; anchor lanes unaffected. `recall:fetch_window_digests` — session_digests by ts (date-column fallback), newest first, 150ch/row.

## 5. Surface (DB ↔ md)

### 5.1 dashboard (`dashboard:write_dashboard`)
- Flow: 4 reconcile passes (milestone_cands, tasks, affect, alerts — each fail-soft + warn alert) → `top_sections:iter_top_blocks` render (Alerts→Tasks→Timeline→Affect→Content; milestone-cand block retired 06/11, write path kept) → `dashboard:_resolve_blocks` per-block: RECONCILED_BLOCK_IDS always overwrite (reconcile absorbed edits) · pure-display blocks hash-skip if user-edited · tombstoned omit → atomic write → md_index hashes recorded after write.
- Tasks bucketing: today / next7 / later / no_date, 6AM Melbourne boundary. Affect: last batch + 24h + 7d windows, V/A split-tone label when std_v>0.3.

### 5.2 subpage catalog (registry `subpages:_REGISTRY`, specs `subpage_specs.py`)
- All inserter-backed unless noted; `<!-- id:N -->` anchors; DB→md unless noted.
- profile (entities, bidirectional soft-delete) · milestone (bidirectional, pinned only) · diary (block_id=date) · memes (Personal/Public) · stickers (C2 catalog, flat `stk_NNN desc` format, desc-editable) · wallet (stub, fetch=[]) · study index (children legacy read_only, hand-managed) · projects index (children read_only; KNOWN: title unsanitised in child path) · cheatsheet (read_only, disk SoT) · atlas (bidirectional, respect_tombstones=False, force_sort_consistency).
- Legacy render fns in subpages_render.py are unreachable (inserter precedes, failure does NOT fall back) — scheduled for deletion (review bloat #1). render_pit is cli-only (`cli:cmd_export_pit`).

### 5.3 sync machinery
- `md_index` — SHA-256 per (path, block_id); baseline = last auto-write; observe mode freezes baseline on user edit. Missing file in observe mode bulk-tombstones its blocks (debounced 200ms). Tombstone aging 30d.
- `watcher` — watchdog on dashboard/handover/db-pages + ~/Desktop/NY/stickers/ (non-recursive, `_StickerHandler`: 1.5s debounce, size-stability check, auto-ingest new images via `sticker_ops:ingest_sticker`, skips stk_NNN/dotfiles/_thumb); 200ms debounce for md; boot full_scan(observe=True) covers crash gap; never renders. Boot: sweep_orphans (prune rows w/ missing files + md lines) → sweep_file_orphans (re-register untracked stk_NNN, exact-phash dedup deletes file). _standardize_image: format-convert only (JPG→PNG), no resize; thumbnails 240px in _thumb/ for wx send.
- `sync_loop` — 5s tick: md newer (mtime epsilon 1s) → reconcile; DB newer (max updated_at per source table) → render. USER_ACTIVE_WINDOW 3s skips render under cursor. KNOWN GAP: tick exception is log-only, no alert (plan B-9).
- `reconcile.py` — routes: milestones (bidirectional + id-anchor splice-back; bare-text line or unanchored single-bracket Me row → insert w/ date=today Melb + canonical line write-back) · milestone_candidates (✅pin/❌tombstone/✏️edit + trail diff) · tasks (trail marker, tick/untick/archive/insert, cosine dedup; [tag] needs no trailing space — CJK-friendly) · affect (aff:id segments + pending id:affect.N; delete window mtime-7d; aff-rendered id-set diff → removed id marks row superseded) · alerts (md delete = resolve; zero-anchor block no-op guard; mtime gate) · timeline (tl_line edits + `+ ` manual add + trail-diff delete, §3). reconcile_memes/profile/diary/etc live in reconcile_inserter.py (reconcile.py shims are back-compat only) — UPDATE/DELETE + unanchored-INSERT pass (memes Personal/Public section→type, profile section→kind, diary new `#### date` block; anchor write-back, natural-key dedup). Conflicts → `reconcile:emit_conflict_alerts` at dashboard + subpage call sites — add_alert(warn, reconcile_conflict), fingerprint=conflict text.
- `drift_sweep` — Trigger A same-root move (immediate) · B cross-root delete+create matched by basename+size within 30s batch window, pending TTL 1800s · dangling delete warn. Refs via rg (timeout 30s, 10MB cap, Python fallback); safe exts auto-apply with info alert; unsafe → pending JSON + `mw drift apply <pid>`. AUTHORIZED_ROOTS ×5 = atlas seed roots.
- `atlas` — seed (INSERT OR IGNORE per root) → `atlas:atlas_sweep_fs` depth-walk stubs/deletes → `atlas:reconcile_atlas` md headings back to DB; retract logic drops stub-only rows outside seed coverage; out-of-root purge guard. Canonical render ~/Desktop/NY/db-pages/atlas.md only.

### 5.4 write arbitration
- Dashboard writers: watcher (observe-only) · sync_loop (timed) · sessionend-tail (one-shot). Both renderers run reconcile first; a race = two atomic writes, second wins, nothing lost. sync_loop guards USER_ACTIVE_WINDOW; sessionend-tail doesn't (session over). flock on every md write.

## 7. Scheduled jobs (launchd, 7 plists)

- com.marrow.watcher — persistent, KeepAlive.
- com.marrow.dashboard-tick 06:01 daily — force dashboard render.
- com.marrow.daily-routine 07:00 daily — candidates + diary for yesterday.
- com.marrow.daily-catchup 19:00 daily — backfill ≤3 missing diary days in 7d window.
- com.marrow.db-backup 03:00 daily — VACUUM INTO local + iCloud offsite, keep 14 each.
- com.marrow.aging Sun 12:00 weekly — 7 cleanup passes (§10).
- MCP daemon has no plist (CC-spawned).

## 8. Alerts

- `repo:add_alert(severity, type, fingerprint, message=, db=)` — dedup key (type, fingerprint, resolved=0); repeats bump hit_count/updated_at/message. Never raises: any DB failure appends the record to DATA_DIR/alerts-fallback.jsonl + stderr note, returns -1; drained at catchup boot (truncate-then-replay). resolve = acknowledge: recurrence re-inserts (anti-mute, by design). Surface: dashboard ## Alerts (`top_sections:render_alerts`, resolved=0) ; resolve via md-delete (reconcile_alerts) or `mw resolve <id>`; aging auto-resolves milestone_added >7d only.
- Current contract + full call-site/falsing audit + fixes: docs/plans/0611-alert-redesign.md. Batch A landed 06/11 (P5 unpark, digest-zero retry chain, fallback sink, aging finally-flush; two-strike chain proven by tests). Remaining gaps (Batch B/C): 3 hooks.py sites use exception text as fingerprint (row flood) · sync_loop tick exception log-only · reconcile_ref cross-day guessing · false-positive diet.

## 9. Catchup & self-heal

- `sessionstart_catchup:_classify` per sid (24h window, union audit_log lifecycle + events): preconditions P1 bridge_owns (TTL 12h, superseded by newer extract row) · P2 session_block=archive · P3 manual_skip · P4 end summary worktree=1/mm_minus_blocked · P5 in-flight iff start row newer than end AND no terminal row (ok/skip/fail/partial) after that start AND start age <15min (`_INFLIGHT_GRACE_SECONDS`) — terminal or stale start falls through, so fail/partial/died sids respawn (fixed 06/11, was park-forever P0-1). States: 1 ppid live→skip · 2 ok,user_count=N & grew→spawn · 3 covered→skip (skip:short_session counts as terminal ok here) · 4 end <5min→skip · 5 end ≥5min no ok→spawn · 6 start+ppid dead→spawn · 7 events only→spawn. MAX_FIRE 2/run. Alerts only on spawn failure (no predicate-based death alerts, by design).
- ppid liveness `sessionstart_catchup:_live_cc_ppids`: os.kill(pid,0) primary; ps lstart (LC_ALL=C) soft confirm.
- catchup `main` boot: `_drain_fallback_sink` replays alerts-fallback.jsonl into alerts table before classification (malformed lines dropped with stderr note).
- daily_catchup 19:00 — diary backfill cap 3/run, 7d window, 6AM cutoff.
- affect heartbeat (SessionStart) · dormant revive (§4.3) · diary vec orphan sweep (§4.2) · mm+ `hooks:_handle_mm_prefix` reset:mm_plus forces re-extraction (pre-archives live jsonl).

## 10. Aging (weekly, one txn, alerts flushed in finally)

- memes: pinned=0 + last_seen<90d → DELETE (NULL last_seen kept).
- tasks: active, 0 FTS title hits in events 30d → archived.
- milestone_added alerts: >7d → resolved (auto-confirm).
- md_index tombstones >30d → DELETE.
- ~/.claude/projects worktree shells → rmtree.
- events vec window: timestamp < now-90d (`[recall].vec_window_days`, 0=off) → DELETE vec rows; exempt recall_count>0 OR affect importance ≥3; caps abort >25% (inert <100 rows) or >10k rows (critical alerts); backup gate: newest daily backup missing/>7d → skip + warn. Recovery: embed_pending re-embeds from intact events rows (vectors are derived data). pending_alerts flushed in `main`'s finally — survives audit INSERT failure (A-4, 06/11).

## 11. Infra

- `llm:LLMClient.call(role, body, tier)` — claude CLI stream-json subprocess, OAuth, no API key. Tier cheap/mid/top → model via [tiers]. Isolation flags strip persona/MCP. 1 retry/provider; severity warn (more providers left) / critical (last); timeout 120s, SIGTERM→SIGKILL ladder; refusal: stop_reason + 22 fingerprints; cost → audit_log llm_call_cost. on_alert is caller-supplied — title.py passes none (its failures stay silent).
- `popen_detach` — mandatory 4-flag combo (DEVNULL stdin, log-fd stdout/err, start_new_session, close_fds); _lazy variant: child self-redirects on first write, silent runs leave no log file.
- backup: `backup:run` VACUUM INTO tmp → os.replace, offsite copy fail-soft (warn, local still lands); `repo:safe_backup_db` in-session copies pruned >7d.
- config: default.toml ← user config.toml deep-merge; paths.toml (paths.py) supplies fallback/extra paths (drift_pending). Key tables: [paths] [backup] [llm.*] [tiers] [embedding] [recall] [sessionend] [*_dedup] [subpages] [transcript].
- title: `title:summarize` detached per prompt, ≥2 user turns, ≤8 units, tier cheap, audit-dedup.

## 12. Addons

- daily.py pipeline (§2.3) vs day-plan CC skill (.claude/skills/day-plan) — unrelated, share the name.
- synapse-wx — own repo + MAP; talks to marrow via MARROW_BRIDGE=1 env + mw CLI + direct sqlite audit flags only.

## 13. Invariants & status

**Invariants**: flock every md write · lifecycle:end commits before popen · single merged sessionend call, fenced segment blocks · 4-flag detach · DB never trusts md free-text inside rendered blocks · journal DELETE + no second conn inside write txn · all DB timestamps UTC.

**Status**: stub = wallet, cheatsheet, profile-render(rows flow once entities populate) · shipped = stickers C2 (MCP: search/pick/ingest/update/delete/list_pending; sticker_ops.py: sha256+phash dedup, thumb gen; subpage sync live; watcher Finder auto-ingest; nudge counter wx-only 10-turn; /sticker-entry command for batch desc fill; system prompt rules in synapse-wx cc.py) · wip = study/projects child pages (legacy read_only), candidate pin/drop HTML buttons · deletable = subpages_render legacy fns (verified unreachable), sessionend_prompts parse_doing_diff cluster (dead ~90 LOC) · open bugs/gaps = review P0/P1 list (docs/notes/0611-system-review.md) until alert-redesign batches land.
