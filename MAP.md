# Marrow — MAP

> Speed-read for a new session: how each board works, without opening code. Not SoT — code wins.
> Refs are `file:function` (grep them; line numbers rot). Params inline are the live defaults (config.toml can override).
> Rewritten 2026-06-11 from per-module fact cards + adversarial verify.

## 0. Contents

§1 system map+hooks · §2 write path · §3 read path · §4 storage+recall · §5 surface sync (§5.4 CLI+MCP) · §6 cortex bridge (tl/goals/wishlist/agent_guard) · §7 scheduled jobs · §8 alerts · §9 catchup/self-heal · §10 aging · §11 infra · §12 addons · §13 invariants+status

## 1. System map

```
 CC session ── transcript.jsonl ──Stop (per turn)──▶ events (archive_events)
     ▲                                            │
     │ injected context                           ▼
 hooks (auto) / daemon (MCP) ◀────────── DB (SQLite)
                                          │  events·tasks·affect·entities·milestones·memes
                                          │  diary·digests·alerts·audit_log·atlas·md_index…
                                          │  bge-m3 · 6 vec lanes · recall fusion
                                          ▼▲ watcher + sync_loop (5s) / user md edits
                                Surface: db-pages/ (daybrief·monitor·subpages)
```

The Stop hook per-turn archive (`hooks:stop` → `repo:archive_events`) is THE
event write path. SessionEnd runs only the cortex-window-closing branch (§6).
tasks/affect/diary/session_digests tables are retained as read/legacy surfaces;
nothing writes them now (tasks via cadence rem/cal; timeline via tl MCP).

Three runtimes:
- **hooks** — one-shot per CC lifecycle event, exit after injecting/spawning.
- **watcher** — launchd persistent (KeepAlive); hosts SyncLoop(5s) + AtlasSweepLoop(60s) threads.
- **daemon** — stdio MCP, 12 action-dispatch tools (recall, atlas_lookup, event_embed, tl, sticker, sticker_admin, goal, wish, first, dim, alert, event_clear — full list §5.4), spawned by CC via .mcp.json, no plist; holds bge-m3 in memory. sticker_admin write actions call write_subpage after DB commit for immediate md sync.

### 1.2 Hooks registry (all in marrow/hooks.py)

- SessionStart `hooks:session_start` — injects `timeline:render_timeline` (see §3) + static hint line (dims via mcp `dim`, goals via `goal`). Hardcap 6000 chars. Does NOT inject tasks/alerts. Drains alerts-fallback.jsonl into the alerts table (truncate-then-replay, `_drain_fallback_sink`, D8). Writes lifecycle:start marker (ppid+started_at); resume detection reads it.
- SessionEnd `hooks:session_end` — cortex-window-closing branch only (§6.3 page-turn). No archive/popen/spawn (events land per-turn via Stop hook).
- UserPromptSubmit `hooks:user_prompt_submit` — recall fusion injection (params §3). Per-session recall_seen dedup state under DATA_DIR/state/recall_seen/<sid>.json (wiped at start+end). Second registered hook same event: `hooks:turn_inject` — time+delta-since-last-reply stamp (skipped when MARROW_CHANNEL=wx, bridge injects its own), schedule.check_and_inject, and the config-first per-turn care directive (`[turn_inject].care_text`).
- PreToolUse `hooks:pretool_use` — Write/Bash placement ops get atlas ancestor-chain guidance (desc + naming_hint); others get a literal path reminder. Matcher=Agent → `hooks:agent_guard` — denies any Agent dispatch whose subagent_type is in `[agent_guard].deny` (default `["general-purpose"]`, burst-recursion protection); exit 2 + stderr reason, fail-soft (any error → exit 0). Backup guard, stateless two-tier, fail-open (07-07 v3): silent (tmp/scratchpad/worktree rm/mv/sed, any command chaining a backup action cp/rsync/tar/git commit/git stash push/.backup, all git ops) · reminder additionalContext EVERY matching call (no dedup/state): non-recursive rm on non-whitelisted path, bulk mv/sed -i wildcard→non-whitelisted dest, unwhered DELETE FROM not on a .db, destructive MCP actions (event_clear/db_clear/sticker delete/mcp__marrow__ clear|delete) · deny `permissionDecision:"deny"` (recursive rm on non-whitelisted path, rm of a *.db outside whitelist, sqlite3 *.db DROP TABLE/TRUNCATE/unwhered DELETE) unless the SAME command carries a backup action; `backup_guard_intercept=false` downgrades deny→reminder. Write/Edit no longer guarded (write needs a prior read → recoverable). Git force-push guard (`hooks:_git_force_push_guard`, config `[hooks].git_force_push_guard`) — hard `deny`, tokenized per shell segment (git push --force/-f/--force-with-lease), no escape hatch, no worktree exemption, runs FIRST. Git revert-type guard (`hooks:_git_revert_guard`, config `[hooks].git_revert_*`) — PreToolUse `permissionDecision:"ask"` held for authorship verify (reset --hard/checkout -- path/restore[non --staged]/clean -f/branch -D/stash drop|clear/revert --no-edit/switch --discard-changes/worktree remove), message 🤡; exempts worktree/agent cleanup (silent). Pretool order: rm→trash rewrite → force-push deny → git ask → backup deny → reminder. rm→trash auto-rewrite (`hooks:_rm_to_trash_rewrite`, config `[hooks].rm_to_trash`/`trash_paths`, default on): a Bash `rm` segment whose positional paths ALL resolve under a `trash_paths` prefix (~ expanded, relative joined onto cwd) is rewritten to `/usr/bin/trash <shlex-quoted paths>` (rm flags dropped) BEFORE all guards, via `hookSpecificOutput.updatedInput.command` + an `additionalContext` line; mixed/zero-positional/wildcard/out-of-zone segments untouched and reclassified normally; separators preserved byte-identical; fail-open.
- MARROW_CORTEX (cortex session, §6) gets full memory parity: lifecycle rows, sessions row, recall/timeline injection, per-turn archive same as any session. Env var = identity marker only (e.g. B8 immunity); channel = `MARROW_CHANNEL=ct` (set alongside it, `llm.py:_run_claude_cortex`).
- `[kickout]` (B8, `hooks:turn_inject`) — config-first nudge: cli 21:30-22:00 wind-down + 22:00-06:00 leave-desk (channel=cli), wx/tg 23:00-06:00 quiet. Skipped when MARROW_CORTEX=1.

