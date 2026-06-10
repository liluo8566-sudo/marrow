2026-06-10

# Memory architecture — brainstorm outcome (pre-grill)

> Status: discussed + approved direction by Lumi. Grill before build.
> Facts verified this session: events ~500-600/day real rate (Lumi purged ~8k of prior 2 weeks); sqlite-vec KNN is brute-force linear scan, no ANN.

## 1. Vec rolling window (event count control)
- Raw events stay (FTS5 scales fine); vec index rolls.
- Window: 90d. Out-of-window → delete events_vec row, keep event row.
- Exempt from eviction: importance>=3 (via affect link), pinned, recall-hit revived.
- Matches existing read-time rule (recall.py:433-447 dormant exclusion) — materialise it into the index instead of scan-then-drop.
- Compensation: out-of-window semantic queries served by digest/diary vec lanes (already live). Near = fine-grained, old = coarse, by design.
- Pre-work: bench sqlite-vec scan time on live DB to pin window size (50k rows assumption unverified).

## 2. Fix broken semi-permanence link
- All 32 affect rows have event_id = NULL — importance floor mechanism designed but disconnected.
- Fix: sessionend writers populate affect.event_id. imp>=3 then auto-exempts from vec eviction + gets decay floor.

## 3. Recall injection reshape (char budget vs info value)
- Replace uniform 5 x 120 chars with score-weighted allocation:
  - top1 ~300 chars incl ±1 turn context; rank 2-3 ~120 chars no context; rank 4-5 label line ~40 chars.
  - Relative cutoff: drop rows with score < top1 * 0.6 (min_score keeps relevance gate).
- Context policy: passive layer gives context to top1 only; deeper fetch via active mcp recall (passive = index, active = fetch).

## 4. Timeline (temporal dimension)
- Exit A — SessionStart `## Timeline` block, beside Affect:
  - 72h per-session lines (15-25 chars each, HH:MM + summary).
  - Source: extra output field on existing DIGEST haiku call → new column on session_digests. Zero new LLM calls.
  - Older days: one per-day rollup line from daily.py diary call → new column on diary. Two-tier granularity: per-session <72h, per-day beyond.
- Exit B — recall time-lane:
  - Time-cue regex ((昨天/今早/上周三/N天前)...) → Melbourne-local date range → UTC → SQL window on events/digests, optional FTS keyword inside window. Deterministic, no embedding.
  - mcp recall gains since/until params for active queries.
- Exit C — visualization (cyberboss-style day/week/month page): last priority, dashboard subpage.
- Division: A = recent days always present; B = precise time lookup; vec recall = timeless semantic association.

## Prerequisite test (before A)
- Pick 3 real sessions: different topics, different lengths, incl one large.
- Dual-run haiku vs sonnet on same transcript for timeline line; blind compare.
- Haiku fails → timeline line field moves to a sonnet call (one line, cost negligible).
