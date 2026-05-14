# NY Foundation Design v1

Last revised 2026-05-15. Status: under review before code.

## Goal
Personal AI memory + workflow system. Replaces existing ny-memm pipeline. Built for migration safety, multi-channel parity, semi-permanent recall, workflow carryover, writing-prompt compliance, cross-session emotional continuity.

## Hard constraints
- No anthropic API key. All LLM calls go through `claude` CLI subprocess (OAuth subscription) or local Ollama for backend tagging
- No cloud embeddings. Local sqlite-vec + sentence-transformers MiniLM
- Atomic writes for all md (`tempfile + os.replace()`)
- Subprocess timeout 900s default
- Hook scripts ≤ 100 lines each
- try/except + alerts row on every scheduled job
- Data lives in `~/.config/ny/`, code lives in `~/.ny/`. Always separate

## Architecture
- daemon — Python MCP server (FastMCP) at `~/.ny/src/ny/daemon.py`. Serves CLI + WeChat clients
- storage — SQLite at `~/.config/ny/ny.db`. FTS5 + sqlite-vec extensions loaded at boot
- runtime — subprocess spawn `claude --output-format stream-json --input-format stream-json --permission-prompt-tool stdio --resume <sid>`. Inherits user OAuth subscription. cyberboss pattern verified
- bridge — Unix socket at `~/.config/ny/ipc.sock` for permission yes/no routing across channels (Phase 4)
- frontend — auto-generated dashboard.md + fixed CLAUDE.md family + profile.md

## User-facing files
Fixed and user-written:
- `~/.claude/CLAUDE.md` — global rules. Imports profile via `@~/Desktop/NY/profile.md`
- `~/Desktop/NY/CLAUDE.md` — local NY rules
- `~/Desktop/Study/CLAUDE.md` — local Study rules
- `~/Desktop/NY/profile.md` — me + us + Stellan persona

System-managed, user-readable. Edit only outside SYSTEM-MANAGED markers:
- `~/Desktop/NY/dashboard.md` — hub
- `~/Desktop/NY/views/Timeline.md` — auto-rendered milestones, scope=me / us
- `~/Desktop/NY/views/Memes.md` — vocab index, includes sticker thumbnails for visual entries
- `~/Desktop/NY/views/Pit.md` — pit list
- `~/Desktop/NY/views/Cheatsheet.md` — hooks / scripts / skills / tools inventory
- `~/Desktop/NY/views/Diary.md` — month-grouped index linking to per-date entries
- `~/Desktop/NY/views/Diary/YYYY-MM-DD.md` — per-day narrative entries

Backend, never edited by user:
- `~/.config/ny/ny.db` — SQLite
- `~/.config/ny/stickers/` — visual meme assets (gif / jpg / png)
- `~/.ny/src/ny/` — daemon code
- `~/.ny/hooks/` — hook scripts

## Dashboard layout
Top section wrapped in `<!-- SYSTEM-MANAGED-START -->` / `<!-- SYSTEM-MANAGED-END -->`. Hook overwrites this block on every regen. Outside markers is user free zone.

System-managed top:
- 🐛 Alerts — system bug / hook failure / script exception. Short phrase per line, system-voice. Severity sorted, max 3
- 📋 Open Threads — daily / study / project grouped. due-first then entry-date. One row format: `[Soon|Next|Later] [YYYY-MM-DD] <task> <progress notes> [Due YYYY-MM-DD]`
- 🪵 Recent Writes — last 10 system writes. table + summary + time

Entry links bottom (Obsidian internal links, multi-tab):
- 💌 Diary — index by month, drill to per-date entry
- ⏳ Timeline — me + us with Stellan inside us, theme-grouped
- 🦄 Memes — text vocab (cipher / event / news / quote) + visual stickers. Binary assets in `~/.config/ny/stickers/`, metadata in vocab table
- ⛏️ Pit — known issues
- 📋 Cheatsheet — auto-generated hooks/scripts/skills/tools inventory

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

## CLI
Entry at `~/.ny/scripts/ny`, symlink at `~/.local/bin/ny`:
- `ny dashboard` — print top block
- `ny diary <date>` — show date entry
- `ny show <type> [filter]` — milestones / vocab / pit / threads / alerts / dir / audit
- `ny add <type> [...]` — thread / milestone / vocab / pit
- `ny migrate` — import existing ny-memm md
- `ny gc --backup` — vacuum + sqlite dump
- `ny help` — print cheatsheet

## Existing templates to preserve
SessionEnd diary subprocess and dashboard regen must follow Lumi's existing templates. Stored as files in `~/.ny/templates/`, loaded by SessionEnd subprocess Claude system prompt.

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

Open Thread row — mixed language tolerated:
- Format: `[Soon|Next|Later] [YYYY-MM-DD] <task> <progress notes> [Due YYYY-MM-DD]`
- One row per thread, due-sorted then entry-date

Alert row — English, system-voice short:
- Format: `[<severity>] <type>: <short phrase>`
- system-voice, not user-voice

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
- Phase 1 MVP: SQLite + 6 MCP tools + dashboard top auto-gen + 3 hooks skeleton + profile.md migration + dir Layer 1 概况 doc + migrate.py + cheatsheet auto-gen
- Phase 2: emotion layer. decay engine + breath. Ombre short/long split (3-day boundary)
- Phase 3: writer-tool subprocess. Main Claude loses direct write on prompt-class md
- Phase 4: bridge. stream-json + Unix socket IPC. WeClaude rewrite on top
- Phase 5+: addons. Random pulse, memes vision tagging, craft pipeline, cross-channel media, Stellan autonomous push

## Phase 1 deliverables
- `memory/` module with CRUD + FTS5 + sqlite-vec
- `tools/` MCP tool implementations
- `dir_watcher/` basic watchdog
- `hooks/session_start.sh`, `session_end.sh`, `write_guard.py`
- `scripts/ny` CLI
- `templates/dashboard.md.template`, `profile.md.template`
- `scripts/migrate.py`
- Stub modules: `emotion/`, `bridge/`, `writer/`, `scheduler/`. Empty `__init__.py` + class skeleton
- Cheatsheet regen on git commit

## Out of Phase 1
- Cross-channel resume → Phase 4
- WeChat permission routing → Phase 4
- Random pulse / proactive push → Phase 5+
- Memes vision tagging → Phase 5+
- WeClaude stream-json migration → Phase 4
- Ollama fallback for emotion tagging → Phase 2

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
- User turn → Claude responds → Claude calls `memory_append` / `thread_update` for relevant capture → SessionEnd async batches all session turns to events table → emotion tagger runs (Phase 2) → diary subprocess renders narrative → dashboard regen reads latest threads/alerts/audit

Read side:
- SessionStart hook pulls dashboard top + active threads + breath emotions → injects into Claude system prompt
- User mentions unknown term → Claude calls `vocab_lookup` → injects into context
- User asks "where is X" → Claude calls `memory_query_dir` → returns absolute path
- User opens dashboard → reads system-managed top block + clicks entry link

## Open verification before Phase 1 code
- sqlite-vec install on macOS 25.4
- `claude --output-format stream-json` still routes to OAuth subscription after 2026-06-15 anthropic SDK billing change (must verify pre-6/15)
- watchdog cost on large `~/Desktop/Study/` tree, with file count baseline
- FastMCP integration with `claude --mcp-config <path>` pattern from cyberboss
