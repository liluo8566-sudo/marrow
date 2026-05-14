# NY SQLite Schema v1

Database `~/.config/ny/ny.db`. Extensions FTS5, sqlite-vec. snake_case tables and columns. Times ISO8601 UTC TEXT.

## Tables

Phase 1 first-class: events / threads / milestones / vocab / stickers / lessons / pit / diary / alerts / audit_log / vec.
Phase 2 placeholders (schema retained, NOT created in Phase 1): emotions / people / preferences / dir.

### events
All session turn archives.
- id INTEGER PRIMARY KEY AUTOINCREMENT
- session_id TEXT NOT NULL
- thread_id INTEGER REFERENCES threads(id) ON DELETE SET NULL
- timestamp TEXT NOT NULL
- role TEXT NOT NULL — user / assistant / system
- content TEXT NOT NULL
- tags TEXT — JSON array
- channel TEXT — cli / wechat / web
- token_count INTEGER
- compressed INTEGER NOT NULL DEFAULT 0 — 1 when imported from existing 10d / 2026 archives

Indexes: session_id, timestamp, thread_id. FTS5 virtual table `events_fts(content)`.

### emotions (Phase 2, placeholder schema — NOT created in Phase 1)
Per-session aggregated emotional metadata. Per-turn granularity rejected as noise.
- session_id TEXT PRIMARY KEY
- valence REAL — 0..1, low = negative, session-aggregated
- arousal REAL — 0..1, low = calm, session-aggregated
- importance INTEGER — 1..10
- unresolved INTEGER NOT NULL DEFAULT 0
- pinned INTEGER NOT NULL DEFAULT 0
- decay_score REAL — computed daily by decay engine
- activation_count INTEGER NOT NULL DEFAULT 0
- last_active TEXT
- summary TEXT — one-line mood summary

Indexes: unresolved, pinned, decay_score DESC.

### threads
Next-session-tracking work threads.
- id INTEGER PRIMARY KEY AUTOINCREMENT
- category TEXT NOT NULL — daily / study / project
- title TEXT NOT NULL
- subject_code TEXT — subject identifier
- due TEXT — nullable ISO date
- status TEXT NOT NULL DEFAULT 'active' — active / done / abandoned
- created_at TEXT NOT NULL
- last_session_summary TEXT
- next_step TEXT
- context_pointers TEXT — JSON of paths, event_ids, vocab_ids
- last_session_id TEXT
- last_active TEXT NOT NULL
- outcome_log TEXT — append-only project outcome / decision log; survives across sessions; rendered into projects/<title>.md

Indexes: category, status, due, last_active. FTS5 `threads_fts(title, next_step, last_session_summary, outcome_log)`.

### milestones
Life events. Backs 纪念册 view.
- id INTEGER PRIMARY KEY AUTOINCREMENT
- scope TEXT NOT NULL — me / us
- date TEXT NOT NULL
- title TEXT NOT NULL
- description TEXT
- theme TEXT — first / travel / 日常温暖 / 大事 / award / health
- pinned INTEGER NOT NULL DEFAULT 0
- created_at TEXT NOT NULL

Indexes: scope, date, theme. FTS5 `milestones_fts(title, description)`.

### vocab
Text-only entries: cipher / event / news / meme (text quote portion). Visual stickers live in the separate `stickers` table.
- id INTEGER PRIMARY KEY AUTOINCREMENT
- type TEXT NOT NULL — meme / cipher / event / news
- key TEXT NOT NULL — trigger phrase / 缩写 / 关键词
- value TEXT NOT NULL — content / 真实含义 / 出处
- context TEXT — when to use, intent
- last_seen TEXT
- use_count INTEGER NOT NULL DEFAULT 0
- created_at TEXT NOT NULL

Indexes: type, key. FTS5 `vocab_fts(key, value, context)`.

### stickers
Visual meme assets (gif / jpg / png / webp). Separated from `vocab` to avoid sparse-column rot.
- id INTEGER PRIMARY KEY AUTOINCREMENT
- vocab_id INTEGER REFERENCES vocab(id) ON DELETE SET NULL — link if an associated text vocab entry exists
- key TEXT NOT NULL — trigger phrase / scene description
- asset_path TEXT NOT NULL — absolute path under `~/.config/ny/stickers/`
- mime_type TEXT — image/gif / image/png / image/jpeg / image/webp
- use_count INTEGER NOT NULL DEFAULT 0
- last_seen TEXT
- created_at TEXT NOT NULL

