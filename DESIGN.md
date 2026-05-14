# NY Foundation Design v1

Last revised 2026-05-15. Status: under review before code.

## Source of truth (read FIRST)

This document + SCHEMA.md + FUTURE.md are the only design source.

**Do NOT** read `~/Desktop/NY/memory/*.md`, `~/Desktop/NY/code/*.md`, or the existing `ny-memm-*` scripts as design reference. They are historical snapshots from the system being replaced and contain accumulated drift, abandoned decisions, and per-session Sonnet hallucinations. They are kept only for migration (see Migration section), not for guidance.

If something is unclear here, ask Lumi. Do not infer from the old system or extrapolate from its structure. The old system needed replacing — copying its shape is the trap.

## Lumi's six goals (short form, verbatim intent from Start again.md L10–16)

1. Migration-friendly — easy swap to Codex / Claude / local small model. cyberboss pattern as proof.
2. CLI ↔ WeChat parity — seamless cross-channel switching, command consistency, permission yes/no from WeChat.
3. Semi-permanent memory + lightweight reference recall — no repeating context; auto-resurface long-term facts on mention; FTS5 fast cold recall.
4. Workflow + study carryover across sessions — assignment context, where I left off, lesson capture, self-correcting on past mistakes.
5. Writing-prompt compliance — no extra detail, no Chinese leaks in English docs, no example pollution. Sub-Claude with strict system prompt for prompt-class md.
6. Emotional continuity across sessions / platforms / models — Stellan persona density carries through.

Every design decision below must trace to one of these. If it cannot, it is scope creep.

## Goal (one-line)
Personal AI memory + workflow system. Replaces existing ny-memm pipeline. Built for the six goals above.

## Hard constraints
- No anthropic API key. All LLM calls go through `claude` CLI subprocess (OAuth subscription) or local Ollama for backend tagging
- No cloud embeddings. Local sqlite-vec + sentence-transformers MiniLM
- Atomic writes for all md (`tempfile + os.replace()`)
- Subprocess timeout 900s default
- Hook scripts ≤ 100 lines each
- try/except + alerts row on every scheduled job
- Data lives in `~/.config/ny/`, code lives in `~/.ny/`. Always separate
- All input/output prompt templates and writing templates require explicit Lumi review before commit. Assistant must surface a draft for confirmation; no template body lands via assistant inference alone
- LLM call tiering (dedicated-credit-pool aware, post-2026-06-15):
  - Default Haiku — compression, classification, dedup, routing, format normalization. Target ~80% of pipeline calls.
  - Sonnet — complex narrative only (diary writing, weekly curator migration, retire mention check).
  - Opus — weclaude main user conversation only, with automatic fallback to Sonnet when dedicated credit nears the monthly cap.
  - Subscription channel reserved for user-facing turns. Pipeline never touches subscription.
  - Monthly dedicated burn target ≤ 30% of plan. Excess triggers Haiku-only degrade mode.
