# Marrow Foundation Design
> Personal AI memory + workflow system. Replaces the ny-memm pipeline. SQLite-backed, model-agnostic, one dashboard.
> This is a frame-level spec — intended effect plus a method direction, not code-level detail.
> Please always writing in English

## Lumi's goals
> Always think about if goals are matched by the design.

1. Host & vendor portable — LLM provider, storage path, scheduler, notifier, backup target swap by config (cyberboss-proven). Mac → Mac mini / VPS / cloud is deployment change, not rewrite. Open-sourceable at the end.
2. Cross-channel parity — CLI and WeChat switch and resume mid-thread without losing the thread; commands align, interrupt/stop/rewind, and permission yes/no behave identically; WeChat is interaction-only, not heavy coding.
3. Semi-permanent memory - major life events permanent, emotion consistent, recent context is never repeated - can drop if unused for a while.
4. Workflow + build carryover — work and study state (where I left off, next step) and the outcome-level build narrative (repo to finished feature, not which line changed) survive across sessions; past mistakes self-summarise into avoided rules (lesson = FUTURE addon, see DECISIONS).
5. Emotional continuity — relationship and persona density transfer losslessly across sessions, platforms, and models without depending on a timeline file or model-native memory.
6. High auto, low maintenance — Lumi never routinely reviews anything anywhere; memory quality and cost stay balanced; every surface is hand-readable and editable on the dashboard or Obsidian including subpages, though she never has to.
7. Perfect, expandable base — the foundation is small but flawless; new capability arrives as an addon or extension, never a base rewrite; the old system's high cost and maintenance burden is the failure being designed out.

## Outcome — what Lumi experiences when it works

- Opens one file (`~/Desktop/NY/dashboard.md`), sees what is open and what broke, nothing else demanded.
- Never repeats context — past facts resurface on mention; cold recall is fast.
- Never manually clears a marker, triggers catchup, or retries a failed step.
- Owns her own memory — anything recorded wrong can be corrected at a point, deterministically. Not a black box she has to beg to forget.
- Switches CLI to WeChat mid-thought without losing the thread.
- Swaps the model/vendor by editing one config line.

## Hard constraints

- LLM calls go through the `claude` CLI subprocess (OAuth subscription) or local Ollama for backend tagging.
- Subscription-first: pipeline soaks the unused Max headroom via stream-json subprocess (cyberboss pattern). Dedicated credit pool is fallback only, steady-state burn ≈ 0%.
- No cloud embeddings — local sqlite-vec + a small local sentence model.
- Atomic writes for every rendered md (temp + replace).
- Every scheduled job: try/except + an alert row on failure. No silent failure.
- Data and code live apart: data under `~/.config/`, code under `~/cc-lab/marrow/`.
- Hook scripts stay small (target ≤ 100 lines each).
- Prompt/subagent template changes: notify Lumi to confirm wording.
- Three LLM tiers: cheap/local for compression-classification-routing (the bulk), mid for narrative (diary, weekly curate), top for the user-facing conversation only.
- Emotion breath at most once per SessionStart (see DECISIONS).

## Architecture

A long-running local daemon serving both the CLI and the WeChat client off one SQLite store. Roles:

- daemon — Python MCP server. Serves CLI + WeChat clients.
- storage — SQLite with full-text + vector search.
- runtime — spawns `claude` as a stream-json subprocess inheriting the OAuth subscription (cyberboss pattern; pass-tested 2026-05-15: no `-p`, consumes the five-hour subscription window with no overage — re-test on 6-15 when the credit split lands).
- bridge — local socket for permission yes/no routing across channels (Phase 4).
- frontend — auto-generated `dashboard.md` + the static CLAUDE.md family (persona, family, MCP usage guide); memory itself is pulled via MCP, never injected here.
- supervisor — daemon health watchdog; restart on crash; alert on restart storm.

Exact paths, module layout, and the MCP tool list are decided when the daemon is built — not pinned here.

## Data model (structure)

Phase-1 first-class tables:

- events — every session turn archived; cleaned human dialogue (tool/fetch/system noise stripped at SessionEnd, never raw RPC)
- threads — next-session work tracking; backs Open Threads
- milestones — life events; backs Milestone view
- vocab — text memes / cipher / event / news
- stickers — visual meme assets; kept separate from vocab to avoid sparse columns
- pit — known issues / deferred fixes; backs Projects pit page
- diary — daily narrative from SessionEnd
- goose_bites — `铁锅`'s same-day takes; own sub-page (Best of the day), independent of diary
- alerts — system bugs / failures only; backs dashboard top
- audit_log — recent system writes; backs Monitor Zone (last N)