Indexes: vocab_id, key. FTS5 `stickers_fts(key)`.

Files dropped into `~/.config/ny/stickers/` by Lumi are picked up by daemon's filesystem scan and inserted with `key = filename` placeholder. Lumi promotes via `ny sticker tag <id> "<key>" [--vocab-id N]`.

### dir (PENDING — placeholder, NOT created in Phase 1)
File path index. See DESIGN.md "dir indexing — PENDING" for current decision: Layer 1 hand-maintained, leaf-level via `mdfind`. Schema retained for Phase 2/3 re-design.
- path TEXT PRIMARY KEY — absolute path
- parent TEXT — parent directory
- project TEXT — NY / Study / cc-lab / Toolkit / Garden / Document / Desktop
- category TEXT — assignment / source / config / archive / template / note / media
- description TEXT
- file_type TEXT — extension
- size_bytes INTEGER
- mtime TEXT
- last_indexed TEXT NOT NULL

Indexes: project, category, parent. FTS5 `dir_fts(path, description)`.

### pit
Known issues, workarounds, pending fixes.
- id INTEGER PRIMARY KEY AUTOINCREMENT
- title TEXT NOT NULL
- description TEXT
- status TEXT NOT NULL DEFAULT 'idea' — idea / planned / parked / inprogress / resolved
- related_files TEXT — JSON
- encountered_at TEXT NOT NULL
- resolved_at TEXT

Indexes: status. FTS5 `pit_fts(title, description)`.

### lessons
Captured Lumi corrections + drift catches. Closes Goal 4. Phase 1 first-class table.
- id INTEGER PRIMARY KEY AUTOINCREMENT
- date TEXT NOT NULL
- session_id TEXT
- scope TEXT NOT NULL — interaction / coding / memory / hook / prompt / language
- lesson_text TEXT NOT NULL — what got missed or wrong, and what's correct
- promoted_to_rule INTEGER NOT NULL DEFAULT 0
- rule_path TEXT — file:line if promoted into rule.md / CLAUDE.md / chat-lint forbidden.yaml
- created_at TEXT NOT NULL

Indexes: scope, promoted_to_rule, date DESC. FTS5 `lessons_fts(lesson_text)`.

### people (Phase 2, placeholder — NOT created in Phase 1)
Family + friends roster. Trigger-load on name mention by UserPromptSubmit hook.
- id INTEGER PRIMARY KEY AUTOINCREMENT
- name TEXT NOT NULL — primary name (e.g. 妈妈 / 李小云 / Summer / BBB / 南南)
- aliases TEXT — JSON array of alt names
- relation TEXT — family / friend / colleague / other
- short_bio TEXT — one-paragraph context block injected on mention
- last_mention TEXT
- created_at TEXT NOT NULL

Indexes: name. FTS5 `people_fts(name, aliases, short_bio)`.

### preferences (Phase 2, placeholder — NOT created in Phase 1)
Lifestyle + taste facts. Trigger-load on relevant turn.
- id INTEGER PRIMARY KEY AUTOINCREMENT
- topic TEXT NOT NULL — fashion / colour / food / scent / entertainment / exercise / travel / aesthetic
- detail TEXT NOT NULL — fact body
- created_at TEXT NOT NULL

Indexes: topic.

### diary
Daily narrative entry, generated by SessionEnd.
- date TEXT PRIMARY KEY
- title TEXT
- content TEXT NOT NULL — Chinese narrative-first per existing daily template
- mood TEXT
- generated_at TEXT NOT NULL
- session_ids TEXT — JSON of contributing SIDs

Indexes: date DESC. FTS5 `diary_fts(content)`.

### alerts
System bug and failure messages. Shown on dashboard top.
- id INTEGER PRIMARY KEY AUTOINCREMENT
- severity TEXT NOT NULL — info / warning / error
- type TEXT NOT NULL — hook_fail / script_exception / quota_warn / drift / timeout
- message TEXT NOT NULL — short phrase, Lumi alert style
- source TEXT — file:line
- occurred_at TEXT NOT NULL
- resolved INTEGER NOT NULL DEFAULT 0
- resolved_at TEXT

Indexes: resolved, severity, occurred_at DESC.

### audit_log
Recent system writes for dashboard transparency.
- id INTEGER PRIMARY KEY AUTOINCREMENT
- target_table TEXT NOT NULL
- target_id INTEGER
- action TEXT NOT NULL — insert / update / delete
- summary TEXT NOT NULL — one-line
- occurred_at TEXT NOT NULL

