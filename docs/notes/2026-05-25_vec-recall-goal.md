# Goal — recall cross-table vec lanes + entity tiebreaker

Acceptance: all items below must pass.

## Outcome 1 — memes / entities / milestones vec lanes online

- db gains three sqlite-vec tables: `memes_vec` / `entities_vec` / `milestones_vec` (bge-m3 / 1024d, aligned with `events_vec`)
- each table row count == its main table row count (full backfill, no gaps)
- `recall.recall_fusion` wires the three lanes into RRF / weighted fusion alongside `events_vec`
- recall config adds weight knobs for the new lanes (sane defaults are fine)

Acceptance probes:
- query (赎身费) → recall output includes a memes row
- query (我的猫) → recall output includes entity card (小胖); query does NOT mention the alias, must hit via cross-table vec
- query (在一起) → recall output includes the milestone (2026-01-17)

## Outcome 2 — entity card same-score tiebreaker

- `marrow/entity_recall.py` outputs entity cards with a valid timestamp (non-empty, non-zero)
- when two entity cards share the same score (e.g. equal mention_count), order by timestamp DESC (newest first)
- pytest covers this: mock two equal-score entities, assert order

## Out of scope

- do NOT touch `daily_prompts.py` / `sessionend_prompts.py`
- do NOT touch `handover.md` / `handover_render.py`
- keep the alias field

## Reference

- `marrow/recall.py:106-180` — events_vec embed write path
- `marrow/recall.py:360-485` — recall_fusion weighted logic
- `marrow/entity_recall.py` — entity recall
- `marrow/migrate.py` — schema bump entry point
- commit `410c250` — handover rename bug (no fix landed)

Pass gate: paste the verify command output (row counts + acceptance probe hits) in the next session for morning review.
