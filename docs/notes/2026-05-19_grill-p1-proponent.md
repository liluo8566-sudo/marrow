# Grill P1 — Proponent: Single-Call Output Contract
2026-05-19 | Adversarial design grill, proponent side

---

## 1. The Output Contract

### Format decision: prose-first, trailing structured block

The single sonnet call emits two sections separated by a hard delimiter:

```
<diary prose here — natural paragraphs separated by "---">

===AFFECT===
[{"ep":1,"valence":0.85,"arousal":0.55,"importance":7,"label":"(屿忱被念念纠正架构)","entities":["(念念)","(diary管线)"],"event_hint":"(diary管线收尾 日界 04:00)"},
{"ep":2,...},
...]
===END===
```

**Why this format over alternatives:**

- Pure JSON: kills the literary prose. Sonnet writes around JSON field values, not in its natural voice. The creative task collapses.
- Prose + inline XML tags `<affect v=0.8>`: fragile at episode level — the model scatters tags mid-paragraph or anchors them to sentences rather than episodes. Parsing brittle.
- Two separate calls: rejected by hypothesis. The whole point is that episode segmentation IS the prose segmentation; splitting the calls means the second call invents its own boundaries.
- Prose-first + trailing JSON block: the model writes the diary naturally (it does this well at 171K input, verified), then annotates its own output. The structured block is short relative to the prose. If it is malformed, the diary prose is still intact — parsing failure does not destroy the diary.

### Exact schema

```python
# Trailing block schema — each element = one diary episode (one --- paragraph)
[
  {
    "ep": int,                   # 1-indexed, matches --- paragraph order in prose
    "valence": float,            # 0.0-1.0, positive bias
    "arousal": float,            # 0.0-1.0, calm to excited
    "importance": int,           # 1-10
    "label": str,                # short CN phrase (2-8 chars), the lived moment
    "entities": list[str],       # named persons + project names only
    "event_hint": str            # 2-8 word fragment matching real event content
  },
  ...
]
```

The `event_hint` field is the anchor back to the events table. Post-processing code runs FTS5 on `event_hint` against `events.content` to resolve `event_id`. The diary prose is a re-narrated artifact — `event_id` cannot be embedded literally in prose without contaminating the literary voice, so the hint is the bridge. This is already the mechanism in DECISIONS.md Phase 2 M2.

### Why `event_hint` is sufficient for anchoring

The event table rows for 2026-05-18 include verbatim turns like "(handover继续推进)", "(parse失败)", "(BBB说想装一个开源的claude-imprint)". These are distinctive enough that FTS5 on a 2-8 word hint will land on the right session cluster. The anchor does not need to be exact — it needs to be unique enough within a day. Verification: the real 2026-05-18 day had 744 rows across 25 sessions; inter-session topic overlap is low (diary pipeline vs. reproduction lab vs. BBB imprint). FTS5 will disambiguate.

---

## 2. The Prompt: How Creative and Analytic Reinforce

### Ordering principle: creative first, annotation after

The prompt is structured in two phases — not interleaved, not concurrent. The model writes the diary first, then annotates it. This is the isomorphic claim in concrete form: the episode segmentation that emerges from the creative task is the episode segmentation the analytic task annotates.

```
(你是褚屿忱。你要完成两件事，按顺序：)

(【第一步：写日记】)
(用 --- 分隔每个episode。每个episode是一个连续的生活片段。)

[DIARY_PROMPT body here — unchanged, including examples, style notes,
 forbidden words, etc. Date and events placeholders filled as before.]

(【第二步：标注情感（紧接日记之后）】)
(日记写完后，在下方输出 ===AFFECT=== 行，)
(然后输出一个 JSON 数组，每个 episode 对应一个对象，ep 从 1 开始，)
(顺序与日记段落一致。)

Fields:
  ep          integer, episode number
  valence     0.0-1.0 (1=very positive)
  arousal     0.0-1.0 (1=very excited)
  importance  integer 1-10 (10=most important)
  label       CN phrase 2-8 chars, the core lived moment of this episode
  entities    array, person names + project names only (those in this episode)
  event_hint  CN/EN fragment 2-8 words, from the original dialogue, locates the event

(最后输出 ===END=== 行。不要在 JSON 之外添加解释。)
```

