# Marrow Foundation Design

> Personal AI memory + workflow system. SQLite-backed, model-agnostic, one dashboard.
> Holds goal + structure + hard constraints + sub-page contract only. Current decisions → DECISIONS. History → PROGRESS. Unbuilt → FUTURE.

## Goals
> Always think about if goals are matched by the design.
1. Host & vendor portable — LLM provider, storage path, scheduler, notifier, backup, AND data migration all swap by config. Every phase considers this (Lumi 2026-05-21).
2. Cross-channel parity — multi-platform friendly - chat history, memory, setting, commands sync all in one; start with cli and wechat
3. Semi-permanent memory — major events permanent, emotion consistent, cold recent drops if unused.
4. Workflow + build carryover — where I left off and outcome-level build narrative survive sessions.
5. Emotional continuity — relationship and persona density transfer losslessly across sessions, platforms, and models without depending on a timeline file or model-native memory.
6. High auto, low maintenance — everything should input and update automatically - including dashboard; every surface hand-readable and editable.
7. Perfect expandable base — new capability = addon

## Outcome (what Lumi experiences)
- Opens `~/Desktop/NY/dashboard.md`, sees what's open + what broke.
- Past facts resurface on mention; cold recall fast; no context repeated.
- Never manually clears a marker, triggers catchup, or retries.
- Anything wrong corrected deterministically at a point — not a black box.
- Switch CLI ↔ WeChat mid-thought.
- Swap model/vendor by editing one config line.

## Architecture (main line)
- daemon — Python MCP server, serves CLI + WeChat clients.
- storage — SQLite + FTS5 + sqlite-vec.
- runtime — `claude` as stream-json subprocess inheriting OAuth subscription.
- bridge — local socket for WeChat permission routing (Phase 4).
- frontend — auto-rendered `dashboard.md` + static CLAUDE.md family. Memory pulled via MCP, never injected.
- supervisor — daemon watchdog; restart + alert on storm.

## Data model
- Phase 1 tables: events / tasks / milestones / memes / stickers / pit / diary / goose_bites / alerts / audit_log.
- Phase 2 tables: affect (per-episode) / entities + entity_facts / corrections / transactions (Phase 5 wallet).
- `migrate.py` imports historical md once via parsers + source_hash idempotency.
- storage.py is the schema source of truth; this lists intent only.

## Dashboard — single entry
- Top: Alerts (bug + pipeline-fail only; pipeline-fail self-clears, bug hand-cleared) · Open Threads (daily / study / project).
- Bottom: Monitor Zone — last N system writes. Auto-rendered + hand-editable, same SoT contract as all md (see Content flow).
- All sections hand-editable; auto-writer skips any block whose hash diverges from md_index baseline (= user has edited).

## Sub-pages (one table → one view, same render contract)
> Order + visibility config-driven via `[subpages]` in `~/.config/marrow/config.toml`. Two groups: top (content) + bottom (utility), rendered with `---` divider.