Phase-2 / later: affect (per-episode, see DECISIONS) / entities+entity_facts (emitted in the single call, see DECISIONS) / corrections (placeholder) / transactions (FUTURE stellan_wallet); emotions/people/preferences/dir placeholders removed.

Migration mapping (source → target):

- `memory/2026.md` log lines → events (one row per line, role=log, compressed=1)
- `memory/timeline.md` ## Us → milestones (scope=us, date=YYYY-MM-DD); ## Me → milestones (scope=me, date=calendar year, birth 1995 + age-range start)
- Lighthouse → milestone (scope=me): Marrow memory-system rebuild
- `memory/reference.md` <cipher> → vocab (type=cipher)
- `code/_pit.md` ## blocks → pit (status=idea)
- `铁锅/语录/*.md` → goose_bites (one row per ### date)
- Dropped: Open-Threads / Alerts / Lessons (empty or stale); <lifestyle> / <family> (Phase 2); Garden HTML keepsakes + stickers (no Phase 1 source — WeChat stickers are Phase 4 / cyberboss)

storage.py is the schema source of truth; this section states intent only.

## Dashboard — the single entry

`~/Desktop/NY/dashboard.md`. Lumi edits one file; everything else is system-managed or rendered from SQLite.

Top, system zones, top to bottom:

1. Alerts — bug reports + pipeline-failure only. Functional state phrases, short. Pipeline-failure alerts self-clear on the next successful run; bug alerts are cleared by Lumi after she acts (Alerts is a writable zone — delete the line, or `mw` command). Never accumulates unbounded. No lesson surface (DECISIONS: lesson out of base).
2. Open Threads — the only zone looked at every session. Three classes: daily / study / project. Row format follows Lumi's existing `### Open-Threads` style, due-first then entry-date.

Sub-page links (Obsidian internal links, click to drill in). Each is rendered from one table — same render contract, differing only by table and view (Cheatsheet is the exception: disk-rendered, read-only):

- Diary — month-grouped, drill into per-day narrative
- Milestone — life events (## Us + ## Me).
- Memes — hot vocabulary + sticker thumbnails.
- 铁锅 goose-bites (Best of the day)
- Study — folder: one page per unit, current rule (progress / due / submitted). Notion stays primary; this is the CC-visible mirror.
- Projects — folder: an index of active + done projects, a pit page (deferred backlog not in Open Threads), one page per project. A sub-page's own sub-pages do not appear on the dashboard — they are reached by jumping into the project page.
- Cheatsheet — scripts / hooks / skills / aliases plus a directory map (roots: marrow code, `~/.config` data, NY), all rendered from disk reality. Read-only: disk is the source of truth, hand-edits are meaningless and overwritten on next render.


Monitor Zone — bottom, read-only, last N system writes (target table + summary + time). Single purpose: Lumi sees where each piece of information landed and fixes the prompt directly. Not a scratch pad.

There is no user scratch zone. (An earlier draft invented one — removed.)

## Editing & correction — how Lumi changes anything

Principle: every rendered file Lumi can see is writable. A hand-edit is reconciled back into the store, never silently overwritten. 
- the Monitor Zone (audit-log mirror) can be read only.

Three hand-run paths, all without code or a required LLM, pick whichever fits:

- Edit the md directly — primary for structured views, supported for narrative views. Open in Obsidian, change / trim / delete, save. Before the next render the hook reconciles back to SQLite, old values to backup, then re-renders. Output matches what she wrote, no visible jump.
- `mw` CLI — precise single point. Edit or remove one record by id. Deterministic, scriptable, no LLM.
- Tell Claude in plain language — convenience. "tighten this diary" / "education's wrong, Bendigo 3y not Melbourne". Claude finds the record, shows current vs new, on confirm writes it, old value to backup.

Reconcile is split by view type — not one parser for all:

- Structured views (Open Threads, milestone, vocab, pit, alerts): each row carries a visible short id at line/block end. id present + text changed → update; id deleted with the row → delete/abandon; new block with no id → insert. md edit is the primary path here.
- Narrative views (diary, goose-bites): row boundary is the date heading only, never a blank line. Two operations only: edit text inside a date block → update (whole content overwritten by id, internals not parsed, system-only columns preserved by id); delete the whole date block including its heading → delete that day. Clearing the body while keeping the heading is not a delete. Splitting narrative into new rows by blank line / dot points is not supported — re-organising history goes through the `mw` CLI or telling Claude, which is the primary path for narrative.

The reconcile semantics above are fixed, not Pending. Only the anchor's character format + per-view render template are Pending (set when each view is built). Conflict guard unchanged: hash-compare before overwrite; if Lumi changed it, back up + one Alert, never silent.

Conflict guard: before any overwrite, hash-compare; if Lumi changed it, back up the old file and raise one Alert. Never overwrite in silence.

## Emotion (Phase 2)

See DECISIONS.md — per-episode affect table, 4-element SessionStart backdrop, single-scalar recall, decay FLOOR tiers. Mechanism converged 2026-05-19 from real source + blind design.

## Hooks (four)

- SessionStart — injects open threads + open alerts (no who-i-am; persona in static CLAUDE.md); (Phase 2) emotional entry — see DECISIONS. Diary-catchup not here: 16:00 launchd (see DECISIONS).
- UserPromptSubmit — must-never-fade injection; plus the optional config-gated deterministic recall fallback (local-embedding vector search → top-K into additionalContext). Default off for a strong model.
- SessionEnd — async, code-only (no LLM): pass an archive-skip gate (see FUTURE Phase 2 — session_archive_skip), then clean this session's transcript (strip tool/fetch/system noise, keep the full human dialogue verbatim) and archive turns to events; regen the dashboard top. Diary is NOT here — see diary scheduling. Emotion is NOT here either (see Emotion).
- PreToolUse — write_guard. Phase 1: the existing global `~/.claude/hooks/prompt-guard.py` (English-only + no pipe tables on prompt-class .md), scope extended to cover `~/cc-lab/marrow/` — one global hook, not a Marrow-local copy. Phase 3: route writes to prompt-class md to the writer sub-Claude; main Claude loses direct write there.

Diary scheduling — see DECISIONS (04:00 routine + 16:00 catchup, single sonnet call; per-session map-reduce kept only as the over-volume fallback). Buddy end-of-turn comments stripped at transcript clean; no lesson extraction.

## Injection

Pull, not push. Memory lives in SQLite and is read on demand via MCP tool calls — the daemon is the MCP server. Tool results return on the MCP channel, not hook stdout, so the ~10000-char hook cap never applies and context never carries unused memory. This is what makes the base expandable: a new memory class is a new table plus a tool, with zero change to the injection path.

- On-demand recall — Claude calls a recall tool when a turn references the past; the daemon returns only the matched rows under a token budget. Scales with the DB without bloating context; always reads the live store.
- Session-start handoff — SessionStart renders open threads + alerts into daemon-rendered CLAUDE.md marker block; short and fixed-size, never a growing md.
- CLAUDE.md holds the static layer plus a daemon-rendered marker block: persona, family, one short MCP usage guide (hand-written zone), and the must-never-fade convention layer in the marker block (see FUTURE Phase 3 — drift/convention infra). The hand-written zone never grows with data; it is not an @import pile.
- @import is not the memory path — it loads once at launch and does not re-read mid-session, which disqualifies it for live recall.
- UserPromptSubmit per-turn injection covers two cases: the rare must-never-fade item, and an optional deterministic recall fallback (config flag, default off for a strong model). When on, the hook runs the user turn through the same local-embedding vector search the model would call and injects the top-K hits into additionalContext. The retrieval engine is identical to model-pulled recall; only the trigger changes from model judgement to deterministic per-turn. It uses the local sentence-embedding model (a fixed local component, no token / subscription / cloud cost, independent of the conversation model — exact model Pending, set at build), never the conversation model and never keyword match. Steady-state cost stays near zero for a strong model with the flag off.

Weak-model mitigation, layered: SessionStart handoff is already deterministic and model-independent; the config-gated UserPromptSubmit fallback covers mid-session; recall call count goes to audit_log / Monitor Zone; an Alert (dashboard top, not Monitor Zone) fires only when a session references the past yet recall stayed 0 for the whole session — gate tuned at build/test. A silent memory miss becomes visible without polluting an otherwise-empty Alerts zone. The residual open risk (a strong model still calling recall on cue without the fallback) stays honestly logged; cyberboss in production is the only evidence so far.

## LLM provider abstraction

All pipeline LLM calls route through one client interface in the daemon. Callers pass intent only (role + body); provider, subprocess flags, model, and credit channel are config, never call-site concerns. Providers plug in behind it: stream-json subscription (default), `claude -p` on dedicated pool (fallback), local Ollama (emergency); Codex/others written only when a real migration needs them.

Selection is a default → fallback → emergency chain in one config file. Swap = edit one line + ensure the named class exists; callers don't change. Auto-rotation: default blocked → fallback + one alert; fallback fails → emergency + second alert; whole chain fails → halt + big alert, no silent degrade.

The per-event topology (which trigger uses which tier, timeout, retry) is a Pending table — filled when each event is built, not pinned now.

## Phase plan

Each phase ships one outcome.

- Phase 1 — Memory core: SQLite + full-text, the daemon with a minimal MCP tool set, all four hooks at phase-1 subset (SessionStart open-threads+alerts handoff only; UserPromptSubmit must-never-fade inject, recall fallback default off; SessionEnd code-only clean+archive + dashboard-top regen, no LLM/emotion/decay; diary via nightly 04:00 routine + 16:00 catchup launchd job; PreToolUse mirrors prompt-guard only), dashboard top render, migrate.py, the `mw` CLI. Runs in parallel with old ny-memm ~2 weeks, then retire it. Stream-json subscription routing is pass-tested (2026-05-15). The remaining unknowns — local vector ext on this macOS, MCP parity with cyberboss, cheap-tier diary quality — are not pre-verified; each surfaces and is settled at first build of its module, no separate verify phase.
- Phase 2 — Emotion (affect) + decay + sub-page render fills out; entity (people/pref) emitted in the single call — see DECISIONS. Sub-page render config-driven (goal 7); stellan_wallet first opt-in addon.
- Phase 3 — Writer authority: prompt-class md writes go through the writer sub-Claude.
- Phase 4 — Cross-channel parity (see FUTURE Phase 4 — weclaude_runtime_rebuild).
- Phase 5 — Addons + open source.

Stub policy: each phase creates only the modules it uses. No empty skeletons. Placeholder tables in schema are allowed (commented); stub classes in code are banned.

## Migration

Phase 1 ships SQLite alongside the running ny-memm; both run in parallel ~2 weeks; old pipeline retires once stable. `migrate.py` imports historical md into tables (per-file source→target mapping in the Data model section above). Old `memory/` md and the `code/` folder move to archive read-only, then are removed after the parallel window. `code/rule.md` folds into `~/Desktop/NY/CLAUDE.md` and is deleted post-merge.

## Safety nets (Lumi's section — do not cut)

Baseline effect: Lumi never manually clears markers, never triggers catchup, never retries. No silent failure. Token bounded. Originals always recoverable.

- backup — DB never lost — daily dump + iCloud offsite — shipped (VACUUM INTO + iCloud, keep14); see DECISIONS.
- retry — transient LLM/IO failure self-heals — one retry then degrade tier — method agreed; thresholds Pending.
- catchup — a missed diary/endhook is recovered — SessionStart-triggered rescan over event-days lacking output (not a resident watcher), idempotent skip — method agreed; scan window/cap Pending.
- failure alert — no silent fail — any step writes an alert row to dashboard top with a recovery hint — agreed.
- concurrent-write lock — parallel session-ends never corrupt the DB — shipped (fcntl.flock app-lock); see DECISIONS.
- atomic write — a crash mid-write never leaves a half file — temp + replace on every rendered md — shipped; see DECISIONS.
- idempotency — catchup re-run never double-inserts — content/source-hash dedup — method agreed.
- timeout brake — a hung agent cannot stall the pipeline or burn tokens — shipped (process-group kill); see DECISIONS.
- edit safety — every visible rendered file is writable; structured views reconcile by row id, narrative views by date block (whole-content overwrite or full-block delete); hand-edits never lost; conflict = back up + alert before overwrite — agreed; anchor char format + render template Pending.
- drift sweep — a moved/renamed/deleted/merged file never leaves dangling references — git-diff-triggered deterministic ripgrep + key-indirection + cheap-model free-text fallback — REQUIRED, mechanism Pending.
- claude.md render guard — the daemon-rendered marker block never destroys the hand-written zone — marker partition + hash-compare + reconcile + backup + atomic + Alert — REQUIRED, mechanism Pending.
- migrate safety — old data never destroyed — parallel run ~2 weeks + originals archived read-only — agreed.
- affect heartbeat — emotion silently rotting is caught same-day — SessionStart code assertion (latest affect >48h or gap day in last 7d → block first line ⚠) — REQUIRED
- affect neutral fallback — bad/missing 04:00 affect JSON never leaves a math hole — code inserts a neutral row (V0.5/A0.3/imp3), diary still writes — agreed
- affect catchup — a missed affect day self-heals — idempotent rescan over event-days lacking affect, same code as backfill — agreed

