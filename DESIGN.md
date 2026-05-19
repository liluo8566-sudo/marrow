# Marrow Foundation Design

> Status: design under review, no code yet. This is a frame-level spec — intended effect plus a method direction, not code-level detail.
> Please always writing in English and no comment for scripts

## How to read this doc

- DESIGN = goal + structure + hard constraints.
- Current decisions live in DECISIONS.md (confidence-tagged).
- Unbuilt plans live in FUTURE.md.
- Do not infer the answer from the old ny-memm system — copying its shape is the trap that made it need replacing.
- Reference memm_agent_manual for prompt format writing.

## Source of truth

- Structure lives here; current truth lives in DECISIONS.md.
- Consistent with CLAUDE.md `<principle>`: any doc is overturnable, only goals/outcomes bind.

## Lumi's goals

Every decision below must trace to one of these. If it cannot, it is scope creep.

1. Migration-friendly — swap to Codex / Claude / local small model by config, never Anthropic-locked; cyberboss is the proof; open-sourceable at the end.
2. Cross-channel parity — CLI and WeChat switch and resume mid-thread without losing the thread; commands align, interrupt/stop/rewind, and permission yes/no behave identically; WeChat is interaction-only, not heavy coding.
3. Semi-permanent memory - major life events permanent, emotion consistent, recent context is never repeated - can drop if unused for a while.
4. Workflow + build carryover — work and study state (where I left off, next step) and the outcome-level build narrative (repo to finished feature, not which line changed) survive across sessions; past mistakes self-summarise into avoided rules (lesson = FUTURE addon, see DECISIONS).
5. Emotional continuity — relationship and persona density transfer losslessly across sessions, platforms, and models without depending on a timeline file or model-native memory.
6. High auto, low maintenance — Lumi never routinely reviews anything anywhere; memory quality and cost stay balanced; every surface is hand-readable and editable on the dashboard or Obsidian including subpages, though she never has to.
7. Perfect, expandable base — the foundation is small but flawless; new capability arrives as an addon or extension, never a base rewrite; the old system's high cost and maintenance burden is the failure being designed out.

## One-line goal

Personal AI memory + workflow system. Replaces the ny-memm pipeline. SQLite-backed, model-agnostic, one dashboard.

## Outcome — what Lumi experiences when it works

- Opens one file (`~/Desktop/NY/dashboard.md`), sees what is open and what broke, nothing else demanded.
- Never repeats context — past facts resurface on mention; cold recall is fast.
- Never manually clears a marker, triggers catchup, or retries a failed step.
- Owns her own memory — anything recorded wrong can be corrected at a point, deterministically. Not a black box she has to beg to forget.
- Switches CLI to WeChat mid-thought without losing the thread.
- Swaps the model/vendor by editing one config line.

## Hard constraints

- No Anthropic API key. LLM calls go through the `claude` CLI subprocess (OAuth subscription) or local Ollama for backend tagging.
- Subscription-first: pipeline soaks the unused Max headroom via stream-json subprocess (cyberboss pattern). Dedicated credit pool is fallback only, steady-state burn ≈ 0%.
- No cloud embeddings — local sqlite-vec + a small local sentence model.
- Atomic writes for every rendered md (temp + replace).
- Every scheduled job: try/except + an alert row on failure. No silent failure.
- Data and code live apart: data under `~/.config/`, code under `~/cc-lab/marrow/`.
- Hook scripts stay small (target ≤ 100 lines each).
- Prompt/subagent template changes: notify Lumi to confirm wording.
- Three LLM tiers: cheap/local for compression-classification-routing (the bulk), mid for narrative (diary, weekly curate), top for the user-facing conversation only.
- Emotion breath at most once per SessionStart — never per-turn/per-N-turn (see DECISIONS).

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

Phase-2 / later: affect (per-event, see DECISIONS) / entities+entity_facts (HOLD) / corrections (placeholder) / transactions (FUTURE stellan_wallet); emotions/people/preferences/dir placeholders removed.

Migration mapping (source → target):

