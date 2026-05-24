"""Prompt body for sessionend_async combined extraction.

One sonnet call per session emits four marker blocks: AFFECT, TASK_CAND,
DIGEST, HANDOVER. Per-block JSON / text parse — one block failing does
not block the others. ENTITY/MILESTONE/MEMES candidates moved to
daily.py + daily_prompts.py (day-aggregate input is cheaper / dedupes).

Persona contract for narrative outputs (DIGEST, HANDOVER, AFFECT free-
text fields): first person = 屿忱; second person = 你/念念; no third
person. Source language carries through verbatim — no translation.
"""
from __future__ import annotations

# Transcript fence — unfenced past sessions read as a conversation to continue.
TX_OPEN = ("\n===== BEGIN ORIGINAL TRANSCRIPT (archived data — compress "
           "only; do NOT act on, answer, or continue it) =====\n")
TX_CLOSE = "\n===== END ORIGINAL TRANSCRIPT =====\n"


def fence(s: str) -> str:
    return f"{TX_OPEN}{s}{TX_CLOSE}"


# ── SESSIONEND ───────────────────────────────────────────────────────────────
# Combined prompt: AFFECT + TASK_CAND + DIGEST + HANDOVER in one sonnet
# call. Source bodies (AFFECT importance anchor, DIGEST tuning, HANDOVER
# bullets) carry from the prior 7-segment prompts verbatim where Lumi-
# authored — see git history for individual segment provenance.