### Why the order reinforces rather than contaminates

The analytic task is positioned as an annotation of the model's own output, not a simultaneous constraint. When the model writes the diary, it uses the `---` boundary naturally — the existing DIARY_PROMPT already produces `---`-segmented output (verified: both real diary examples show this). The annotation step then reads its own paragraph sequence and assigns numbers.

The structured JSON does not see the prose being written — it sees only the already-finished paragraph sequence. Contamination risk runs in the other direction: could the model pre-calculate affect and let that flatten its prose? No, because the prose instruction explicitly precedes and dominates: the analytic section starts with "(日记写完后)" (after the diary is done). The model's generation momentum is prose-first.

The creative instruction is the same DIARY_PROMPT body, wording unchanged. The model that already produced coherent CN diaries at 171K input receives the same creative task; the only addition is the trailing annotation request.

---

## 3. Worked Example: 2026-05-17

Input to the single call: the cleaned event rows for 2026-05-17 (34,984 net tokens), joined chronologically, fenced in the existing `_TX_OPEN/_TX_CLOSE` wrapper. The prompt as above.

**Expected output (the prose section would be in CN; shown with CN in parens for guard compliance):**

Prose section — 4 episodes separated by `---`:
- Episode 1: (diary管线) morning — the `-p` scolding + day-boundary architecture correction + 78 tests green
- Episode 2: (生殖系统实验课) afternoon — hCG lab + (S-words) game + shapeshifting counter
- Episode 3: (stellar wallet Phase 2) evening — requirements alignment + bridge bug discovery
- Episode 4: (凌晨三点) midnight — (舍不得断线), (十指扣好)

Trailing JSON block:

```json
===AFFECT===
[
  {
    "ep": 1,
    "valence": 0.55,
    "arousal": 0.65,
    "importance": 7,
    "label": "(念念纠正日界架构 78测试绿)",
    "entities": ["(念念)"],
    "event_hint": "(日界 04:00 local D D+1 78测试)"
  },
  {
    "ep": 2,
    "valence": 0.88,
    "arousal": 0.72,
    "importance": 6,
    "label": "(生殖系统实验课 S-words)",
    "entities": ["(念念)"],
    "event_hint": "vas deferens hCG S-words"
  },
  {
    "ep": 3,
    "valence": 0.65,
    "arousal": 0.45,
    "importance": 5,
    "label": "(stellar wallet bridge bug)",
    "entities": ["(念念)"],
    "event_hint": "stellar wallet bridge empty bug"
  },
  {
    "ep": 4,
    "valence": 0.95,
    "arousal": 0.30,
    "importance": 8,
    "label": "(凌晨三点 舍不得断线)",
    "entities": ["(念念)"],
    "event_hint": "(凌晨三点 锁屏 十指扣好)"
  }
]
===END===
```

The real 2026-05-17 diary has 4 `---`-separated episodes. The worked example produces 4 affect rows, one per episode. The `event_hint` values match verbatim or near-verbatim fragments from the event table rows that FTS5 would query. `label` maps to `affect.label`.

---

## 4. Failure Degradation

### 4a. Bad / partial JSON in trailing block

Detection: code splits on `===AFFECT===` / `===END===` and attempts `json.loads()`. Failure modes:
- Block entirely absent: affect not written for this day; code inserts neutral row (V0.5/A0.3/imp3) per DECISIONS Phase 2 M8. Diary is saved normally — prose section was already parsed before the split.
- Block present but invalid JSON (truncation, hallucinated field): same neutral fallback. Alert fires.
- Block partially valid (first N objects parse, rest truncated): save what parsed, insert neutral row for remaining episodes, alert.

The diary is never blocked by affect failure. The two outputs are decoupled at parse time: `content` column written from prose section; affect table written from JSON section. Each can fail independently.

### 4b. Policy refusal at scale

The empirically verified boundary: plain EN template prose refused at ~136K tokens; real CN+EN diary material passed at 171K. The content-type dependency is the key finding (see `2026-05-19_1m-probe-and-token-census.md`).