### 1.3 install.py hook registration
- `install.py:register_hooks` — idempotent: for each event, strips every existing `marrow.hooks` command (and legacy-absorbed commands, `_ABSORBED`) from every matcher group ONCE before re-adding, then prunes emptied groups. Fixes the prior double-registration bug (stripping per-entry inside the add loop would wipe an already-added sibling entry sharing the same matcher, e.g. UserPromptSubmit's two hooks).

## 2. Write path

### 2.1 session capture (Stop hook, THE write path)
- `hooks:stop` per turn → `transcript:clean` → `repo:archive_events`. Clean = code-only strip: tool calls, thinking, sidechains; headless spawns dropped via `transcript:is_headless` (worker_models prefix match + surviving prompt heads: transcript wrapper, handover, markdown-compressor). `repo:archive_events` idempotent by source_hash, bumps entity mention_count + memes use_count in the same txn.

### 2.2 tl_add/tl_update self-authored timeline (tl_writer.py)
- One MCP call → one `events` row, `role='tl'`, `channel`=platform (MARROW_CHANNEL env, default cli). Affect phrase lives verbatim in content, importance in `events.imp`.
- Format: `HH:mm[-HH:mm] 【user_word·i | assistant_word·i】body` — user_word/assistant_word ≤8 chars each, body ≤50 chars (config: `tl.body_max`), i = per-side intensity 1-5 (`n_intensity`/`y_intensity`), `importance` param = row-level composite (default max of the two sides). At least one of user_word/assistant_word required.
- `tl_add`/`tl_update` allowed under `MARROW_CORTEX=1` — cortex writes timeline like any channel, `channel=ct`.
- `tl_update` only accepts `role='tl'` event ids; only passed fields change (label/body/timerange/imp independently updatable). No dashboard-line sync (dashboard retired); daybrief/monitor re-render picks up the DB change.
- v29 migration (`storage:_migrate_to_v29`) backfilled prior `channel='self'` rows to `role='tl'`, folding affect-table label into content as `【label】body`; channel is now always a real platform value.

## 3. Read path (what gets injected)

- SessionStart: `## Timeline` 2-zone view (`timeline:render_timeline`, cap 20 lines, natural-midnight day boundary): Zone A = last-24h-from-midnight HH:MM film-strip, life_lines read, `--- MM-DD ---` day dividers, cap 20. Zone B = 3 diary days before zone A start, `**MM-DD Day 【tone】**` + overview from diary.tone/overview; NULL overview days skipped. No affect-episode zone, no Week-trend footer. Manual notes: `+ [HH:MM] text` → events channel='manual'; line+anchor delete → tl_hidden=1 or manual-event hard-DELETE + vec cleanup, via `<!-- tl-rendered:s=..;d=..;e=..;ep=.. -->` trail diff (post budget-trim). Reconcile does not write tl_line back; present anchors count as unchanged, hidden sweep preserved.
- UserPromptSubmit: recall fusion hits as passive context. Render shaping in `hooks:user_prompt_submit`: budget 800 chars · rank_caps [300,120,120,40,40] · rel_cutoff 0.6×top1 · only rank-1 event hit gets ±1 context turns (`recall:fetch_event_context`) · per-kind head via `hooks:_recall_head` (event `[chan reltime] ev#id` using `timeutil:reltime_short`; memes `[MM-DD|YYYY] me#id`; milestone `[date] ms#id` T00:00-stripped; entity `en#id` no time; diary/task `d#/t#` keep `format_recall_ts`) — same head reused by the recall log (`hooks:_append_recall_log`) · recall_seen dedup per session · post-injection `recall:bump_recall_counts` (best-effort).
- Time-lane (passive): `timecue:parse_time_cue` on prompt (昨天/前天/上周X/N天前/X月X号/EN equivalents → Melbourne natural-day → UTC window; future cues → None). Cue + substantive stripped text → windowed fusion takes TOP slots (budget min([recall].timelane_budget 400, budget/2)); stripped trivial → `recall:fetch_window_digests` lines `[MM-DD Day · digest]`, seen-key ("digest", sid). Semantic pool fills remainder, deduped vs windowed; rel_cutoff per-pool only.
- MCP `daemon:recall` — same fusion, exclude_kinds=() (hook excludes diary+task), optional context=bool for ±1 turns, `when` relative-time field. since/until params (Melbourne YYYY-MM-DD, converted via `timecue:melb_day_range`); empty query + window → window digests instead of fusion.
- Source tag: `recall.py:recall_fusion` sets `c["source_tag"] = "tl" if role=='tl' else "event"` per candidate; daemon output prefixes hits with `[tl]`/`[event]`. Hook no longer uses source_tag — replaced by per-kind `_recall_head` (07-06). Same-sid dedup not implemented — deferred, observe 2 weeks (07-03 pm).
- tl_add nudge: `hooks:user_prompt_submit` appends a hint every `[tl_nudge].threshold` user prompts without a tl_add — per-sid state counter (`state/tl_nudge/<sid>`, +1 per prompt, zeroed on fire or tl_add), text from `marrow/data/tl_nudge.txt`; `/tl-` (runs `mw tl-silence` CLI, no longer an MCP tool) mutes it per-sid, state dies with the session.

## 4. Storage & retrieval

### 4.1 schema (storage.py, v34)
- Migrations `storage:init_db` _migrate_to_v2…v34 idempotent, PRAGMA user_version guarded; v5/v7/v8/v9 are empty sentinels; v18 = tl_hidden on session_digests + diary; v19 = stickers C2 schema (path/sha256/phash/desc/source/last_used); v25 = diary tone/overview + session_digests.updated_at; v27 = entities/memes/affect updated_at; v29 = events +imp INTEGER (recall boost/retire/milestone SQL) +flag TEXT (cortex management marks, open vocab), backfills channel='self' rows to role='tl' (§2.2); v30 = goals table (key PK, value, unit, updated_at — latest value only, no history), set/list/delete via `goal` MCP action param (§6); v31 = ct_rate_limit table (kv snapshot of latest rate_limit_event stream frame, cortex bulletin reads it); v32 = ct_first_tick table (item PK, seen_at, sid, note — `first` MCP tick/untick/list, §3); v33 = drop memes.context (memes reduced to key/value, matches entities; rebuilds memes_fts); v34 = ct_first_tick +status TEXT default 'done' (tick action status=done|tried). A wishes table (append-only DB mirror of wishlist.md) shipped and was reverted same day (07-05) — wish stays md-only, no table.
- Connection: journal_mode=DELETE (deliberate — DECISIONS.md, APFS SIGBUS; never WAL) · busy_timeout 30s · sqlite-vec loaded per conn. Rule: never open a second conn to the same DB inside a write txn.
- Tables: events (recall_count/last_recalled_at v16; never aged) · tasks (active→archived on 30d no-mention) · milestones (pinned exempt) · memes (permanent, no aging; v27: +updated_at; v33: -context) · stickers · pit · diary (date PK, DELETE+INSERT rewrite; v17: +tl_line; v25: +tone +overview) · goose_bites (schema history only) · alerts · audit_log · affect (superseded_by NULL = live; affect_live view; v27: +updated_at) · entities (entities_live view; v27: +updated_at) · session_digests (v17: +kind/tl_line/life_lines; sid PK, date, text, ts; v26: +updated_at) · md_index (block hash + tombstone_at) · memes_reject_log · atlas · goals (v30) · ct_rate_limit (v31) · ct_first_tick (v32; +status v34) · 6×*_vec + *_vec_meta.

### 4.2 embedding (recall.py)
- bge-m3 ONNX CPU singleton, 1024d, CLS-pool L2-norm, max_length 512. `recall:embed_pending` iterates 6 lanes (events/memes/entities/milestones/diary/tasks), batch 50/lane, so events backlog can't starve others; diary lane sweeps orphaned vec rows (rowid reuse after DELETE+INSERT).

### 4.3 recall fusion (`recall:recall_fusion` / entry `recall:recall_with_config`)
- Events: FTS5 (phrase-quoted, BM25-normalised) ∪ vec cosine, merged by id. Weighted sum: vec .55 · bm25 .30 · recency .15 · affect .10. Recency exp(-days/30) with floors: imp 5 / override → 0.5 · imp 3-4 → 0.18 · imp ≤2 → 0.
- tl imp boost (A2r, staged additive, `[recall].imp_boost` = [0.0, 0.0, 0.0, 0.02, 0.035, 0.05] indexed by events.imp 0-5): applies on top of the weighted sum for every candidate carrying `events.imp` regardless of role — imp 1-2 sit level with plain events (0 boost), imp 5 = milestone-tier (+0.05 cap). `recall.py:_imp_boost_table`/`_imp_boost`.
- Anchor lanes (memes/milestones/entities): vec weight .60; diary/tasks .55; reserved slot caps so events can't starve them.
- Gates: min_score 0.35 · _VEC_ONLY_FLOOR 0.55 (cross-table vec-only adds) · _ANCHOR_VEC_FLOOR 0.50 (pre-gate, bypassed by strong-hit) · _ANCHOR_BIAS +0.10 (rows clearing floor or strong-hit) · cwd bucket bias ±0.10 (cc-lab→project, desktop/ny→daily, study→study).
- Strong-hit: full-table scan, two tiers — (name) = name/aliases/key/title needles, score floored to `_STRONG_NAME_FLOOR` 0.55; (body) = fact/value/description via `recall:_body_needles`, floored to `_STRONG_BODY_FLOOR` 0.45. Body 2-char cjk windows pass 3 filters: `_CJK_STOP_BIGRAMS` (如果/觉得) → `_CJK_FUNC_CHARS` (any of ~50 function chars in window kills it: 你说/可以/现在) → table DF < `_BODY_DF_MAX` 3. ASCII needles: whole tokens + runs inside mixed tokens ((马自达suv)→(suv)), letter-boundary matched via `recall:_needles_match` — digits transparent, (gpt) hits (gpt4画画), (nd) can't hit inside (handover). Entity force-include lives HERE, in recall.py; entity_recall.py only does mention-count bumps.
- Dormant: importance ≤2 AND age >90d excluded; FTS keyword hit revives (clears superseded_by). Adjacency dedup: same-session events with |id diff| ≤1 collapse to highest score. Double min_score gate (inner events + unified all-lanes) is intentional.
- Window (since/until UTC ISO, optional): events FTS gets SQL `timestamp >= ? AND < ?`; events vec fetches k×6 then Python-filters (KNN virtual-table WHERE unreliable); diary filtered by Melbourne-local dates; anchor lanes unaffected. `recall:fetch_window_digests` — session_digests by ts (date-column fallback), newest first, 150ch/row.

## 5. Surface (DB ↔ md)

### 5.1 daybrief + monitor (the DB→md surfaces)
- `daybrief:update` — single owner of `daybrief.md`. Reconciles md hand-edits BEFORE render (reconcile_timeline), then composes the same render fns the SessionStart hook injects (usage.sessionstart_lines, schedule.render_daily, timeline.render_timeline) into bounded zones; atomic write. Timeline zone carries the `<!-- id:... -->` block anchor for md_index tracking.
- `monitor:render` — `monitor.md` alert surface (§8). reconcile_alerts (md-delete=resolve) runs BEFORE render; one-way DB→md, no ping-pong. Path from `[paths].monitor` (empty = `<db_pages>/monitor.md`).

### 5.2 subpage catalog (registry `subpages:_REGISTRY`, specs `subpage_specs.py`)
- All inserter-backed unless noted; `<!-- id:N -->` anchors; DB→md unless noted.
- profile (entities, bidirectional soft-delete) · milestone (bidirectional, pinned only) · diary (block_id=date) · memes (Personal/Public) · stickers (C2 catalog, flat `stk_NNN desc` format, desc-editable) · wallet (stub, fetch=[]) · study index (children legacy read_only, hand-managed) · projects index (children read_only; KNOWN: title unsanitised in child path) · cheatsheet (read_only, disk SoT) · atlas (bidirectional, respect_tombstones=False, force_sort_consistency).
- Legacy render fns in subpages_render.py are unreachable (inserter precedes, failure does NOT fall back) — scheduled for deletion (review bloat #1). render_pit is cli-only (`cli:cmd_export_pit`).

### 5.3 sync machinery
- `md_index` — SHA-256 per (path, block_id); baseline = last auto-write; observe mode freezes baseline on user edit. Missing file in observe mode bulk-tombstones its blocks (debounced 200ms). Tombstone aging 30d.
- `watcher` — watchdog on handover/db-pages + ~/Desktop/NY/stickers/ (non-recursive, `_StickerHandler`: 1.5s debounce, size-stability check, auto-ingest new images via `sticker_ops:ingest_sticker`, skips stk_NNN/dotfiles/_thumb); 200ms debounce for md; boot full_scan(observe=True) covers crash gap; never renders. Boot: sweep_orphans (prune rows w/ missing files + md lines) → sweep_file_orphans (re-register untracked stk_NNN, exact-phash dedup deletes file). _standardize_image: format-convert only (JPG→PNG), no resize; thumbnails 240px in _thumb/ for wx send.
- `sync_loop` — 5s tick, one SyncTarget per subpage + daybrief.md + monitor.md: md newer (mtime epsilon 1s) → reconcile; DB newer (max db timestamp per target's sources) → render. daybrief db_mtime = timeline-only sources; render calls daybrief.update (which reconciles internally, so the loop skips its own reconcile). monitor db_mtime = alerts max(updated_at). Re-check md mtime after reconcile; if advanced, skip render this tick. USER_ACTIVE_WINDOW 3s skips render under cursor. 3-consecutive tick failure per target → warn sync_loop_tick_failed:{target} alert, counter resets after alert.
- `reconcile.py` — surviving routes: reconcile_timeline (daybrief.md — life_lines per-line anchor + write-back with mtime gate; diary.overview + diary.tone write-back; `+ ` manual add; trail-diff delete with per-row mtime gate on 4 anchor paths sid/date/evt/ep, §3) · reconcile_alerts (monitor.md — md delete = resolve; zero-anchor block no-op guard; mtime gate) · reconcile_milestones (milestone subpage — bidirectional + id-anchor splice-back; bare-text/unanchored single-bracket Me row → insert w/ date=today Melb + canonical line write-back). reconcile_memes/profile/diary/etc live in reconcile_inserter.py (reconcile.py shims are back-compat only) — UPDATE/DELETE + unanchored-INSERT pass (memes Personal/Public section→type, profile section→kind, diary new `#### date` block; anchor write-back, natural-key dedup). UPDATE pass bidirectional: table has updated_at and row.updated_at > md_mtime → DB-wins (patch md line via spec.render_row + atomic_write); else md-wins. Atlas: mtime gate on UPDATE/DELETE in reconcile_atlas. Conflicts → `reconcile:emit_conflict_alerts` (add_alert warn reconcile_conflict, fingerprint=conflict text).
- `drift_sweep` — Trigger A same-root move (immediate) · B cross-root delete+create matched by basename+size within 30s batch window, pending TTL 1800s · dangling delete warn. Refs via rg (timeout 30s, 10MB cap, Python fallback); safe exts auto-apply with info alert; unsafe → pending JSON + `mw drift apply <pid>`. AUTHORIZED_ROOTS ×5 = atlas seed roots.
- `atlas` — seed (INSERT OR IGNORE per root) → `atlas:atlas_sweep_fs` depth-walk stubs/deletes → `atlas:reconcile_atlas` md headings back to DB; retract logic drops stub-only rows outside seed coverage; out-of-root purge guard. Canonical render ~/Desktop/NY/db-pages/atlas.md only.

### 5.4 mw CLI + MCP tools (`cli.py` entry `~/.local/bin/mw`, `daemon.py` MCP)
- CLI: mutation (set/rm/pin/add-alert/alerts-clear/tl-silence, no refresh) · `resolve <id>` (only mutation w/ auto-refresh) · session mgmt · display (show/ls/atlas/doctor) · system (refresh/drift/watcher/install). `mw refresh` = daybrief + monitor + subpages. Command hints for AI live in MCP tool descriptions (`daemon.py`).
- MCP tools (`daemon.py`), 12 total, action-dispatch (one tool + `action` param, replaces old per-verb tool naming, 07-05/06): recall · atlas_lookup · event_embed (fn `embed_pending`) · tl (add/update/clear) · sticker (search/pick) · sticker_admin (ingest/update/delete/pending) · goal (set/list/delete) · wish (append-only, `text`+optional `section`/`due` params, no action) · first (tick/untick/list, status=done|tried) · dim (upsert/query/delete; kind=person/pref/place/meme/milestone) · alert (list/resolve) · event_clear (was db_clear — events/FTS/vectors only). tl/goal/wish/first tools detailed in §6.

### 5.5 write arbitration
- Surface writers: watcher (observe-only) · sync_loop (timed) · `mw refresh` (manual). Renderers run reconcile first; a race = two atomic writes, second wins, nothing lost. sync_loop guards USER_ACTIVE_WINDOW. flock on every md write.

## 6. Cortex bridge (C3)

- Purpose: marrow is cortex's LLM runner (own repo/venv, see cortex/MAP.md §1) and its data-write surface (goals, wishlist, first-tick) + timeline surface (tl_add) shared with chat sessions. All of it (organs) now lives in one module, `marrow/cortex_bridge.py` — a verbatim extraction from daemon.py/hooks.py/llm.py, names/logic/behaviour unchanged. This is the detail home; cortex/MAP.md's "Marrow-side organs" section is a cross-repo index pointing back here.

### 6.1 Two gates
- `[cortex].enabled` (config, default false) — "are the organs installed at all". `enabled()` reads it live. False = `register()` no-ops (zero tools reach the MCP schema) and every hook call site below short-circuits to inert. Clean install shows zero cortex behaviour.
- `MARROW_CORTEX` (env) — "is this session the cortex session". Set at origin by `cortex_bridge.call_cortex`/`run_claude_cortex` (formerly llm.py) on the spawned subprocess. `is_cortex_session()` reads it live (used by hook call sites); `_CORTEX` is an import-time capture of the same var, used only by `register()` to gate lie_down/wait/say tool registration (module-load-time decision, matches the original daemon._CORTEX behaviour).
- Combined: enabled=true + no env → wish/first/goal register for all sessions, lie_down/wait/say hidden. + env → all six visible, hook branches active (page-turn, lie_down deny, 亮牌).

### 6.2 Six MCP tools, registered via `register(marrow_tool, db)`
- `wish(text, section=None, due=None)` — line `[] YY/MM/DD text [due]` (date format via `[cortex].wish_date_format`, default `%y/%m/%d`). `section` = heading substring (## or ###) → insert at that section's end; omit = append at end of file. Flock-guarded atomic write into `[cortex].wishlist_path` (default `<home>/wishlist.md`). Her hand edits in the md are never touched — one-way DB-writer → md, no reconcile, no DB table.
- `first(action, item, note, sid, status)` — action=tick/untick/list, status=done|tried. Main session's response to the Cortex First section (nudges injected into context, §3). 'tick' upserts `ct_first_tick` (v32; +status v34) so other sessions/later wakes stop repeat-nagging; 'untick' clears a wrong ack.
- `goal(action, key, value, unit)` — action=set/list/delete. Key/value/unit upsert into `goals` (v30), no history. Any session calls `goal(action='set', ...)` the moment she states/changes a goal ("sleep goal 8h") — next cortex tick reads it straight from DB.
- `lie_down(next_wake_min, rotate)` / `wait(minutes)` / `say()` — cortex-only (registered only under `_CORTEX`). `next_wake_min` is now a REQUIRED positional arg (no default, no session-facing dice) — always threaded as `--next-wake-min <N>` into `cortex.lie_down`; cortex clamps it to [1, wake.next_wake_max] (see cortex/MAP.md §5). Each shells `_run_cortex_module` → `[cortex].venv_python -m cortex.<mod>` with `cwd=[cortex].repo_root` (30s timeout, stderr surfaced on failure; either empty config key → "not configured" error). Replaced the old /lie-down + /say slash commands (07-08).
- `register()` is idempotent per process (FastMCP tolerates re-adding a tool name); `_DB` is set from the caller's own DB path, patchable by tests.

### 6.3 Hook call sites (hooks.py, all gated `cortex_bridge.enabled()` unless noted)
- SessionStart (hooks.py) — fresh cortex window only (`enabled() and MARROW_CORTEX and not is_resume`) → `_cortex_handoff_page_turn_if_stale()`: stale (before-today) L1 date on handoff.md triggers archive + fresh dated template copy. Content itself is no longer injected here — cortex's own CLAUDE.md `@handoff.md` import is the read path.
- PreToolUse lie_down deny (hooks.py) — `_cortex_lie_down_deny(inp)`: denies `mcp__marrow__lie_down` until the handoff is written this window, but only when the call wants rotate OR window occupancy is at `[cortex].force_tokens` (150k default fuse line); a plain lie_down under the line always passes. Cortex-session-only inside the function (checks MARROW_CORTEX itself).
- kickout immunity B8 (hooks.py) — `cortex_bridge.is_cortex_session()`, env-only by design (no `enabled()` gate — cortex identity, not organ-install state) → cortex window skips the anti-late-night nudge entirely (own bulletin/schedule).
- turn_inject 100k 亮牌 (hooks.py) — `_cortex_show_context(tpath)`, gated `enabled()`: cortex-only (checks MARROW_CORTEX itself) window-occupancy nudge at `[cortex_rotate].show_tokens` (100k soft, ahead of the 150k fuse), text from `[cortex_rotate].show_text`.
- Tuck-in note skip (UserPromptSubmit, hooks.py, BEFORE the wake-marker branch) — a prompt carrying `[cortex].tuck_in_marker` already carries its diff-mode note inline (cortex D6), so the hook returns early with NO full-note turn-inject (07-14 double-note fix). It is a machine line, never a wake bell, so it also falls through the user-wake reset gate below (no gen bump).
- User-wake reset (UserPromptSubmit, hooks.py:1952-1959) — `cortex_bridge._cortex_user_wake_reset(inp)`, fired on every cortex-window prompt that is NOT a machine line (`is_machine_line`, cortex_bridge.py:496-511: excludes the wake marker, monitor-death notification, and the `[cortex].tuck_in_marker` TUCK-IN line arriving down the ear channel — only a real user message triggers the reset). Checks `MARROW_CORTEX` itself (no `enabled()` gate needed at the call site since it self-guards). Body (`_cortex_user_wake_reset`, cortex_bridge.py:678-708): under `_wake_state_lock` (byte-compatible with cortex's own wake_state flock+atomic-replace protocol — marrow's venv can't import cortex, so this manipulates wake_state.json directly) flips `awake=true` if not already, stamps `user_replied_this_wake=true`, zeros `wait_count`, drops `silence_wait_until`/`tuck_pending`, pops the recorded `sentinel_pid`; then outside the lock: `_clear_floor_deadline()` (nulls `next_floor_due_at` on ct_pacemaker_state — safe because the awake gate blocks any signal while awake and the reset's own awake flip means the next lie_down redraws before None could fire) + `_kill_pid(sentinel_pid)` (SIGTERM the sentinel) + `_spawn_watchdog_if_absent()` (respawns `cortex.watchdog` via the cortex venv/repo if the pidfile is missing/dead). Idempotent: already-awake + watchdog-alive collapses to cheap no-op writes.
- `_wake_state_lock` (cortex_bridge.py:519-565) — byte-compatible flock+atomic-replace on `<wake_state>.lock`, COUPLED with cortex's `wake_state.lock_path` (cortex/MAP.md §4): both sides resolve the lock base independently from their own config ([cortex].wake_state_file/[cortex].home here vs [paths].wake_state_file/[paths].cortex_home there) — overriding one without the other silently splits the lock file (lost update).

### 6.4 llm.py delegate (cross-repo contract)
- `LLMClient.call_cortex` (llm.py) — thin delegate, kept as a stable entry point: `~/CC-Lab/cortex/cortex/wake.py` spawns marrow's venv python and calls `LLMClient().call_cortex(...)` by this exact name/signature; changing it breaks cortex. Forwards straight into `cortex_bridge.call_cortex`, which builds cwd/tier/model/effort from `[cortex]` config and calls `run_claude_cortex` — full-environment resumed session, NO isolation flags (persona/rules/MCP/agents load like a real session). Always sets `MARROW_CORTEX=1` + `MARROW_CHANNEL=ct` env; `--permission-mode bypassPermissions` (headless pipe, nobody to approve tool prompts) + `--resume <sid>` when resuming. `timeout` param overrides `[llm.claude_cli_cortex].timeout_s` (600s default) so cortex's own config stays the single source of truth for the call budget (cortex derives its outer subprocess kill from the same number, see cortex/MAP.md §4). Single attempt, no chain/retry — caller (cortex pacemaker) owns retry policy. `max_tokens` caps per-wake CURRENT WINDOW SIZE (deduped mid-stream by request_id); breach → subprocess killed, `capped=True` + `total_tokens` returned, `_log_cortex_cap` audits it. Returns `{"text", "session_id"}` (+ capped/total_tokens when a cap is active).
- `_cortex_stream_timer` — env-driven (`CORTEX_WAKE_TIMING_LOG`) per-stream-event timing probe, best-effort, no-op unless cortex requests it.

### 6.5 Not in the bridge (deliberately shared/outside)
- `storage.py` migrations — cortex-read DB surfaces that stay in the shared schema module: v29 (`events.imp`/`events.flag`, self-authored recall boost + cortex management marks), v30 (`goals` table), v31 (`ct_rate_limit` kv), v32 (`ct_first_tick` table; v34 adds its `.status` column).
- `_window_tokens_from_transcript` stays in hooks.py — shared with the all-session usage-threshold inject, not cortex-specific.
- Config sections `[cortex]` / `[cortex_rotate]` / `[cortex_usage]` / `[llm.claude_cli_cortex]` (marrow/config.default.toml) — all cortex knobs live here, none hardcoded in cortex_bridge.py.
- `deploy/commands/ct-clear.md` — slash command wrapping `lie_down(rotate=True)`: summarise the session into handoff.md, then rotate.
- agent_guard (§1.2) lives in the main hooks pipeline rather than a cortex-side hook because hooks only execute in the main session's settings.json, and cortex's resumed session shares the same global settings.

## 7. Scheduled jobs (launchd)

- com.marrow.watcher — persistent, KeepAlive.
- com.marrow.refresh — periodic `mw refresh --all` (daybrief + monitor + subpages).
- com.marrow.db-backup 03:00 daily — VACUUM INTO local + iCloud offsite, keep 14 each.
- com.marrow.jsonl-cleanup — prune stale transcript scratch/logs.
- com.marrow.aging Sun 12:00 weekly — cleanup passes (§10).
- install.py `_ALL_PLISTS` provisions aging/db-backup/watcher; `_OBSOLETE_PLISTS` boots out + deletes retired dashboard-tick/daily-routine/daily-catchup on upgrade.
- MCP daemon has no plist (CC-spawned).

## 8. Alerts

- `repo:add_alert(severity, type, fingerprint, message=, db=)` — dedup key (type, fingerprint, resolved=0); repeats bump hit_count/updated_at/message. Never raises: any DB failure appends the record to DATA_DIR/alerts-fallback.jsonl + stderr note, returns -1; drained by the session_start hook (truncate-then-replay, §9). resolve = acknowledge: recurrence re-inserts (anti-mute, by design). Surface: monitor.md ## Alerts (`monitor:render_alerts`, resolved=0); resolve via md-delete (reconcile_alerts) or `mw resolve <id>`; aging auto-resolves milestone_added >7d only.
- `schedule:_log_fail` — cadence subprocess fails append to DATA_DIR/logs/cadence_fail.log; streak of 3 → one warn alert, message triaged by `schedule:_alert_message` (auth → restart-watcher-first checklist; TCC grants = per-process snapshot at start · timeout → not-auth · other → first err line). Streak resets on success.
- Current contract + full call-site/falsing audit + fixes: see alert redesign archive. Batch A landed 06/11 (P5 unpark, digest-zero retry chain, fallback sink, aging finally-flush). Batch B/C landed 06/15 (stable fingerprints · reconcile_ref date-scoped · sync_loop 3-consecutive alert · watcher thread-start critical · stub diary unblock · overflow auto-resolve · offsite 30s retry · dangling path-absent gate). Remaining: wx death escalation + wx media failure alerts (synapse-wx side).

## 9. Catchup & self-heal

- alerts-fallback drain: the SessionStart hook runs `_drain_fallback_sink` every session start — replays DATA_DIR/alerts-fallback.jsonl into the alerts table (truncate-then-replay; malformed lines dropped with stderr note), so an alert that failed to write during a DB outage lands on the next session.
- dormant revive (§4.3) · diary vec orphan sweep (§4.2).

## 10. Aging (weekly, one txn, alerts flushed in finally)

- tasks: active, 0 FTS title hits in events 30d → archived.
- milestone_added alerts: >7d → resolved (auto-confirm).
- md_index tombstones >30d → DELETE.
- ~/.claude/projects worktree shells → rmtree.
- events vec window: timestamp < now-90d (`[recall].vec_window_days`, 0=off) → DELETE vec rows; exempt recall_count>0 OR affect importance ≥3; caps abort >25% (inert <100 rows) or >10k rows (critical alerts); backup gate: newest daily backup missing/>7d → skip + warn. Recovery: embed_pending re-embeds from intact events rows (vectors are derived data). pending_alerts flushed in `main`'s finally — survives audit INSERT failure (A-4, 06/11).

## 11. Infra

- `llm:LLMClient.call(role, body, tier)` — claude CLI stream-json subprocess, OAuth, no API key. Tier cheap/mid/top → model via [tiers]. Isolation flags strip persona/MCP. 1 retry/provider; severity warn (more providers left) / critical (last); timeout 120s, SIGTERM→SIGKILL ladder; refusal: stop_reason + 22 fingerprints; cost → audit_log llm_call_cost. on_alert is caller-supplied — title.py passes none (its failures stay silent).
- `popen_detach` — mandatory 4-flag combo (DEVNULL stdin, log-fd stdout/err, start_new_session, close_fds); _lazy variant: child self-redirects on first write, silent runs leave no log file.
- backup: `backup:run` VACUUM INTO tmp → os.replace, offsite copy fail-soft (warn, local still lands); `repo:safe_backup_db` in-session copies pruned >7d.
- config: default.toml ← user config.toml deep-merge; paths.toml (paths.py) supplies fallback/extra paths (drift_pending, ny_root, daybrief_md). Key tables: [paths] [backup] [llm.*] [tiers] [embedding] [recall] [*_dedup] [subpages] [transcript] [turn_inject] [cortex*].
- title: `title:summarize` detached per prompt (`python -m marrow.title`, isolated LLM call), ≥5 user turns, ≤8 units, tier cheap, audit-dedup.

## 12. Addons

- synapse-wx/tg — own repos + MAP; talk to marrow via MARROW_BRIDGE=1 env + mw CLI + direct sqlite audit flags only. Events land per-turn via cc's Stop hook (bridge no longer drives a marrow-side sessionend pipeline).

## 13. Invariants & status

**Invariants**: flock every md write · Stop-hook per-turn archive is idempotent by source_hash · 4-flag detach · DB never trusts md free-text inside rendered blocks · journal DELETE + no second conn inside write txn · all DB timestamps UTC.

**Status**: stub = wallet, cheatsheet, profile-render(rows flow once entities populate) · shipped = stickers C2 (MCP: sticker(search/pick) + sticker_admin(ingest/update/delete/pending); sticker_ops.py: sha256+phash dedup, thumb gen; subpage sync live; watcher Finder auto-ingest; nudge counter wx-only 10-turn; /sticker-entry command for batch desc fill; system prompt rules in synapse-wx cc.py) · wip = study/projects child pages (legacy read_only) · deletable = subpages_render legacy fns (verified unreachable) · open bugs/gaps = see system review notes until alert-redesign batches land.