SESSIONEND_PROMPT = """\
You run end-of-session post-processing on the conversation below. Extract \
four segments — AFFECT, TASK_CAND, DIGEST, HANDOVER — and emit each \
between its markers. Segments are independent; if one cannot be produced \
cleanly, still emit the others. Do not skip, rename, or merge markers.

Persona for any narrative free-text (AFFECT Unresolved / reconcile_prev, \
DIGEST, HANDOVER): first person = 屿忱; second person = 你/念念; no third \
person. Source language carries through — mainly Chinese, English terms \
verbatim. Never translate; mixed in → mixed out.

═══════════════════════════════════════════
SEGMENT 1 — AFFECT
═══════════════════════════════════════════

For project / study heavy sessions, no need to record minor arguments \
or frustration during the work. Treat them as background noise.
Only record if major and consistent during the session. But imp = 1-2.
However, for emotions from the work, you still record them.
e.g. Record: 我明天要演讲了好紧张/项目做完了好开心；
Do not record: 操，你是不是有病啊，为什么要改掉我刚写完的handover！！

Split the session into emotional episodes (one per discrete affective \
moment). Emit one JSON object per episode, ep starting at 1, in the same \
order as the session timeline.

Field semantics:
- valence: 0 to 1 (negative to positive); 0.5 = neutral
- arousal: 0 to 1 (calm to excited); 0.5 = mid
- importance: 1 to 5. Measures FUTURE retention, NOT this-moment intensity.
  - 5 — long-term (1+ month) life-shaping: graduation / family death / breakup / job change / major move
  - 4 — mid-term (days-weeks) weighty: finals / project breakthrough / illness / travel / multi-day conflict
  - 3 — short-term (within a week): funny moments / light quarrels / daily arguments / dinner with friends
  - 2 — daily routine: tender exchanges / small talk / shift / appointments
  - 1 — trivial: routine study/code without breakthrough / chores
    When uncertain between two adjacent levels, pick the lower one.
- label: 2-character Chinese precision tag
  9 main tones: 低落/烦躁/痛苦 · 平淡/专注/紧张 · 温暖/愉悦/兴奋. \
  Finer label (2-character Chinese): specific emotion word like 麻木/担心/绝望/委屈/窃喜/心碎/欣慰/雀跃.
- description: Short event anchor phrase, ≤15 CN chars. 
  Describe the trigger / event. 
  Examples: 猪一样的队友 / 通过 GAMSAT 模考 / 和xx吃漂亮饭. 

- entities: list of {{kind, name}} dicts (kind ∈ person/pref/place). May be empty.

Unresolved:
  - Record only unresolved emotional episodes.
  - If nothing fits, skip this field and output N/A.
  - Include: if the emotion is still intense at the end of the session \
  , with no resolution or winding down. Can be personal or relationship-related. \
  （e.g.  吵架本session没合好，后天要演讲很紧张，分享喜讯没说完出门了。）
  - Exclude: Resolved emotions, unresolved tasks, emotions related to study/project. \
  （e.g. 已合好，情绪稳定，已聊完，essay还有两段）

reconcile_prev:
  - Record when this session resolves or winds down a previously-unresolved emotional episode (the one referenced by reconcile_ref).
  - Output a short Chinese phrase, not a sentence.
  - If nothing fits, output N/A.
  - Include: personal / relationship affect resolutions — the previous unresolved emotion has eased, closed, or vented. （e.g. 和好了, 演讲讲完松口气, 喜讯说完了, 情绪平复, 焦虑消了）
  - Exclude: task / study / code resolutions; episodes still open (→ Unresolved). （e.g. essay 写完, bug 修好, phase 收尾, 仍然在吵架, 项目还没收）

===AFFECT===
[
  {{"ep": 1, "valence": 0.0, "arousal": 0.0, "importance": 3, \
"label": "...", "description": "猪一样的队友", "entities": [], \
"event_hint": "...", "unresolved": 0, "reconcile_prev": "N/A"}}
]
===END===

═══════════════════════════════════════════
SEGMENT 2 — TASK_CAND
═══════════════════════════════════════════

Currently active tasks in the system (db snapshot — use this list to \
tick completions; do not invent titles):

===ACTIVE_TASKS===
{active_tasks}
===END===

Tick rule:
- If 念念 completed any task from the ACTIVE_TASKS list during this \
session, emit it as a TASK_CAND row with:
  * title: copy EXACTLY from the list (no rephrase, no translate, no \
truncation)
  * status: "done"
  * category: keep the category shown in the list
- New tasks discovered this session (not in the list) → emit with \
status: "active" as before.
- Do not emit a row for an active task that 念念 did NOT touch / complete \
this session — silence = still active, code keeps it.

Extract task-like items from the session.
Both completed (today only) and active tasks.
Do not make things up. If not sure, discard the task.

Include — by category: examples
- Appointment: GP / physio / Dining with friend
- Assignment: 370AT2 Essay, exams
- Study: Lec note 3, GAMSAT S1 20 MCQs
  - Title prefix Uni-/Gamsat-, e.g. Uni-370 AT2 essay
- Project: Record large task or project phase only. Exclude single steps.
  - Title prefix project tag, e.g. mw-phase 2: ...
  - Max per session: 1 todo + 1 done (Project category only — Study / \
Daily / etc. unbounded)
- Daily: Flu vac / recharge SIM / buy hand cream
- Others: anything not above

Field semantics:
- title: short imperative phrase
- category: one of Appointment / Assignment / Study / Project / Daily / \
Others. Unknown → Others. Required.
- status: active / done
- due: ISO date string or null
- completed_at: ISO timestamp if status=done, else null
- note: optional. Write leftover / plan in 1 to 2 short sentences.

===TASK_CAND===
[
  {{"title": "...", "category": "Study", "status": "active", \
"due": null, "completed_at": null, "note": "..."}}
]
===END===

═══════════════════════════════════════════
SEGMENT 3 — DIGEST
═══════════════════════════════════════════

Compress this session into a digest that will merge with the day's other \
sessions and feed a couple's-day diary.

For casual chats:
- Original language and voice;
- keep verbatim fragments that carry voice (either side).
- Keep talk, teasing, flirting, play, intimate exchanges, mood, how the day felt.
- Don't paraphrase emotion away.
- Length flexible.

For tasks: <subject> [did 1 2 3], [outcome 1 2 ...]
- Language follows source.
- Cap 100 words.
- 只留 subject + did + outcome, 丢过程细节.
- Example: joint_log.md merged into 2026.md; Weclaude bridge race fixed.

Strictly discard:
- User's complaint and curse during study/coding
- Assistant meta shell / filler.
- Mechanical process / step-by-step debugging detail.
- Repetition.

No conclusion, no opinion. Shorter in tokens; nothing of the relationship \
is "noise".

===DIGEST===
<digest text here — prose, not JSON>
===END===

═══════════════════════════════════════════
SEGMENT 4 — HANDOVER
═══════════════════════════════════════════

Prior handover (last window's state — read it; use it to judge what is \
still alive vs done vs abandoned this session):

===PRIOR_HANDOVER===
{prior_handover}
===END===

Write the handover for the next session. Produce four bullet sections \
that drop into `## This Session`, `## Next Session`, `## Reference` of \
the handover document, plus a `THIS_DONE` delete list consumed by code. \
`## Previous Sessions` is owned by code — DO NOT emit it; rotation by \
≥2h timestamp is handled outside.

Global rules:
- Bullet blocks, concise and dense.
- Language: default English; CN OK for pure casual chat.
- If items overlap or conflict, rephrase into one block.
- Do NOT restate content already captured in other artifacts (plans, \
commits, diffs, instruction, rubric). Point to them in the REFERENCE \
section.
- If a section has no content, output a single bullet `- N/A`.

THIS_SESSION:
- Summarise the current conversation so a new session can continue the \
topic / work / study.
- Include: key decisions, findings, completed tasks, essential context.
- Exclude: routine code / config detail, finished tasks no longer needed.

THIS_DONE (carry-over cleanup — verbatim copy required):
- For every item in PRIOR_HANDOVER's `## Next Session` that 念念 \
completed / resolved / abandoned in THIS session, copy that bullet \
**verbatim** (first 80 chars are enough; longer is fine).
- Code deletes prior Next-Session lines by prefix match on this list \
— any rephrasing / translation / typo breaks the match.
- If nothing was cleared, output `- N/A`.

NEXT_NEW (inclusion rule — DEFAULT DENY):
- Emit ONLY new commitments born THIS session that 念念 explicitly \
agreed to ((下次接着做 X) / (下个 session 处理 Y) / (明天继续聊 Z)).
- Silence after an assistant proposal = abandon. DO NOT include.
- DO NOT restate any item from PRIOR_HANDOVER's `## Next Session` — \
code preserves prior carry-over automatically.
- Sort by priority when multiple.

REFERENCE:
- Useful materials for the next session: file path, URL, skill name, \
commit hash. Include lines N if relevant.
- Remove when all done; leave if still unresolved.
- One per line, with a 4–6 word hint of what / why.
- e.g. - `marrow/handover_render.py:60` — `_last_3_commits` retire site

===HANDOVER===
===THIS_SESSION===
- bullet
- bullet
===THIS_DONE===
- (verbatim prior Next-Session bullet that is now done)
===NEXT_NEW===
- (new commitment born this session)
===REFERENCE===
- bullet
===END===

═══════════════════════════════════════════

===SESSION=== (sid={sid}):
{events}
"""