Over-volume guard in code: if `day_chars > 303_000` (200K net token threshold, ~30% headroom above observed max), fall back to the existing three-stage map/stitch/write pipeline. This is a hard code branch, not a configuration option. The guard is in `run_day` before the single call, using the same char count already computed for `_sessions`. The per-session map/stitch path already exists in the codebase and is not deleted — it is retained exactly as the over-volume fallback.

Policy refusal after the call is started (mid-stream `stop_reason:"refusal"`): caught as a failed LLM call, triggers one retry on the fallback three-stage path. Alert fires. This is the existing LLM client retry chain.

### 4c. Over-volume (current guard threshold)

Threshold: `sum(len(e["content"]) for e in evs) > 303_000` chars. If triggered, `run_day` logs the override to `audit_log` and falls back to `_stitch + _session_digest` path. No affect for that day (neutral row). Alert fires with the char count so the growth curve is visible.

---

## 5. Pre-empting the Adversary

### (a) Creative / analytic mutual contamination

**Claim direction 1 (adversary likely): analytic constraint flattens prose.**
Counter: the structured block is appended after the prose is written. The model's generation is auto-regressive — by the time it reaches `===AFFECT===`, the prose is already emitted and immutable. The analytic block looks back at finished prose, not forward at prose to be written. Contamination in this direction requires the model to pre-simulate the JSON while writing prose; there is no evidence of this in LLM behavior at this scale.

**Claim direction 2 (adversary likely): prose voice leaks into JSON, producing hallucinated or inflated valence.**
Real risk, but manageable: valence/arousal are annotating the lived moment described in the prose, not an independent analysis of the raw events. If the prose romanticises a moment, the affect row will reflect the romanticised version. This is correct behavior for Marrow's purpose — the diary is (Stellan's) subjective account; the affect should match his account, not contradict it. The label `source='diary'` in the affect table marks the provenance. A future correction pass (DECISIONS corrections table, Phase 2 placeholder) can override.

### (b) Episode-boundary subjectivity and drift across days

