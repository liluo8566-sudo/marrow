2026-06-10

# Memory architecture — grilled plan (build-ready)

> Status: grilled 2026-06-10 23:15. Approved by Lumi pending one veto point (§3 budget 600→800).
> Facts: events ~500-600/day real rate; sqlite-vec KNN = brute-force linear scan, no ANN; embed = bge-m3 ONNX, 1024-dim (recall.py:37,71).
> Dispatch: all coding via sonnet worktree agents; fable plans/reviews only.

## Batch 1 — Recall injection reshape + relative time (renderer layer only)

### 3. Score-weighted char allocation
- Replace uniform 5 x event_max_chars=120 with per-rank caps: [300, 120, 120, 40, 40].
  - top1: ~300 chars incl ±1 adjacent turns (only rank with context).
  - rank 2-3: ~120 chars snippet, no context. rank 4-5: one label line ~40 chars.
- Relative cutoff: drop rows with score < top1 * 0.6 (config key, default 0.6). min_score stays as absolute gate.
- budget_chars: live 600 → 800 (caps sum 620 would clip; 800 = default.toml value). LUMI VETO POINT.
- Context policy: passive = index (top1 only gets context); active mcp recall = fetch (param to request full context on all rows).
- Anchor lanes (milestones/memes/entities): same rank-cap downgrade applies; they never had ±1 context (cards, not turns).
- Where: hooks.py:947-1022 render loop + recall.py limit plumbing.

### 4-display. Relative time on recall hits
- Render ([06-08 Mon · 2d ago]) — absolute + relative, one shared formatter, used by passive hook + mcp recall.
- Deterministic render-time code; Melbourne-local for display, UTC stays in DB.

## Batch 2 — Affect link, then vec window (strict order)

### 2. Fix semi-permanence link (affect.event_id all NULL)
- Found: TASK_AFFECT already outputs event_hint per episode (sessionend_prompts.py:152-157) but seg_affect drops it (never read). Zero prompt change needed.
- Fix in seg_affect (sessionend_writers.py:56-122): match event_hint (fallback: description) against this session's events rows (FTS phrase then substring); best-match turn id → affect.event_id. No match → NULL (graceful).
- No backfill of existing rows: first eviction is ≥90d after window ships; they age out of relevance naturally. Milestones carry the permanent layer regardless.

### 1. Vec rolling window
- Raw events stay forever (FTS5 scales); vec index rolls. Out-of-window → delete events_vec row + meta, keep event row.
- Window: 90d default, pinned by bench (synthetic 50k x 1024-dim KNN timing script, run before sizing; config key for window days).
- Exempt from eviction: linked affect importance>=3 · recall_count>0 (see infra below).
- New columns events.recall_count + events.last_recalled_at — updated best-effort on recall hit (passive + active). Shared infra: eviction exemption now, §5 recall-hit boost later.
- Evicted rows: still FTS-searchable; no re-embed/return path (digest lane covers old-range semantic queries: near = fine, old = coarse, by design).
- Where: aging.py new pass in existing Sunday weekly transaction; matches read-time rule recall.py:433-447 (materialise scan-then-drop into the index).