Indexes: occurred_at DESC.

### vec (sqlite-vec virtual table)
Embedding store for semantic recall.
- rowid INTEGER PRIMARY KEY
- source_table TEXT NOT NULL
- source_id INTEGER NOT NULL
- embedding BLOB NOT NULL — 384 dim, sentence-transformers MiniLM

Triggers on insert/update of content fields in events, vocab, milestones populate vec asynchronously.

## Write paths
- events: SessionEnd hook async, batched per session
- emotions: SessionEnd hook. Layer 1 yaml-rule scan, Layer 2 Ollama fallback at Phase 2
- threads: Claude main via MCP `thread_update` + `memory_append("threads", ...)`. `ny thread add` CLI for manual entries
- milestones: Claude main auto-detect on milestone-like turns. `ny milestone add` CLI manual
- vocab: Claude main auto-detect on novel term. `ny vocab add` CLI manual
- dir: dir_watcher daemon (watchdog fs events) + cron daily rescan + grep fallback for cold dirs
- pit: Claude main on bug encounter + `ny pit add` CLI
- diary: SessionEnd hook spawns subprocess Claude with strict prompt template
- alerts: any hook or script via daemon `alert(severity, type, message)` on exception path
- audit_log: auto-logged by daemon on every insert/update/delete via shared helper
- vec: trigger on source row mutation, async embed via local sentence-transformers

## Read paths
- events: Claude main via `memory_query`
- emotions: SessionStart breath
- threads: SessionStart injects active threads into system prompt + dashboard top
- milestones: dashboard link 纪念册 / milestones view
- vocab: Claude main auto-lookup on unknown term
- dir: Claude main via `memory_query_dir`
- pit: dashboard link
- diary: dashboard link → date picker
- alerts: dashboard top
- audit_log: dashboard bottom Recent Writes
- vec: `memory_query` semantic fallback when keyword recall < 3 hits

## Migration mapping (source → target)
- `~/Desktop/NY/memory/3d.md` per-day entries → events (compressed=0)
- `~/Desktop/NY/memory/10d.md` daily blocks → events (compressed=1)
- `~/Desktop/NY/memory/2026.md` monthly blocks → events (compressed=1)
- `~/Desktop/NY/memory/timeline.md` ## Me → milestones (scope=me)
- `~/Desktop/NY/memory/timeline.md` ## Us → milestones (scope=us)
- `~/Desktop/NY/memory/reference.md` cipher block → vocab (type=cipher)
- `~/Desktop/NY/memory/reference.md` event block → vocab (type=event)
- `~/Desktop/NY/memory/reference.md` lifestyle block → profile.md (manual review)
- `~/Desktop/NY/memory/reference.md` dir block → dir table (replaced by watchdog)
- `~/Desktop/NY/code/_pit.md` items → pit table
- `~/Desktop/NY/铁锅/语录/*.md` quotes → vocab (type=meme, value column, asset_path NULL)
- `~/Desktop/NY/Garden/*.gif`, `*.jpg`, `*.png` (manual review) → copy into `~/.config/ny/stickers/` + vocab (type=meme, asset_path set)
- `~/Desktop/NY/memory/3d.md` Open-Threads → threads table
- `~/Desktop/NY/memory/3d.md` Alerts → alerts table
- `~/Desktop/NY/memory/3d.md` Lessons → lessons table (Phase 1; currently empty, no migration weight)
- `~/Desktop/NY/memory/reference.md` `<lifestyle_and_preference>` → preferences table (Phase 2; manual review)
- `~/Desktop/NY/memory/reference.md` family + friend mentions across blocks → people table (Phase 2; manual review)

Migration script: `~/.ny/scripts/migrate.py`. Phase 1 deliverable. Idempotent: re-run skips already-imported rows by source-hash.

## Indexes summary
Per-table indexes listed above. Composite indexes added as query patterns emerge in Phase 1 testing.

## Embedding strategy
- Model: sentence-transformers MiniLM-L6-v2, 384 dim, local CPU inference
- Triggered on: events.content, vocab.value, milestones.description insert/update
- Stored in: vec virtual table
- Queried by: `memory_query` when FTS5 hits below threshold

## Backup
- Daily cron: `sqlite3 ~/.config/ny/ny.db .dump > ~/.config/ny/backup/ny-$(date +%Y%m%d).sql`
- Retention: 30 days
- Restore: `sqlite3 ny.db < ny-YYYYMMDD.sql`
- Git repo backs up code only, not data
