# Grill P1 — Adjudication: single-call output contract
2026-05-19 | main-session ruling on proponent vs adversary (memo, not a truth source)

Inputs: `2026-05-19_grill-p1-proponent.md`, `2026-05-19_grill-p1-adversary.md`, both read in full.

## Verdict

The isomorphic single-call contract **holds and is implementable**. Format accepted:

- One sonnet call. Input = whole-day cleaned event rows, `_fence()`-wrapped, inside the (unchanged, Lumi-owned) DIARY_PROMPT body.
- Output = `---`-segmented CN diary prose, then a hard-delimited trailing block:
  `===AFFECT===` JSON array (one object per `---` episode: `ep / valence / arousal / importance / label / entities / event_hint`) `===END===`.
- Post: split on delimiters; prose to `diary.content`; JSON to affect rows; FTS5 `event_hint` to `event_id`. Prose and affect decoupled at parse time — malformed JSON never blocks the diary write.

Why it holds: segmentation and annotation are co-produced in one pass by the same model, which kills the only real failure of a two-pass design (writer/annotator boundary mismatch). Empirically proven: real 5-17/5-18 diaries are already natural `---` 4-episode prose, produced at 171K input, `stop_reason:end_turn`.

## Must-fix before ship

1. **Post-call refusal sentinel (P0, engineering, no Lumi call).** DECISIONS:33 fallback covers JSON-parse-fail only. Policy refusal (verified real: large plain-EN prose, `stop_reason:"refusal"`, "violate our Usage Policy") is NOT covered: on refusal `is_error` may be false and `result` carries the refusal sentence — llm.py `_parse_claude` returns it as success and it lands in the diary, no fallback. Required: detect `stop_reason=="refusal"` OR a refusal fingerprint in result → treat as failure → route to the retained three-stage over-volume fallback → neutral affect + alert. The char-count guard (303K) stays as a pre-call early-exit, but the real safety net is this post-call sentinel.

2. **Affect rows cascade with the diary row (engineering constraint → hard point 4 schema).** `run_day(force=)` deletes+rewrites the diary row (diary.py ~L413); affect rows keyed to the old episode prose orphan with no alert. Required: affect rows for a date are deleted+rebuilt in the SAME txn as the diary rewrite (date-keyed cascade). `event_hint` to `event_id` FTS needs a uniqueness threshold: on multi-match, prefer NULL over first-match — a wrong link silently corrupts recall fusion, which is worse than no link. Blast radius is bounded: `event_id` feeds ONLY the recall-fusion affect-bonus (DECISIONS:27, 0.10 capped), NOT the SessionStart emotional backdrop — anchoring failure does not touch emotional continuity.

## Bundle to Lumi (effect-first, final round — not asked mid-grill)

3. **Affect-quality skew vs goal 5.** DIARY_PROMPT mandates (生动有趣) / (故事性强) and bans (流水账). Effect: emotionally significant but QUIET days (calm tenderness, low-mood silence) get written less dramatically → their importance/arousal is systematically pressed down; over weeks the affect record skews interesting-positive and under-weights quiet-significant days, eroding goal-5 emotional-continuity completeness. Root is prompt wording (affects three-layer and single-layer equally), so it does NOT block contract ①. Fix touches Lumi-owned DIARY_PROMPT wording (DESIGN L35) → her call: whether to add an affect-annotation clause to the effect of (情感重大但平淡或低谷的 episode 不得因叙事性不足而压低 importance 与 arousal).

**Lumi ruling 2026-05-19**: clause accepted; Lumi adds it herself (her DIARY_PROMPT). Direction is NOT "amplify negatives"—system never forces low/quiet days high and runs NO semantic-wrongness auto-judge; interesting-positive bias is her-curated. Resolves Adversary Attack 5(ii) by product ruling, not code safety net.

## Accepted cost — recorded, not reopened

- Multiple orthogonal affects inside one episode (5-17 lab: study-win + tease + power-joke) collapse to one valence/arousal scalar. Direct consequence of the Lumi-locked per-episode granularity. Known, accepted.
- Affect = Stellan's subjective narrative emotion, NOT objective lived emotion. Adversary framed this as the fracture; **overruled**: Marrow's diary IS the first-person lived layer (CONTEXT.md); goal 5 transfers exactly this subjective relational continuity. `source='diary'` marks provenance; the Phase-2 corrections table can override. This is the correct design choice, not a bug.

## Downstream

- Hard point ② (episode boundary): resolved by the co-produced single-pass argument; SessionStart recall uses peak-band + 7d-trend aggregate, robust to minor boundary drift. No separate ruling needed.
- Hard point ④ (schema): must encode must-fix #1 (refusal path) + #2 (date-keyed affect cascade, uniqueness-thresholded event_hint, nullable event_id).
- Hard point ⑤ / handover bundle: carries point #3.
- No second cross-questioning round: contract is implementable; #3 is a product call not answerable by more agent debate.