### Safety nets (Batch 2 — eviction is the only destructive step)
- Evict = DELETE events_vec row + events_vec_meta row together, same transaction. Meta left behind = embed_pending thinks row is embedded forever = unrecoverable hole (same orphan class as todo Audit 3 — fix together).
- Reversible by design: events row intact → clearing meta deliberately lets embed_pending re-embed. Document the recovery command in MAP.
- Run cap: single aging run evicting >25% of vec rows or >10k rows → abort whole pass + critical alert (mass-evict = bug, not aging).
- Backup gate: skip destructive pass + warn alert if last DB backup older than 7d (mw-db-backup.plist exists — verify freshness, don't assume).
- Audit: one audit_log row per run — evicted N, exempted M (by reason), duration.
- event_hint match: ambiguous (multiple equal hits) → NULL, never guess; matched pairs logged to audit_log for spot-checking.
- recall_count/last_recalled_at updates: best-effort try/except — a stats write must never block or fail the recall path.
- Rollback levers are all config: budget/caps/cutoff/window-days revert by config edit, no code revert needed.

## Batch 3 — Timeline + digest reshape (test first, then A, then B)

> Root principle (Lumi 06/11): every session must inherit memory, inherit emotion, perceive her current emotion. Life details ((拿铁拉花是一片叶子，散步看到小雏菊)) are first-class memory; study/code details are noise.
> Pipeline SUPERSEDED (Lumi 06/11 evening): sessionend merges to ONE sonnet call — TASKS/AFFECT/KIND/TL/LIFE/VOICE/FACTS in one output, one transcript read (input tokens halved, outputs same-source). The earlier "no new segments on sonnet" rule is replaced by this merge; attention risk goes to gate v3 (3 old sessions, blind, Lumi judges; fail → revert to two calls).

### 0. Prerequisite test — mini-gate v2 (before A)
- First gate run 06/11 ((docs/notes/0611-tl-gate-blind.md + key)): both models failed once each (haiku CN fluency on small session; sonnet zero-information jargon line on large session) → verdict: prompt was the problem, not the model. TL stays in the haiku DIGEST call.
- Rerun on the SAME 3 sessions with the reshaped prompt below; haiku vs sonnet, blind, Lumi judges. Judge criteria: LIFE accuracy (zero confabulation), CN fluency, TL written from life perspective ((深夜和老婆一起更新recall机制) style, plain words, no project jargon).
- Haiku fails → TL+LIFE move to a sonnet call (cost negligible).
- RESULT (06/11, docs/notes/0611-tl-gate-v2-*): zero confabulation, zero LIFE-on-task violations across 6 runs. Haiku failed the study session (EN-jargon TL, missed LIFE, thin VOICE); sonnet's only fault was FACTS verbosity = missing word cap in the test prompt. GATE PASSED → DIGEST call goes sonnet, with TL+FACTS ≤120 words on task sessions.

### 4A-1. DIGEST output reshape (haiku call, structure replaces prose)
- Digest's consumers are all machines (daily merge, timeline render, recall FTS) — prose moves entirely to diary; digest becomes structured lines. Cache-safe: shared prefix ends at _TRANSCRIPT_BLOCK (sessionend_prompts.py:23-28); only post-prefix task text changes.
- Output format:
  - `KIND: casual|task` — model's existing two-branch judgement, now explicit.
  - `TL: <15-30 CN chars>` — both kinds. Life perspective: who + what happened, plain words, no project jargon. Length soft (embedded EN terms don't count against it).
  - `LIFE:` — **casual sessions only**, one line per item ≤20 chars (food/drink/sights/places/body state/small moods), 0-10 lines, `N/A` when none.
  - `VOICE:` — casual only, verbatim fragments, current rules unchanged.
  - `FACTS:` — task only, `<subject> <did> <outcome>` lines, max 5; task-session total (TL + FACTS) hard cap 120 words — gate v2 showed sonnet writes essays when the original 150-word cap is omitted (the cap was the working part of the old prompt; keep it).
- No tone/emotion labels anywhere in digest (Lumi 06/11): affect episodes already carry tone. daily.py instead injects the day's affect rows (with eph/epl marks) into the diary call input — deterministic code-side pass-through, no model re-extraction.
- AFFECT segment (merged call, Lumi 06/11 evening): V/A + label + eph/epl unchanged in substance, plus per-episode `open` flag for unresolved emotion (quarrel un-coaxed, anxiety pending, awaiting result). Episode wording: her perspective, near-verbatim, plain words (Lumi dissatisfied with current sonnet episode summaries — gate v3 judges this too).
- Model: DIGEST call moves haiku → sonnet (gate v2 verdict 06/11: haiku failed the study session — EN-jargon TL, missed LIFE, thin VOICE; study/chat quality is the core goal, task layer matters least). FACTS cap above guards sonnet's verbosity.
- ANTI-CONFABULATION (Lumi decision 06/11, firm): task sessions keep the existing "drop ALL details" rule — NO LIFE extraction. >95% of task sessions contain no life details; forcing extraction makes the model dress code details up as life. Accepted loss: a latte mentioned mid-coding is dropped. Do not soften this with "N/A if none" prompt tricks.
- Parse: new columns session_digests.kind, .tl_line, .life_lines (newline-joined, FTS-searchable); VOICE/FACTS stay in body. Parse fail → columns NULL + alert, body kept raw.
- Fullwidth-colon tolerance in parser ((TL：)) — haiku slipped once in gate v1.

### 4A-2. Diary — unchanged
- 07:00 daily keeps timing, role (the system's only prose layer: weaves the day's LIFE/VOICE/FACTS lines into narrative), dims cand. Input-format note in prompt only.
- Adds diary.tl_line (25-40 chars day summary) as planned.

### 4A-3. `## Timeline` block — merged affect+events view, FINAL format (Lumi 06/11 evening; supersedes layer spec above)
- Replaces BOTH the old timeline layers and the SessionStart `## Affect` block — one merged view, render-layer JOIN only (affect + session_digests + diary tables unchanged). One render fn, two outlets: SessionStart hook + dashboard block.
- Top: `> 未解:` line(s) — episodes flagged open, until closed by a later AFFECT segment / Lumi deletes on dashboard / 7d expiry.
- Last 24h: flat film-strip, newest→oldest, `HH:MM` lines (LIFE lines from casual + TL line per task session), session's first line carries its 【tone】; day crossings get a `--- MM-DD ---` divider; cap 15 lines, over → drop farthest.
- 24-72h: per-day `**MM-DD Day 【tone】**` header + up to 3 period lines `AM`(6-12) `PM`(12-18) `ND`(18-next 06); ND 0-6 belongs to the PREVIOUS day. Period line = all sessions' TLs in that period (any kind) joined by time order, truncated; empty period hidden. Cap ~12 lines incl. headers.
- Day 4-7: `Week 【tone ↗/↘/→】` trend line (this-week vs last-week V/A mean, code-side) + one line per day `MM-DD Day 【tone】 diary.tl_line`.
- Tone labels: V/A mean per scope (session / period-day / day / week) via existing split-tone mapping. NO episodes in the injection (Lumi field test: episodes in SessionStart get ignored; recall is their outlet — affect recall lane parked in §5).
- No line for the in-progress session.
- Trim order: day lines → period lines (farthest day first) → Last-24h farthest lines.
- Budget math: timeline ~1100 absorbs the retired Affect block (~300) — SessionStart total well inside HARD_CAP 6000.

### 4A-4. Dashboard timeline block + edit-back (pulled forward from §4C)
- Same render fn as injection. Editable: tl text via line anchors `<!-- tl:sid -->` / `<!-- tl:d:YYYY-MM-DD -->` → reconcile writes back session_digests.tl_line / diary.tl_line. Tone/label segments display-only. Deleted line = no-op (next render restores; timeline is a window view, no resolve semantics).
- Fix existing affect gap (Lumi 06/11): reconcile diffs `<!-- aff:ids -->` id-set; id removed from anchor + its segment deleted → mark that affect row superseded (no physical delete). Currently text edits work but single-episode deletion is impossible.

### 4B. Recall time-lane — DONE 06-11 (timecue.py + window params; coarse path reads session_digests.text until Batch 3 lands tl_line)
- Passive: time-cue regex ((昨天/今早/昨晚/前天/上周X/周X/N天前/X月X号)... enumerate + unit tests at impl) → Melbourne-local range → UTC → SQL window.
- Window query: no keyword → session_digests/tl lines (coarse); with keyword → FTS events inside window (fine).
- Merge: time-lane hits take top injection slots (deterministic beats probabilistic), then semantic hits fill remaining budget.
- Active: mcp recall gains since/until (Melbourne natural-day strings, converted internally).
- Canonical case: (你还记得我上周说过灭绝师太说了xxx) → recall("灭绝师太", since/until=last week) → window-first then FTS/vec. Without window, recency 0.15 can't outrank older high-score hits of a recurring term.

### 4C. Visualization + edit (cyberboss-style day/week/month page)
- Dashboard subpage, deterministic render. Design alongside Batch 3 build (Lumi 06/11): tl_line display AND edit path — correcting/rewording a session or day line from the dashboard writes back to session_digests.tl_line / diary.tl_line.
- Build can trail Batch 3 A/B, but schema + edit flow decided together so columns don't need rework.

## Later — §5 Retrieval quality backlog
- Needle extraction for passive events FTS (whole-query phrase-quoting near-always misses conversational queries; reuse _expand_needles idea, 2-4 low-freq terms).
- Nickname/abbrev/CN-EN: FTS + entities alias path, not bge. Treat with parked enrich-at-insert item [06/08]. Recurring persons → entities rows with aliases.
- Recall-hit boost: recall_count (infra lands in Batch 2) feeds score bias; frequently-recalled drifts toward semi-permanent.
