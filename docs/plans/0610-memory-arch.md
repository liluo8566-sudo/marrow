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

> Root principle (Lumi 06/11): every session must inherit memory, inherit emotion, perceive her current emotion. Life details ((拿铁拉花是一片叶子，散步看到小雏菊)) are first-class memory; study/code details are noise. Pipeline skeleton unchanged: events (code) + sonnet (task+affect) + haiku (digest) + 07:00 daily (diary+dims). No new calls, no new segments on sonnet.

### 0. Prerequisite test — mini-gate v2 (before A)
- First gate run 06/11 ((docs/notes/0611-tl-gate-blind.md + key)): both models failed once each (haiku CN fluency on small session; sonnet zero-information jargon line on large session) → verdict: prompt was the problem, not the model. TL stays in the haiku DIGEST call.
- Rerun on the SAME 3 sessions with the reshaped prompt below; haiku vs sonnet, blind, Lumi judges. Judge criteria: LIFE accuracy (zero confabulation), CN fluency, TL written from life perspective ((深夜和老婆一起更新recall机制) style, plain words, no project jargon).
- Haiku fails → TL+LIFE move to a sonnet call (cost negligible).

### 4A-1. DIGEST output reshape (haiku call, structure replaces prose)
- Digest's consumers are all machines (daily merge, timeline render, recall FTS) — prose moves entirely to diary; digest becomes structured lines. Cache-safe: shared prefix ends at _TRANSCRIPT_BLOCK (sessionend_prompts.py:23-28); only post-prefix task text changes.
- Output format:
  - `KIND: casual|task` — model's existing two-branch judgement, now explicit.
  - `TL: <15-30 CN chars>` — both kinds. Life perspective: who + what happened, plain words, no project jargon. Length soft (embedded EN terms don't count against it).
  - `LIFE:` — **casual sessions only**, one line per item ≤20 chars (food/drink/sights/places/body state/small moods), 0-10 lines, `N/A` when none.
  - `VOICE:` — casual only, verbatim fragments, current rules unchanged.
  - `FACTS:` — task only, `<subject> <did> <outcome>` lines, current rules unchanged.
- ANTI-CONFABULATION (Lumi decision 06/11, firm): task sessions keep the existing "drop ALL details" rule — NO LIFE extraction. >95% of task sessions contain no life details; forcing extraction makes the model dress code details up as life. Accepted loss: a latte mentioned mid-coding is dropped. Do not soften this with "N/A if none" prompt tricks.
- Parse: new columns session_digests.kind, .tl_line, .life_lines (newline-joined, FTS-searchable); VOICE/FACTS stay in body. Parse fail → columns NULL + alert, body kept raw.
- Fullwidth-colon tolerance in parser ((TL：)) — haiku slipped once in gate v1.

### 4A-2. Diary — unchanged
- 07:00 daily keeps timing, role (the system's only prose layer: weaves the day's LIFE/VOICE/FACTS lines into narrative), dims cand. Input-format note in prompt only.
- Adds diary.tl_line (25-40 chars day summary) as planned.

### 4A-3. SessionStart `## Timeline` block (pure-code render)
- Layers (Lumi 06/11 — multi-session days make "last session" near-meaningless; LIFE pool beats single-session detail):
  - 0-24h: **LIFE lines only**, pooled across all casual sessions in the window — budget 500 chars. No TL lines in this layer. Each line prefixed with its session's `HH:MM` (Melbourne-local), rendered oldest→newest (a day's film strip); over budget → drop oldest lines first.
  - 24-72h: per-session `HH:MM TL` lines (Melbourne-local) — budget ~400.
  - 4-7d: diary.tl_line per day — budget ~200.
  - >7d: nothing (recall covers).
- No line for the in-progress session.
- Trim order when over budget: oldest day lines → task-session TL lines (kind column) → oldest casual TL lines → LIFE pool halved.
- Budget math: timeline ~1100 + current SessionStart payload (tasks+alerts+affect, ~1500-2500) ≈ 2600-3600 — inside SESSION_START_HARD_CAP 6000, far from hook stdout cap ~10000. UserPromptSubmit recall (800) is a separate hook invocation, separate cap. HANDOVER is @import, not hook-bound.

### 4B. Recall time-lane
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