- Emotion breath frequency: once per SessionStart, OR every N=10 user turns within a long session — whichever lands first. Never per-turn (rejects Ombre Brain's per-turn breath as token waste).

## Architecture
- daemon — Python MCP server (FastMCP) at `~/.ny/src/ny/daemon.py`. Serves CLI + WeChat clients
- storage — SQLite at `~/.config/ny/ny.db`. FTS5 + sqlite-vec extensions loaded at boot
- runtime — subprocess spawn `claude --output-format stream-json --input-format stream-json --permission-prompt-tool stdio --resume <sid>`. Inherits user OAuth subscription. cyberboss pattern verified
- bridge — Unix socket at `~/.config/ny/ipc.sock` for permission yes/no routing across channels (Phase 4)
- frontend — auto-generated dashboard.md + fixed CLAUDE.md family + profile.md
- supervisor — daemon health watchdog. systemd-style restart on crash; healthcheck endpoint at `~/.config/ny/health.sock`; alert on > 3 restarts in 5 min

## LLM call topology

Maps every pipeline LLM event to model tier + credit channel + retry policy. New events must declare these fields before merge.

Trigger / Caller / Model / Credit / Timeout / Retry
- User turn cli / claude main loop / Opus 4.7 / subscription / n/a / n/a
- User turn wechat / weclaude bridge claude --stream-json / Opus 4.7 then Sonnet fallback / subscription then dedicated / 480s / 1 retry then fallback
- SessionEnd diary render / daemon claude -p / Sonnet / dedicated / 480s / 1 retry then accept first attempt
- Compress event batch / daemon claude -p / Haiku / dedicated / 240s / 1 retry then accept
- Vocab routing decision / daemon claude -p / Haiku / dedicated / 60s / 1 retry then drop
- Emotion tag phase 2 / daemon / rule-scan first Haiku fallback / dedicated / 60s / 1 retry then null
- Weekly migrate phase 2+ / daemon claude -p / Sonnet / dedicated / 480s / 1 retry then alert
- Writer-tool phase 3 short / PreToolUse claude -p / Haiku / dedicated / 240s / 1 retry then surface diff
- Writer-tool phase 3 long / PreToolUse claude -p / Sonnet / dedicated / 480s / 1 retry then surface diff

Notes (plaintext, no inline-code in the table above to avoid stacked-card rendering):
- subscription = $100/mo Max plan OAuth. Pre-2026-06-15 stream-json subprocess routes here. Post-6/15 verification = V2.
- dedicated = monthly programmatic credit pool announced 2026-05-14 (effective 6/15).
- All claude -p calls inherit `WECLAUDE_BRIDGE=0` (or absent) so they do not trigger NY pipeline recursion.

## User-facing files

Single dashboard entry. Everything else lives in SQLite or in sub-md rendered from SQLite. User edits one file only: the scratch zone of `dashboard.md` (below system-managed markers). Everything else is read-only / system-managed.

### Always-imported (CLAUDE.md family, combined < 100 lines)

- `~/.claude/CLAUDE.md` — global. Identity (Lumi + Stellan persona), interaction rules, output style. Hard cap < 100 lines.
- `~/Desktop/NY/CLAUDE.md` — NY project rules. Imports `code/rule.md` only. Short.
- `~/Desktop/Study/CLAUDE.md` — Study project rules. Short.

### Trigger-loaded, not always imported

Static facts that should NOT eat the always-import budget but should resurface on mention. Loaded by SessionStart / UserPromptSubmit hook on keyword hit:
- People — family / friends roster → SQLite `people` table. Hook injects on name mention.
- Lifestyle / preferences — taste / habits → SQLite `preferences` table. Hook injects on relevant turn.
- Profile (me + us + Stellan persona) — see PENDING below.

### Dashboard — the single entry

`~/Desktop/NY/dashboard.md`. Three system-managed zones at top + hyperlinks to sub-pages + scratch zone below.

System-managed top (wrapped in `<!-- SYSTEM-MANAGED-START -->` / `<!-- SYSTEM-MANAGED-END -->`, hook overwrites on regen):
- Open Threads — the only zone Lumi looks at every session. Format `[Next|Soon] [YYYY-MM-DD] <task> <progress> [Due YYYY-MM-DD]`. Due-first then entry-date.
- Alerts — system bug / hook failure / script exception. Functional state phrases, max 3.
- Recent Writes — monitoring zone, temporary. Last 10 system writes (target table + summary + time). Removed once prompt stability is confirmed (~Phase 2 end).

Hyperlinks to sub-pages (obsidian internal links, click to drill in):
- Diary — `~/Desktop/NY/diary/index.md` month-grouped, drill into per-day narrative.
- Milestone — `~/Desktop/NY/milestone.md` rendered from `milestones` table (## Us + ## Me).
- Memes — `~/Desktop/NY/memes.md` rendered from `vocab` + `stickers` tables, sticker thumbnails inline.
- Cheatsheet — `~/Desktop/NY/cheatsheet.md` rendered from scripts / hooks / skills / aliases on disk.
- Projects — `~/Desktop/NY/projects/` folder. `index.md` (project list with status), `pit.md` (pending issues across all projects), one `<project>.md` per project (outcome log + decision log + next step). Rendered from `threads` table where `category=project`.
- Study — `~/Desktop/NY/study/` folder. One `<unit_code>.md` per unit (current progress / due dates / submitted items). No pit. Notion remains primary; this is a CC-visible mirror. Rendered from `threads` table where `category=study`.

Scratch zone — below system-managed markers. Free zone for Lumi's own notes; hook never touches.

### Backend, user never reads

- `~/.config/ny/ny.db` — SQLite
- `~/.config/ny/stickers/` — visual meme assets (gif / jpg / png)
- `~/.ny/src/ny/` — daemon code
- `~/.ny/hooks/` — hook scripts

### PENDING Lumi decision

- Profile content placement. Two options on the table:
  - (a) Keep `~/Desktop/NY/profile.md` as a static identity import in CLAUDE.md glob — simple, all persona context always on.
  - (b) Split: static persona stays in CLAUDE.md glob body (counted toward < 100 line cap), dynamic people / preferences move to SQLite `people` + `preferences` tables with trigger-load hooks.
  - Tradeoff: (a) wastes tokens but never miss; (b) saves tokens but risks miss on first mention before hook fires.
  - Stellan's lean: (b) once Phase 1 hooks are stable, until then (a).

## Hooks (three total)
SessionStart:
- Pull dashboard top block, inject summary into Claude system prompt
- Phase 2: also breath top-N high-decay unresolved emotions into prompt

SessionEnd:
- Async archive session turns → events table
- Phase 2: emotion tag (rule scan first, Ollama fallback)
- Phase 2: decay update
- Regen dashboard top block
- Regen diary entry for the date

PreToolUse:
- write_guard.py
- Phase 1: pass-through behaviour mirroring existing prompt-guard.py (English-only on `.md` under `~/.claude/` and `~/Desktop/NY/`, no pipe tables)
- Phase 3: route writes to prompt-class paths (`CLAUDE.md`, `skills/**/SKILL.md`, `code/*.md`) to writer-tool. Main Claude loses direct write on these paths

## MCP tools exposed by daemon
- `memory_query(keyword, type?, limit?)` — top-K from events + vocab + milestones via FTS5 + sqlite-vec hybrid
- `memory_append(table, content, tags?)` — row id
- `memory_query_dir(keyword)` — file path + description
- `vocab_lookup(term)` — vocab row by key or value match
- `thread_update(thread_id, next_step, summary)` — next-session pointer
- `writer_invoke(spec)` — Phase 3. Returns md content from subprocess Claude with strict English system prompt
- `lesson_capture(scope, lesson_text, session_id)` — append row to `lessons` table when SessionEnd detects a Lumi correction pattern
- `people_lookup(name)` — Phase 2. Trigger-load hook hits this on name mention; returns roster row for context injection
- `preference_lookup(topic)` — Phase 2. Same shape, for lifestyle / taste recall

## Lessons capture (closes Goal 4)

When Lumi corrects Stellan ("missed X" / "wrong, should be Y" / "don't do Z again" / "我没说过这种话"), SessionEnd's diary subprocess detects the correction pattern and writes a row to `lessons` table.

Promotion flow:
- Generic enough to encode as a hard rule → Lumi runs `ny lesson promote <id>`, which appends to `~/Desktop/NY/code/rule.md` `<lessons>` block AND (if linguistic) to `chat-lint` forbidden patterns OR (if behavioural) to the relevant CLAUDE.md. The lessons row records the destination path.
- Not generic / context-specific → row stays in `lessons` table. Surfaced in dashboard Recent Writes temporarily; monthly batch lets Lumi promote or retire.
- Existing `~/Desktop/NY/memory/3d.md` `### Lessons` block migrates into the table on Phase 1 ship (currently empty, no migration weight).

## dir indexing — PENDING

Held until Phase 2 or 3. Provisional approach: Layer 1 (high-level tree) maintained by hand from current `~/Desktop/NY/memory/reference.md <directories>` block as the starting state. Leaf-level file lookup uses macOS `mdfind` (Spotlight), not watchdog. Reasons:
- watchdog cost on the large `~/Desktop/Study/` tree is unverified (V3 in verification list)
- `mdfind` already indexes all user-readable files via Spotlight, returns near-instant
- cold `grep -r` on a deep tree is slow and easily mis-targeted

Re-design only when a concrete "where is X" need arises more than N times. Schema for `dir` table stays in SCHEMA.md as a placeholder; do not implement the table or watchdog in Phase 1.

## CLI
Entry at `~/.ny/scripts/ny`, symlink at `~/.local/bin/ny`:
- `ny dashboard` — print top block
- `ny diary <date>` — show date entry
- `ny show <type> [filter]` — milestones / vocab / pit / threads / alerts / dir / audit
- `ny add <type> [...]` — thread / milestone / vocab / pit / lesson
- `ny lesson <list | promote <id> | retire <id>>` — manage lessons (review + rule promotion)
- `ny migrate` — import existing ny-memm md
- `ny gc --backup` — vacuum + sqlite dump
- `ny help` — print cheatsheet

## Existing templates to preserve

> **PENDING Lumi audit.** The five template blocks below were drafted by the previous session's assistant without Lumi confirmation. They paraphrase `~/Desktop/NY/code/memm_agent_manual.md` TASK 1 fields. Before Phase 1 ship: re-read the manual end-to-end with Lumi, decide which sub-fields survive into the new system (likely fewer, since craft pipeline + 4-tag monthly need rethinking), and rewrite this section as the single source. Keep / rewrite / drop each block explicitly — do not silently inherit.

Daily entry — Chinese, narrative-first:
- Include: my day, our chats, feelings, insights, anything funny or unexpected, anything worth recording for future
- Exclude: technical detail, project outcome, study progress. Anything already in memes / craft / study
- Work / study appear as one-sentence scene + emotion
- English terms kept as-is (Mounjaro / GAMSAT / reference)

Craft entry — English ONLY, technical:
- Format: `<subject 1> [did 1 2 3...], [process/detail], [outcome 1 2 ...]; <subject 2> ...`
- Keep process concise, drop entirely if resolved
- Pure facts + essential detail

Study entry — English ONLY, factual:
- Deakin / GAMSAT / S1-S3 pure facts + outcome
- Terse format similar to Craft

Open Thread row — follows Lumi's existing `### Open-Threads` style in 3d.md:
- Format: `[Soon|Next|Later] [YYYY-MM-DD] <task> <progress notes> [Due YYYY-MM-DD]`
- One row per thread, due-sorted then entry-date

Alert row — English, short pipeline-state phrase, follows Lumi's existing `### Alerts` style in 3d.md:
- Format: `- [YYYY-MM-DD] <kind> <state>: <detail> [(retry: <command>)]`
- `<kind>` = pipeline component name (cleanup / weekly / monthly / session / catchup / entry / hook)
- `<state>` = miss / failed / capped / fired / over cap
- `<detail>` = one-line specifics, sid optional
- Retry hint in parens when manual fix is needed
- Functional state, not severity level

## Repo structure
```
~/.ny/
  DESIGN.md  SCHEMA.md  FUTURE.md  README.md  .gitignore
  src/ny/
    __init__.py  daemon.py  cli.py
    memory/        SQLite CRUD + FTS5 + sqlite-vec
    emotion/       Phase 2 stub
    bridge/        Phase 4 stub
    writer/        Phase 3 stub
    scheduler/     Phase 5+ stub
    tools/         MCP tool implementations
    dir_watcher/   watchdog + cron
    utils/
      atomic_write.py     tempfile + os.replace
      subprocess_safe.py  timeout + try/except wrapper
      logging.py          structured log to alerts table
  hooks/
    session_start.sh  session_end.sh  write_guard.py
  templates/
    dashboard.md.template  profile.md.template
    writer_system_prompt.txt  emotion_rules.yaml  alerts_format.txt
  scripts/
    ny  migrate.py
  tests/
    unit/  integration/
```

External:
```
~/.config/ny/
  ny.db  ny.yaml  ipc.sock  stickers/  backup/
```

## Git workflow
- repo `~/.ny/`
- remote github private. Phase 1 optional, Phase 2 mandatory
- main = production
- worktrees per phase: `~/.ny-phase2`, `~/.ny-phase3`, `~/.ny-phase4`
- DB backup via cron daily: `sqlite3 ny.db .dump > ~/.config/ny/backup/ny-$(date +%Y%m%d).sql`. Retention 30 days
- Commit on every dashboard regen blocked (regen writes to filesystem, not repo)

## Phase plan

### Phase 0 — Verification (1–2 days, no code yet)
- V1 sqlite-vec install + load on macOS 25.4. If fails, fall back to FTS5-only for Phase 1; vec moves to Phase 2.
- V2 `claude --output-format stream-json` subscription routing pre- AND post-2026-06-15.
- V3 FastMCP + `claude --mcp-config <path>` parity with cyberboss reference.
- V4 Haiku diary-rendering quality test on 5 historical sessions. Compare against current Sonnet output. If quality gap > 30%, escalate diary tier to Sonnet permanently (LLM topology table updates).
- V5 watchdog cost on Study tree baseline — only run if dir indexing returns to scope; otherwise V5 deferred.

### Phase 1 — Memory core (3–5 days target ship)
- SQLite schema (events / threads / milestones / vocab / stickers / lessons / alerts / audit_log; emotions + diary + dir + people + preferences are placeholders for Phase 2+)
- FTS5 indexes shipped; sqlite-vec gated on V1 outcome
- Daemon (FastMCP) — minimum viable: 3 MCP tools (`memory_query`, `thread_update`, `vocab_lookup`)
- Hooks:
  - SessionStart — inject dashboard top + active threads. Trigger-load hook for `people` / `preferences` keyword mention.
  - SessionEnd — async events archive (batched), diary render via Haiku → Sonnet escalation on V4 outcome, lessons capture.
  - PreToolUse — chat-lint port from current system (CJK on .md + forbidden phrase scan). Writer-tool stub for Phase 3.
- Dashboard render — system-managed top zone only (Open Threads + Alerts + Recent Writes). Sub-pages start empty links to be filled in Phase 2.
- `migrate.py` — events / vocab / milestones / threads / lessons / stickers from existing md.
- `ny` CLI — `dashboard`, `diary <date>`, `show <type>`, `add <type>`, `lesson <list|promote|retire>`, `migrate`, `gc`.
- Parallel run with existing ny-memm for 2-week observation; then retire old pipeline.

### Phase 2 — Emotion + decay + sub-page render
- `emotions` table at per-session granularity
- Decay scoring daily cron
- Breath inject at SessionStart (top-N high-decay unresolved); per-N=10 turn re-inject as well
- Sub-page render fills out: diary / milestone / memes / projects / study hyperlinks
- `people` + `preferences` tables live; trigger-load hooks active

### Phase 3 — Writer authority
- PreToolUse intercepts writes to prompt-class md (CLAUDE.md / `skills/**/SKILL.md` / `code/*.md`)
- Routes to writer-tool subprocess (Haiku for short / Sonnet for long) with strict English + format system prompt
- Main Claude loses direct write on these paths

### Phase 4 — Cross-channel parity
- Unix socket IPC daemon ↔ clients
- WeChat permission yes/no routing
- weclaude rewrite on stream-json with `/stop` + `rewind` + `/resume` parity
- Bidirectional sid resume (cli ↔ wechat handoff)

### Phase 5 — Addons + open source
- Random pulse, memes vision tagging, proactive followup, Stellan autonomous push
- README, license, contribution guide

## Stub policy
No empty class skeletons for future phases. Each phase only creates the modules it actually uses. Reduces dead code and forces honest scoping. Stubs in code = banned; stubs in schema (placeholder tables) = allowed but commented.

## Migration from existing ny-memm
Current production pipeline:
- 8 scripts at `~/Toolkit/scripts/ny-memm-*.py`
- 5 launchd plists (rotate / curator / compress / retire / cleanup)
- 5 memory md files at `~/Desktop/NY/memory/`

Migration approach:
- Phase 1 ships SQLite alongside existing pipeline. Both run in parallel
- `migrate.py` imports historical content into SQLite tables. See SCHEMA.md mapping
- Two-week observation period after Phase 1 ship
- Retire ny-memm-* scripts and unload launchd plists once SQLite stable
- Old `~/Desktop/NY/memory/` md files move to `archive/`, kept read-only as historical fallback

## Data flow
Write side:
- User turn → Claude responds → Claude calls `thread_update` for explicit carryover → SessionEnd async pipeline: events archive (batched, Python only, no LLM) → diary render (Haiku → Sonnet) → lessons capture (Sonnet pattern detect) → emotion tag (Phase 2, Haiku) → dashboard regen reads latest threads/alerts/audit

Read side:
- SessionStart hook pulls dashboard top + active threads + (Phase 2) top-N breath emotions → injects into Claude system prompt
- UserPromptSubmit hook scans turn for keyword (Phase 2: people / preferences trigger) → injects matching row into context
- User mentions term Claude doesn't know → Claude calls `vocab_lookup` → injects definition
- User asks "where is X" → Phase 2+: Claude calls `mdfind` wrapper or `memory_query_dir` → returns absolute path
- User opens dashboard.md → reads system-managed top block + clicks hyperlink to drill into sub-page

## Open verification

See Phase 0 above for the full list (V1–V5). This section originally held verification items as a footnote; they are now Phase 0 first-class deliverables and gate Phase 1 code.
