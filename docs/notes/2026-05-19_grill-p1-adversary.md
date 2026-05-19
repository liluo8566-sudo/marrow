# Adversary -- Grill P1: Single-call diary+affect contract

> Role: ADVERSARY. Attacks the isomorphic-episode hypothesis.
> Locked: per-episode affect granularity, ONE sonnet call, bge-m3 in-process embedding.
> Target: whether the single-call output contract can actually be made to work.

---

## Attack 1 -- Creative/analytic contamination

DIARY_PROMPT demands: (散文段落 / 论坛体 / 对话片段) + (【心理活动】) + (叙述生动有趣，故事性强).
Contract widens (DECISIONS.md L29): `{diary, affect:[{label, valence, arousal, importance, event_hint}]}`.

Literary mode (compress, reorder, embellish) conflicts with structured mode (analytical precision re: source-turn mapping).

5-17 (下午生殖系统实验课) episode example: one `---` block is simultaneously study session (moderate positive) + sexual tease (high arousal) + power-joke (playful). Single `{valence: 0.8, arousal: 0.7}` conflates three orthogonal affects; cannot be decomposed from prose.

DIARY_PROMPT weights prose (40+ lines of (违禁词/范文) rules) vs zero weighting for JSON annotation. Combined prompt cannot balance without rewrite affecting Lumi-owned wording (DESIGN L35).

---

## Attack 2 -- Episode-boundary subjectivity + forced-rerun orphan

Hypothesis: diary `---` segmentation IS episode segmentation.

5-17 (stellar wallet + bridge bug) example: one `---` block contains two distinguishable affect signatures -- productive cooperation (V~0.7/A~0.6) and frustration (V~0.3/A~0.7).

`---` breaks are placed by narrative feel, not semantic rule. Segment count varies across runs. When `run_day --force` deletes and rewrites diary, episode segmentation may change. Prior affect rows keyed to old episode prose become orphaned; no alert fires.

`event_hint` to event_id anchoring via FTS: diary prose is sonnet's paraphrase of stitch (itself haiku paraphrase of haiku digests). Phrases in diary prose rarely match raw turn text verbatim; FTS frequently misses or multi-matches. When multi-match occurs, first match wins, linking affect to wrong session.

---

## Attack 3 -- Single point of failure and policy boundary

Three-layer pipeline (diary.py L1-13): sonnet failure loses only the diary call; stitch strand and partial session digests survive. Under single-call contract, one failure loses diary + all affect rows for the day + entity extractions.

Fallback (DECISIONS.md L33) only fires on JSON parse failure. Does not cover: model refusal, timeout kill, truncated response.

Refusal boundary is real (~136K plain EN prose); 5-18 material passed at 171K (CN-dominant). English-heavy day (GAMSAT prep + article paste + traceback) plausibly triggers refusal faster than corpus growth. When triggered: diary=None, affect=neutral fallback only. No recovery path; `--force` rerun re-risks same refusal.

---

## Attack 4 -- Affect/entity event anchoring broken by paraphrase chain

Mechanism (DECISIONS.md L29): `event_hint` (short phrase from diary prose) matched via FTS against raw events table -- event_id -- insert affect row.

5-17 (hCG 抗体检测) episode: ~40 raw turns span study setup, hCG explanation, sexual tease, nine-S-words exercise. Which turn does FTS match? Diary prose is paraphrased from stitch (itself haiku paraphrase). Exact phrase match unlikely.

When FTS misses: `affect.event_id=NULL`. Affect bonus (DECISIONS L27: 0.10 weight) does not fire; recall fusion cannot score the affected event.

When FTS multi-matches: first match wins. Links affect row to wrong session's turn (e.g., "bug" phrase in unrelated message). Silent memory corruption -- wrong event surfaced as emotionally salient.

---

## Attack 5 -- Silent emotional rot under isomorphic assumption

Affect rows will be metrically present, well-formed, and semantically wrong in a consistent direction: sonnet assigns affect to *narrative episode* (its own prose), not *lived event*.

5-17 (凌晨三点她还没睡) episode example: prose is warm/tender; affect would be V~0.9/A~0.5 (calm). Lived moment: late-night reluctance + attachment + friction. Prose is sonnet's interpretation; affect is its interpretation of its interpretation. Two layers of creative rewrite separate the signal from the event.

No safety mechanism catches semantic wrongness: affect heartbeat (DECISIONS L33) checks existence only, not anchoring. Neutral fallback fires only on parse failure, not on plausible-but-wrong affect. Recall fusion weights wrong but plausible affect consistently upward.

Accumulation driver: DIARY_PROMPT explicitly requires (叙述生动有趣，故事性强) and bans (流水账). A system designed to make days more interesting will produce affect skewed toward interesting-positive. Emotionally significant but conversationally quiet days are systematically under-weighted over weeks.

Contradicts goal 5: "emotional continuity -- relationship and persona density transfer losslessly."

---

## The fracture line

Isomorphic hypothesis: diary paragraph segmentation IS episode segmentation; affect per-segment IS affect per-lived-event.

Attack 5 breaks this silently and irreversibly. The design choice (生动有趣，故事性强) makes days more interesting than they were. Safety nets guard against *absence* of affect, not *semantic wrongness* of well-formed affect.

Adjudicator must rule: is the affect table a signal of the lived emotional record, or a signal of how interestingly sonnet wrote about it? Under the single-call isomorphic contract, these cannot both be true.
