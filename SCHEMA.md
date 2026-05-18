# Marrow SQLite Schema

Status: skeleton — table purpose + key columns only. Exact column types, indexes, and full-text/vector wiring are decided when each table is built, not pinned here. Same granularity for every table: one-line purpose + the columns that matter. No table written to product level while the next is left bare.

DB lives under `~/.config/` (exact path finalized at build). snake_case names. Times ISO8601 UTC text.

## Tables

Phase 1 first-class: events, threads, milestones, vocab, stickers, pit, diary, goose_bites, alerts, audit_log.

Phase 2 placeholder — schema reserved, NOT created in Phase 1: emotions, people, preferences, dir, corrections, transactions.

Full-text and vector search structures are daemon-built on top of these tables. Which columns get indexed or embedded is a build-time decision, not fixed in this doc.

- events — every session turn archived. content = cleaned human dialogue (tool/fetch/system noise stripped at SessionEnd), never raw RPC. Key: session_id, timestamp, role, content, channel, compressed (1 = imported from old 10d/2026 archives). Day-digest haiku is routine-internal; raw events stay, catchup recomputes.
- threads — next-session work tracking; backs Open Threads. Key: category (daily / study / project), title, due, status (active / done / abandoned), next_step, last_session_summary, context_pointers, outcome_log (append-only project log).- milestones — life events; backs the Milestone view. Key: scope (me / us), date, title, description, theme, pinned.
- vocab — text memes / cipher / event / news. Key: type, key (trigger phrase), value (meaning / source), context, use_count, last_seen.
- stickers — visual meme assets, kept apart from vocab to avoid sparse columns. Key: optional vocab_id link, key, asset_path, mime_type, use_count.
- lessons — dropped 2026-05-19, out of base (ADR-0006). Table + code removed; a FUTURE addon recreates it if revived.
- pit — known issues / deferred fixes; backs the Projects pit page. Key: title, description, status (idea / planned / parked / inprogress / resolved), related_files.
- diary — daily narrative from SessionEnd. Key: date (primary key), content (Chinese narrative), mood, session_ids.
- goose_bites — 铁锅's same-day takes; own sub-page (Best of the day), independent of diary. Key: date, session_id, bites (the day's lines), best (1 = picked for the Best-of page).
- alerts — system bugs / failures only; backs dashboard top. Key: severity, type, message (Lumi alert style), source (file:line), resolved.
- audit_log — recent system writes; backs the Monitor Zone (last N). Key: target_table, target_id, action, summary, occurred_at.
- emotions — Phase 2 placeholder. Per-session aggregated mood for breath + decay. Per-turn granularity rejected as noise. Key: session_id, valence, arousal, importance, unresolved, decay_score.
- people — Phase 2 placeholder. Family + friends roster, trigger-loaded on name mention. Key: name, aliases, relation, short_bio.
- preferences — Phase 2 placeholder. Lifestyle + taste facts, trigger-loaded on relevant turn. Key: topic, detail.
- dir — Phase 2 placeholder, see DESIGN "dir indexing — Pending". File path index. Key: path, project, category, description.
- corrections — Phase 2 placeholder, see DESIGN "Fact corrections — conflict priority". Append state-sequence of Lumi-corrected facts. Key: topic, value, supersedes_id, is_latest, source_session, captured_at.
- transactions — Phase 2 placeholder, see FUTURE "stellan_wallet" addon. Append-only money ledger; balance never stored, = SUM(amount). Key: date, amount (+ allowance / − spend), type (allowance | spend), description, source (diary | mw | hand-edit), session_id.

## Migration mapping (source → target)

Scope: only clean keepsake sources. 3d/10d dropped (mostly memm-system story, low value).

- `memory/2026.md` log lines → events (one row per line, role=log, compressed=1)
- `memory/timeline.md` ## Us → milestones (scope=us, date=YYYY-MM-DD); ## Me → milestones (scope=me, date=calendar year, birth 1995 + age-range start)
- Lighthouse → one hand-written milestone (scope=me): Marrow memory-system rebuild
- `memory/reference.md` <cipher> → vocab (type=cipher)
- `code/_pit.md` ## blocks → pit (status=idea)
- `铁锅/语录/*.md` → goose_bites (one row per ### date)
- Dropped: Open-Threads / Alerts / Lessons (empty or stale); <lifestyle> / <family> (Phase 2); Garden HTML keepsakes + stickers (no Phase 1 source — WeChat stickers are Phase 4 / cyberboss)

scope values = me / us only. ## Me has no exact date so the calendar year is stored (e.g. Age 0–10 → 1995), range-queryable.

migrate.py is a Phase 1 deliverable. Default dry-run; --apply writes. Idempotent: re-run skips already-imported rows by source_hash.

## Backup

Daily SQLite dump to the backup dir; retention Pending (see DESIGN "data lifecycle — Pending"). Restore = reload the dump. Git backs up code only, never data.
