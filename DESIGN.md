# Marrow Foundation Design

> Personal AI memory + workflow system. SQLite-backed, model-agnostic, one dashboard.
> Goals + structure + hard constraints only. Mechanism → MAP. Decisions → DECISIONS. History → PROGRESS. Unbuilt → FUTURE.

## Goals & Outcomes
> Always think about if goals are matched by the design.
1. Host & vendor portable — LLM provider, storage path, scheduler, notifier, backup, AND data migration all swap by config → Swap model/vendor by editing one config line.
2. Cross-channel parity — chat history, memory, setting, commands sync all in one; start with CLI and WeChat → Switch CLI ↔ WeChat mid-thought.
3. Semi-permanent memory — major events permanent, emotion consistent, cold recent drops if unused → Past facts resurface on mention; cold recall fast; no context repeated.
4. Workflow + build carryover — where I left off and outcome-level build narrative survive sessions.
5. Emotional continuity — relationship and persona density transfer losslessly across sessions, platforms, and models.
6. High auto, low maintenance — everything inputs and updates automatically including dashboard; every surface hand-readable and editable → Opens dashboard, sees what's open + what broke. Never manually clears a marker, triggers catchup, or retries.
7. Expandable base — new capability = addon.

## Architecture (main line)
- daemon — Python MCP server, serves CLI + WeChat clients.
- storage — SQLite + FTS5 + sqlite-vec.
- runtime — `claude` as stream-json subprocess inheriting OAuth subscription.
- bridge — local socket for WeChat permission routing.
- frontend — auto-rendered `dashboard.md` + static CLAUDE.md family. Memory pulled via MCP, never injected.
- supervisor — daemon watchdog; restart + alert on storm.

## Content flow — bidirectional sync
- DB and md are peers, not primary/replica. Both writable; both changes preserved.
- DB → md: inserter mode, per-block content_hash skip on user-edited blocks.
- md → DB: watcher detects edit → md_index hash diff → DB sync (insert/update/tombstone).
- MCP/CLI → DB: programmatic writes render affected subpage atomically after DB commit.
- Block id `<!-- id:N date:YYYY-MM-DD -->` stable across renders. All hand-edits preserved. No silent overwrites.
- Recovery: md readable + DB backed up; either side can reconstruct the other.

## Hard constraints
- LLM via `claude` CLI subprocess (OAuth). No cloud embedding.
- Three tiers: cheap/local for bulk, mid for narrative, top for user-facing only.
- Atomic write every rendered md (temp + replace). Every scheduled job: try/except + alert on fail.
- Data under `~/.config/marrow/`, code under `~/CC-Lab/marrow/`. Hook scripts ≤100 lines.
- Stub policy: each phase builds only what it uses. Placeholder tables OK; stub classes banned.
- Prompt/subagent template change: notify Lumi.

## Safety nets
> Baseline: Lumi never manually clears markers, never triggers catchup, never retries. No silent fail. Token bounded. Originals recoverable.
- Required nets: backup · retry · catchup · failure-alert · concurrent-write lock · atomic write · idempotency · timeout brake · edit safety · drift sweep · claude.md render guard · affect heartbeat · affect neutral fallback · affect catchup.
- Shipped → PROGRESS. Pending → FUTURE.