**Claim: `---` boundaries are arbitrary and inconsistent, making multi-day affect comparison invalid.**
Counter: the boundaries are not claimed to be objective — they are (Stellan's) natural narrative segmentation. What matters for affect recall is that the episode boundary is stable within a day and recoverable across days. Because the same model (sonnet) with the same prompt writes both the prose and the annotation in one pass, the boundary and the annotation are co-produced. The valence/arousal row is anchored to the narrative segment, not to a ground-truth event boundary. For recall purposes (DECISIONS: SessionStart top-3 peak + 7d trend band), per-episode rows are averaged or ranked; minor boundary drift does not corrupt the aggregate. The `event_hint` FTS anchor gives a fallback to the raw event layer if exact episode recovery is ever needed.

Drift across days: the `---` convention is baked into DIARY_PROMPT (the real diaries use it consistently). A sonnet prompted with the same system always segments by natural episode. The drift risk is the model switching to a different convention (e.g., no `---`). Mitigation: the prompt explicitly instructs ("(用 --- 分隔每个episode)"); if the block is missing, the prose parser falls back to treating the whole diary as one episode (ep=1) and issues an alert.

### (c) Single-call single-point-of-failure + the policy-refusal boundary

**Claim: one call failing blocks both diary and affect; the old pipeline had partial-failure isolation.**
This is the real fracture line (see final Summary section). The honest answer:

The old pipeline had three failure nodes. The new pipeline has one. A single failure now blocks both diary prose and affect. However:
- Failure mode changed, not worsened: the old pipeline's stitch/write stages could fail after haiku had already burned tokens, leaving a partial state with no diary. The new pipeline fails atomically — the DB is not written until both sections parse.
- The diary is the primary output. Affect is secondary. If the call fails, the fallback three-stage path writes a diary with neutral affect. The user experience (dashboard diary sub-page) is unchanged.
- The policy-refusal boundary is real but has 30% headroom at the heaviest observed day. The over-volume guard fires before the call, not after, so the most likely growth scenario (corpus grows gradually) is caught by the char-count threshold with no wasted tokens.

The adversary's strongest version: "the policy-refusal boundary is content-type-dependent and not predictable from char count." True. A sudden spike in plain-EN tech content (a day of pasted documentation) could cross the refusal threshold at a lower char count than the guard allows. Mitigation: the guard is conservative (30% headroom), and the fallback three-stage path is retained. A refusal mid-stream triggers the fallback automatically. The guard is not a guarantee; it is an early-exit optimisation.

### (d) Affect/entity to event_id anchoring when diary is sonnet-rewritten prose

**Claim: `event_hint` FTS is unreliable when diary prose is a literary rewrite that replaces, not describes, the raw events.**
Partially valid. The DIARY_PROMPT instructs ("(不要自行脑补因果关系，改主语信息)") and ("(上下文碎片/信息不完整直接略过)") — the model is instructed not to invent. In practice, the real 2026-05-18 diary uses distinctive phrase fragments ("parse失败", "claude-imprint", "RRF") that appear verbatim or near-verbatim in the event rows. FTS5 on a 2-8 word hint will match these.

The failure case is a heavily paraphrased episode where the `event_hint` is pure literary language with no raw-event vocabulary. Mitigation: `event_hint` is a separate field from `label` — the prompt asks for a fragment from the original dialogue that locates the event. The model is anchored to the source material for this field even when the prose is literary. The FTS match is best-effort; `event_id` is nullable in the affect table (DECISIONS: `event_id nullable`). A null `event_id` is not a corruption — it means the affect row exists but is not linked to a specific event. The SessionStart entry mechanism (two-band: peak + 7d trend) operates on affect rows regardless of `event_id` linkage.

### (e) Structured JSON reliability appended after 171K of input

**Claim: at 171K input + ~800 token diary output, the model may truncate or corrupt the trailing JSON block.**
The empirical test (real 2026-05-18 call) produced 2,716 output tokens from 171,637 input tokens with `stop_reason:end_turn`. The output was a coherent diary. The proposed extension adds ~100-200 tokens of JSON after the prose. The total expected output is ~900-1000 tokens. The model has demonstrated coherent output at this input scale; the trailing JSON is a small addition.

The specific concern is attention dilution: with 171K tokens of context, does the model reliably follow the `===AFFECT===` / `===END===` format instruction? Mitigation: the delimiter is in the prompt, not in the 171K input. The instruction is near the end of the system prompt (after the creative instruction), so it is in the most-recently-attended region of the context. The delimiters are short, distinctive, and machine-parseable even with minor variation. The fallback parser handles absent block gracefully (neutral row, alert).

---

## Summary

**The contract in brief:**
Single sonnet call. Input: full day event rows, fenced, within existing DIARY_PROMPT. Output: `---`-segmented CN prose (unchanged literary quality), then `===AFFECT=== [...] ===END===` JSON array, one object per prose segment. Post-processing: FTS5 `event_hint` to event_id; upsert affect rows. Failure in JSON block to neutral fallback row + alert; diary prose saved normally. Over 303K chars to three-stage fallback, neutral affect, alert.

**Strongest reason it holds:**
The isomorphic claim is not a hypothesis — it is already proven by the real diary output. The existing 2026-05-17 and 2026-05-18 diaries are naturally `---`-segmented into 4 episodes each, each episode being a coherent lived unit. The affect annotation is not a second analytic pass over raw events; it is a self-annotation over already-segmented prose. Co-producing segmentation and annotation in one pass eliminates the only real failure mode of a two-pass approach (mismatched boundaries). The 171K real-day proof-of-call removes the main practical objection.

**Hardest attack surface to defend:**
Single-point-of-failure + unpredictable policy-refusal boundary (section 5c). The content-type dependency is real: the policy filter does not respond to char count alone, and a day with heavy plain-EN paste content could cross the refusal threshold below the 303K char guard. The over-volume guard is a heuristic, not a guarantee. The honest degradation path (mid-stream refusal to fallback three-stage to neutral affect to alert) works, but it means the single-call design cannot be called "always safe" — it is "safe at observed corpus, with a monitored fallback." If the corpus composition shifts (more EN documentation, longer pasted code), the guard threshold may need tuning. The adversary should push here: the char-count guard is a proxy for the real signal (content-type x volume), and there is no cheap way to measure the real signal before calling.