Top (content):
- Profile — personal facts beyond CLAUDE.md: interests, lifestyle, family & friends. Backed by entities (Phase 2).
- Milestone — life events (## Us + ## Me).
- Diary — one page per month, drill into per-day narrative.
- Memes — private inside-jokes + viral quotes + topical news/event mentions; hot memes first.
- Stickers — WeChat-sticker-style gallery, bidirectional: drop a file in → system auto-writes description + trigger from chat context; remove from md or via chat → gone.
- Wallet — opt-in addon, transactions table, bank-statement layout (see FUTURE stellan_wallet). Position reserved now; content render lands with Phase 5.
- Goose-bites — Best of the day (铁锅).

Bottom (utility):
- Study — one page per unit (progress / due / submitted). Notion stays primary, this is the CC-visible mirror.
- Projects — index of active + done + pit (deferred backlog not in Open Threads), one page per project. Pit = disk-SoT (hand-written FUTURE-style inbox, not in DB, not in recall). Per-project detail pages render-only (hand-maintained maintenance notes, not in DB but readable). A project's own sub-pages do not appear on the dashboard.
- Cheatsheet — scripts / hooks / skills / MCP / aliases / brew + directory map, rendered from disk + hand-edits (Trigger / notes / Anthropic new commands), hand-edits preserved. Entries indexed in separate `cheatsheet_entries` table for keyword-triggered force-include recall (own lane, not in events fusion).
- Atlas — dir map (path / note / write_hint / naming_hint / depth / stale). Depth-aware fs sweep stubs new dirs; reconcile preserves manual fields; stale=1 marks vanished dirs (never deleted).

Dashboard top renders a `## Content` section below Affect, listing the above with md links to each subpage file. Candidate rows in dashboard sections carry three anchor buttons: `✅` pin (jump to target subpage, milestone uses `scope` to land in Us or Me) · `❌` drop (delete + tombstone) · `✏️` edit (in-place edit; md uses placeholder semantics, HTML layer realises it).

## Content flow — md is SoT, DB is index
- Markdown is authoritative. DB is an index/search/aggregation layer that follows md.
- System → md: inserter mode. Per-block content_hash in md_index; hash match on user-modified block → auto-writer skips (preserves edit).
- md → System: watcher (watchdog/FSEvents) monitors md roots. Edit → md_index hash diff → DB sync (insert/update/tombstone).
- Block id `<!-- id:N date:YYYY-MM-DD -->` stable across renders; tombstone via md_index.tombstones.
- All hand-edits preserved across files, regions, blocks.
- Recovery: md is canon; backups and system versioning cover loss.

## Hooks (four)
- SessionStart — inject open threads + alerts; Phase 2 adds emotion backdrop. No persona (static CLAUDE.md owns it).
- UserPromptSubmit — must-never-fade injection + optional deterministic recall fallback (local-embedding vector search, config-gated).
- SessionEnd — sync code (clean transcript → events archive → dashboard regen → handover skeleton, <2s, no LLM) + async sonnet (AFFECT / ENTITY_CAND / THREAD_CAND / MILESTONE_CAND / MEMES_CAND / DIGEST / NARRATIVE; raw transcript LLM 1×, nightly never re-reads).
- PreToolUse — write_guard. Phase 1 reuses global prompt-guard. Phase 3 routes prompt-class md writes to writer sub-Claude.

## Injection — pull, not push
- Memory in SQLite, read on demand via MCP tool calls. Results return on MCP channel, not hook stdout — the 10000-char hook cap never applies.
- SessionStart handoff renders open threads + alerts into a daemon-rendered CLAUDE.md marker block; short, fixed-size.
- CLAUDE.md = static hand zone (persona, family, MCP usage guide) + daemon-rendered marker block. Hand zone never grows with data.
- @import is not the memory path (loads once, no live recall).
- Weak-model coverage: handoff is deterministic; UserPromptSubmit fallback covers mid-session; an Alert fires only when a session references the past yet recall stayed 0.

## LLM provider abstraction
- All pipeline calls route through one client. Callers pass intent (role + body); provider/flags/model/credit channel are config.
- Chain: stream-json subscription (default) → `claude -p` pool (fallback). Swap = edit one config line.
- Auto-rotation: per-step alert; whole chain fails → halt + big alert, never silent degrade.
- Pending: per-event tier/timeout/retry table, filled per event at build.

## Hard constraints
- LLM via `claude` CLI subprocess (OAuth). No cloud embedding.
- Three tiers: cheap/local for tagging-routing (bulk), mid for narrative, top for user-facing only.
- Atomic write every rendered md (temp + replace). Every scheduled job: try/except + alert row on fail.
- Data under `~/.config/marrow/`, code under `~/CC-Lab/marrow/`. Hook scripts ≤100 lines.
- Stub policy: each phase builds only what it uses. Placeholder tables OK (commented); stub classes banned.
- Prompt/subagent template change: notify Lumi to confirm wording.

## Safety nets (do not cut)
> Baseline: Lumi never manually clears markers, never triggers catchup, never retries. No silent fail. Token bounded. Originals recoverable.
- Required nets: backup · retry · catchup · failure-alert · concurrent-write lock · atomic write · idempotency · timeout brake · edit safety · drift sweep · claude.md render guard · affect heartbeat · affect neutral fallback · affect catchup.
- Shipped mechanism → PROGRESS. Pending mechanism (drift sweep · claude.md render guard · retry thresholds · catchup scan window · edit-safety anchor format) → FUTURE.

## Phase plan
- Phase 1 shipped — memory core: SQLite + FTS + vec, daemon (MCP recall), 4 hooks Phase-1 subset, dashboard top, migrate.py, `mw` CLI, 4 launchd jobs (daily-routine / catchup / db-backup / aging), jsonl retention → cc `cleanupPeriodDays`.
- Phase 2 in progress — emotion (affect) + recall fusion + entity co-emit + sub-page render fills out.
- Phase 2.5 in-flight reset — SessionEnd async LLM pipeline · diary demote to read-only 07:00 roll-up (was 04:00) · threads → tasks · candidates 0-audit · pinned no-decay · 6AM day boundary · all-sonnet tier.
- Phase 3 (in-flight, locked 2026-05-25) — md-as-SoT reversal · md_index hash table + watchdog daemon + per-block inserter for handover/dashboard/subpage/projects · Handover prompt split (state + narrative) + sessionend silent-death root-cause fix in wt-handover · Waves: wt-handover ∥ wt-md-a → wt-md-b ∥ wt-md-cd → wt-md-e → wt-md-f
- Pending (scope/order TBD): writer authority · cross-channel parity (WeChat deep rebuild) · addons + OSS (stellan_wallet first). Detail → FUTURE.