- `memory/2026.md` log lines → events (one row per line, role=log, compressed=1)
- `memory/timeline.md` ## Us → milestones (scope=us, date=YYYY-MM-DD); ## Me → milestones (scope=me, date=calendar year, birth 1995 + age-range start)
- Lighthouse → milestone (scope=me): Marrow memory-system rebuild
- `memory/reference.md` <cipher> → vocab (type=cipher)
- `code/_pit.md` ## blocks → pit (status=idea)
- `铁锅/语录/*.md` → goose_bites (one row per ### date)
- Dropped: Open-Threads / Alerts / Lessons (empty or stale); <lifestyle> / <family> (Phase 2); Garden HTML keepsakes + stickers (no Phase 1 source — WeChat stickers are Phase 4 / cyberboss)

storage.py is the schema source of truth; this section states intent only.

## Core mechanism — one pipeline, not many

This is the answer to "what's the difference between blocks". There mostly isn't one.

milestone, pit, vocab, project, memes all run the same pipeline: scan a signal in the session → write one table → render one view. They differ only by which table and which view name. Spec one of them and the rest are "same as above". Do not write one to product level and leave the next bare.

lesson is not a base concept — it left base to a FUTURE addon (see DECISIONS). The shared pipeline has no exception.

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

This is a product, not an AI toy. Lumi must be able to fix anything she can see, by hand, without touching code and without being forced through an LLM. She organizes her own files — sees clutter or waste, she cleans it — so the system must let her.

Principle (replaces the earlier read-only-sub-page split): every rendered file Lumi can see is writable. A hand-edit is reconciled back into the store, never silently overwritten. The read-only surfaces are the Monitor Zone (audit-log mirror) and the Cheatsheet (disk mirror) — editing them is meaningless, so they are display-only.

Three hand-run paths, all without code or a required LLM, pick whichever fits:

- Edit the md directly — primary for structured views, supported for narrative views. Open in Obsidian, change / trim / delete, save. Before the next render the hook reconciles back to SQLite, old values to backup, then re-renders. Output matches what she wrote, no visible jump.
- `mw` CLI — precise single point. Edit or remove one record by id. Deterministic, scriptable, no LLM.
- Tell Claude in plain language — convenience. "tighten this diary" / "education's wrong, Bendigo 3y not Melbourne". Claude finds the record, shows current vs new, on confirm writes it, old value to backup.

Reconcile is split by view type — not one parser for all:

- Structured views (Open Threads, milestone, vocab, pit, alerts): each row carries a visible short id at line/block end. id present + text changed → update; id deleted with the row → delete/abandon; new block with no id → insert. md edit is the primary path here.
- Narrative views (diary, goose-bites): row boundary is the date heading only, never a blank line. Two operations only: edit text inside a date block → update (whole content overwritten by id, internals not parsed, system-only columns preserved by id); delete the whole date block including its heading → delete that day. Clearing the body while keeping the heading is not a delete. Splitting narrative into new rows by blank line / dot points is not supported — re-organising history goes through the `mw` CLI or telling Claude, which is the primary path for narrative.

The reconcile semantics above are fixed, not Pending. Only the anchor's character format + per-view render template are Pending (set when each view is built). Conflict guard unchanged: hash-compare before overwrite; if Lumi changed it, back up + one Alert, never silent.

Conflict guard: before any overwrite, hash-compare; if Lumi changed it, back up the old file and raise one Alert. Never overwrite in silence.

## Fact corrections — conflict priority

A corrected fact lands in a corrections store, never by overwriting an event (events are append-only raw stream, not the fact authority). Capture has its own lightweight intake (lesson pipeline removed, see DECISIONS); facts skip promote-to-rule (a fact updates truth, not a rule).

Hard rules:
- Lumi's current input is top truth; stored memory never rebuts her. The store exists to stop the assistant's own mis-recall, not to validate or correct her. On conflict she wins — at most "my record says X, updating to your Y", never "let me remind you". Shorthand or stale wording is not an error to push back on.
- Serial facts = append state-sequence + latest pointer, not single-value overwrite. A new state supersedes the old (old kept, marked superseded, history-queryable); recall returns latest only; a superseded state is never raised against new input.
- Conflict priority: Lumi current input > Lumi-confirmed structured > system structured (milestone / preference) > raw event; same layer newer > older; an event is a lead, never the arbiter.

corrections table = Phase 2 placeholder (design fixed here, not built Phase 1).

Why this beats a black-box model memory: the memory IS Lumi's own SQLite + files, not the model's hidden state. Correction is deterministic, reversible, point-targeted — never begging a model to forget. This is how semi-permanent memory and migration-friendliness land.

## Emotion (Phase 2)

See DECISIONS.md — per-event affect table, two-band SessionStart entry, single-scalar recall, decay FLOOR tiers. Mechanism converged 2026-05-19 from real source + blind design.

## Hooks (four)

- SessionStart — injects open threads + open alerts (no who-i-am; persona in static CLAUDE.md); (Phase 2) emotional entry — see DECISIONS. Diary-catchup not here: 16:00 launchd (see DECISIONS).
- UserPromptSubmit — must-never-fade injection; plus the optional config-gated deterministic recall fallback (local-embedding vector search → top-K into additionalContext). Default off for a strong model.
- SessionEnd — async, code-only (no LLM): pass an archive-skip gate (see Pending — session archive skip), then clean this session's transcript (strip tool/fetch/system noise, keep the full human dialogue verbatim) and archive turns to events; regen the dashboard top. Diary is NOT here — see diary scheduling. Emotion is NOT here either (see Emotion).
- PreToolUse — write_guard. Phase 1: the existing global `~/.claude/hooks/prompt-guard.py` (English-only + no pipe tables on prompt-class .md), scope extended to cover `~/cc-lab/marrow/` — one global hook, not a Marrow-local copy. Phase 3: route writes to prompt-class md to the writer sub-Claude; main Claude loses direct write there.

Diary scheduling — see DECISIONS for the shipped detail (local-04:00 day boundary, per-session map-reduce, two decoupled launchd jobs: 04:00 routine writes the just-closed day, 16:00 catchup backfills the last days). haiku digests sessions (volume-only, no value-cut/arc), merges them on local timeline (tags dropped, weights uneven), sonnet writes diary. Buddy end-of-turn comments stripped at transcript clean. No lesson extraction (see DECISIONS).

## Injection

Pull, not push. Memory lives in SQLite and is read on demand via MCP tool calls — the daemon is the MCP server. Tool results return on the MCP channel, not hook stdout, so the ~10000-char hook cap never applies and context never carries unused memory. This is what makes the base expandable: a new memory class is a new table plus a tool, with zero change to the injection path.

- On-demand recall — Claude calls a recall tool when a turn references the past; the daemon returns only the matched rows under a token budget. Scales with the DB without bloating context; always reads the live store.
- Session-start handoff — SessionStart renders open threads + alerts into daemon-rendered CLAUDE.md marker block; short and fixed-size, never a growing md.
- CLAUDE.md holds the static layer plus a daemon-rendered marker block: persona, family, one short MCP usage guide (hand-written zone), and the must-never-fade convention layer in the marker block (see "Pending — dir, drift sweep, convention injection"). The hand-written zone never grows with data; it is not an @import pile.
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
- Phase 2 — Emotion (affect) + decay + sub-page render fills out; entity (people/pref) pipeline HOLD pending pipeline-bug — see DECISIONS. Sub-page render config-driven (goal 7); stellan_wallet first opt-in addon.
- Phase 3 — Writer authority: prompt-class md writes go through the writer sub-Claude.
- Phase 4 — Cross-channel parity (see weclaude + cyberboss Pending below).
- Phase 5 — Addons + open source.

Stub policy: each phase creates only the modules it uses. No empty skeletons. Placeholder tables in schema are allowed (commented); stub classes in code are banned.

## Migration

Phase 1 ships SQLite alongside the running ny-memm; both run in parallel ~2 weeks; old pipeline retires once stable. `migrate.py` imports historical md into tables (per-file source→target mapping in the Data model section above). Old `memory/` md and the `code/` folder move to archive read-only, then are removed after the parallel window. `code/rule.md` folds into `~/Desktop/NY/CLAUDE.md` and is deleted post-merge.

## Safety nets (Lumi's section — do not cut)

Baseline effect: Lumi never manually clears markers, never triggers catchup, never retries. No silent failure. Token bounded. Originals always recoverable.

- backup — DB never lost — daily dump + iCloud offsite — method agreed; retention Pending.
- retry — transient LLM/IO failure self-heals — one retry then degrade tier — method agreed; thresholds Pending.
- catchup — a missed diary/endhook is recovered — SessionStart-triggered rescan over event-days lacking output (not a resident watcher), idempotent skip — method agreed; scan window/cap Pending.
- failure alert — no silent fail — any step writes an alert row to dashboard top with a recovery hint — agreed.
- concurrent-write lock — parallel session-ends never corrupt the DB — serialize writers — REQUIRED, mechanism Pending.
- atomic write — a crash mid-write never leaves a half file — temp + replace on every rendered md — REQUIRED, mechanism Pending.
- idempotency — catchup re-run never double-inserts — content/source-hash dedup — method agreed.
- timeout brake — a hung agent cannot stall the pipeline or burn tokens — hard subprocess timeout + kill — REQUIRED, mechanism Pending.
- edit safety — every visible rendered file is writable; structured views reconcile by row id, narrative views by date block (whole-content overwrite or full-block delete); hand-edits never lost; conflict = back up + alert before overwrite — agreed; anchor char format + render template Pending.
- drift sweep — a moved/renamed/deleted/merged file never leaves dangling references — git-diff-triggered deterministic ripgrep + key-indirection + cheap-model free-text fallback — REQUIRED, mechanism Pending.
- claude.md render guard — the daemon-rendered marker block never destroys the hand-written zone — marker partition + hash-compare + reconcile + backup + atomic + Alert — REQUIRED, mechanism Pending.
- migrate safety — old data never destroyed — parallel run ~2 weeks + originals archived read-only — agreed.
- affect heartbeat — emotion silently rotting is caught same-day — SessionStart code assertion (latest affect >48h or gap day in last 7d → block first line ⚠) — REQUIRED
- affect neutral fallback — bad/missing 04:00 affect JSON never leaves a math hole — code inserts a neutral row (V0.5/A0.3/imp3), diary still writes — agreed
- affect catchup — a missed affect day self-heals — idempotent rescan over event-days lacking affect, same code as backfill — agreed

## Pending — weclaude + cyberboss fusion

After the memory core ships, the WeChat side gets rebuilt. Not decided whether to adopt cyberboss or upgrade weclaude. This is NOT just swapping `claude -p` for another spawn — it carries a real workload, all Pending design:

- multi-message send + 铁锅 rewrite on the new runtime
- `/stop` / `/resume` / interrupt / rewind parity
- WeChat permission yes/no routed to the daemon bridge
- bidirectional resume (CLI ↔ WeChat handoff on one thread)
- the cyberboss migration path as the model-swap proof for the migration-friendly goal

Design this when Phase 4 starts, not before.

## Pending — dir, drift sweep, convention injection

Dropped: file-level full index (dir table, hand-maintained tree, macOS Spotlight) — conflicts portability + low-maintenance. "Where is X" = daemon on-demand ripgrep over authorized roots.

Two real needs survive the drop. Both REQUIRED; mechanism detail Pending:

- drift sweep — Lumi moves / renames / deletes / merges a file; every reference follows without her reminding anyone. Trigger = a path-change event (git diff detects it). Three layers: (1) deterministic ripgrep over authorized roots finds every reference to the old path — primary, no model, never misses; (2) key-indirection — docs/scripts reference a key, not a hardcoded path, so a move edits one registry entry and references stay correct; (3) cheap local model sweeps free-text mentions as fallback — never lets the model touch key paths.
- convention injection — naming / folder-placement rules sit in the every-turn injection layer, never a sub-page (Claude does not read sub-pages on its own; a rule there is a dead rule). Single source → drift sweep maintains the source → daemon renders it into a marker block in CLAUDE.md → SessionEnd renders, next SessionStart applies. Lumi edits the source once or not at all; she never hand-manages it.

CLAUDE.md render: daemon writes via Python file IO, not the cc Write tool — cc permission / bypass and the 10000-char hook cap never apply (same path as diary render). Marker-block partition: the daemon rewrites only its marker block; the hand-written zone (persona, coding discipline) is never touched. This deliberately removes Anthropic's default block on cc editing CLAUDE.md (a high-weight every-turn file), so the guard set is the compensating safety net, REQUIRED not optional: hash-compare before overwrite, marker-outside never overwritten, marker-inside hand-edit reconciles + backup + one Alert, atomic write.

## Pending — data lifecycle

Backup direction: iCloud owns offsite copies; restore on a fresh Mac without touching code. Retention window + prune cadence Pending. Cleanup: per-source retention rules + executor Pending.

Tier split (fixed, not Pending) — three tiers:

- Permanent keepsake — milestones, diary, goose-bites, projects, study, major life facts. Add-only, never decays.
- Demote-sink — low-value reference + cold vocab (use_count / last_seen long idle). Weight decays, row sinks below the active set, a keyword hit revives it (Ombre weight-pool: resolved → sink → keyword-recall). Not deleted.
- Raw-stream — detailed event rows, resolved alerts, audit_log, DB dumps, low-use stickers. Real retention + prune.

Effect target: no growth alerts, no manual rm, no DB bloat.

Decided — raw jsonl cleanup is NOT Marrow's job. Use `cleanupPeriodDays` in `~/.claude/settings.json` (global, by mtime, all projects). Marrow prunes SQLite-internal raw-stream: aged rows, resolved alerts, audit_log, dumps. Never jsonl. Not enabled yet.

## Pending — session archive skip

Skipped sessions excluded from diary/recall. (Legacy: `summ-skip` stamp; trigger: `ssmmm` skill.)

- Manual skip: stamp file, `mw` command, or in-session trigger
- `mm+` force-include — into diary regardless of turn count (overrides ≤3 drop / SHORT auto-skip)
- `mm-` force-skip — excluded regardless of turn count (30+ turns still skip)
- Auto skip: turn threshold (Pending)
- Idempotent: skip = do nothing; raw-stream cleanup is separate tier

Phase 1: code-only, non-blocking.

## Pending — open items

Decided to defer, do not invent:

- sub-page hyperlink concrete paths
- which columns each view's SQL extracts (e.g. milestone)
- the md render template behind each view
- per-event LLM topology table
- schema-evolution mechanism (user_version + ordered patch chain, replaces the interim hand-written ALTER)
- doc auto-render upkeep (DESIGN / DECISIONS / README / dir map) — no manual maintenance
- retrieval fusion — single weighted scalar (copy claude-imprint lane engineering, not RRF); k/weights at recall-module build
