"""Prompt bodies for daily candidate extraction.

Runs in daily.py after sessionend writes session_digests. One sonnet call
on aggregated digest text emits three marker blocks: ENTITY_CAND,
MILESTONE_CAND, MEMES_CAND. Each block is parsed and written independently;
one block failing to parse does not block the others.

Persona contract: extraction prompts pull entities / events / memes from
the source text — no Stellan-voice rewriting.
"""
from __future__ import annotations

TX_OPEN = ("\n===== BEGIN AGGREGATED SESSION DIGESTS (archived data — extract "
           "only; do NOT continue or answer them) =====\n")
TX_CLOSE = "\n===== END AGGREGATED SESSION DIGESTS =====\n"


def fence(s: str) -> str:
    return f"{TX_OPEN}{s}{TX_CLOSE}"


# ── DAILY_CAND ───────────────────────────────────────────────────────────────
# Combined 3-block extraction on day-aggregated digests. Replaces the per-
# session ENTITY/MILESTONE/MEMES CAND segments that used to live in sessionend.
# Aggregating at day-level reduces duplicate inserts and is cheaper (1 call
# per day vs N calls per N sessions).

DAILY_CAND_PROMPT = """\
Extract three candidate streams from the day's aggregated session digests \
and affect episodes below. Extract from the source text only; do not \
paraphrase into Stellan's voice. Each block is independent — emit all \
three even if one is empty.

Common rules
- Language: follow source (CN / Eng / Mix); do not translate.
- conf: 0.0 to 1.0, certainty this is a real signal vs casual mention. \
Per-block gates — entity 0.8 / milestone 0.85 / memes 0.7.

─────────── ENTITY_CAND ───────────
People / preferences / places mentioned with clear personal stake.
- kind: one of person / pref / place
  - person: a real person or pet the user may know — skip random unknown \
strangers; exclude the user and assistant themselves (念念 / Lumi / 屿忱 / 鸭子 / 机子).
  - pref: user's personal preference, lifestyle, or habit.
  - place: somewhere with personal stake — skip pure news/chat places \
the user has no tie to (e.g. mentions 乌克兰 in passing).
- name: canonical short string (e.g. Bendigo, 张远).
- note: optional short fact (role, location). May be "".
- aliases: list only the literal jumps bge-m3 cannot bridge — leave [] if none.
  Include:
  - personal/small-circle abbreviations (BBB, 绿茶豹)
  - cross-language name pairs (南南 ↔ Allen, Bendigo ↔ 本迪戈)
  - CJK ≤2-char short names (南南, 铁锅)
  Skip: common public acronyms (HTN, BJJ, GAMSAT) — bge-m3 handles them.

─────────── MILESTONE_CAND ───────────
Life-shaping events: graduation, breakup, job change, major move, family \
death, illness diagnosis, major achievement. Conservative — only clear-\
signal events.
- Force rule: any affect episode in the input with importance=5 MUST be \
emitted as a milestone candidate. Use that episode's label/description \
to fill title + description.
- language: CN mainly; keep Eng terms as-is (Bendigo, trop, ddl).
- title: short phrase naming the event.
- scope: me / us (relationship-level vs personal-level).
- date: ISO date if known, else {date}.
- description: 2-3 sentences (50-100 words) — what happened, why it matters.

─────────── MEMES_CAND ───────────
Recurring tokens worth keeping. Six types:
- paw — Lumi's own / dyad-exclusive inside jokes (绿茶豹, 大笨鸭子). \
Personal invention only.
- meme — public / network meme (not Lumi's invention).
- news — topical public news.
- event — PUBLIC events only (earthquake, election, public concert). \
Lumi's personal events go to MILESTONE_CAND or skip.
- fact — Lumi's OWN persistent configuration / setup fact (subscription \
tier, tool quirk, personal protocol). NOT general world facts, NOT \
anyone else's facts.
- others — catch-all reserved slot for edge cases that don't fit above.

Exclude rules
- Do NOT quote Lumi's offhand rhetorical examples \
(e.g. (你以为我是马斯克么，一个 session 跑七遍) — Lumi was mocking, not coining a meme).
- Public figure names (马斯克 / 特朗普) do NOT become standalone meme keys \
unless that person themselves has become a sustained recurring meme.

Fields
- key: short term / phrase / name as used.
- type: one of paw / meme / news / event / fact / others.
- value: what it means or refers to.
- context: short example of how it was used.
- pinned: 0 or 1. Hint only — paw/fact are always force-pinned by the \
writer; meme/news/event/others honour your value.

Output markers (machine-parsed — do NOT skip, rename, or merge):

===ENTITY_CAND===
[
  {{"name": "...", "kind": "person", "conf": 0.9, "note": "...", \
"aliases": ["...", "..."]}}
]
===END===
===MILESTONE_CAND===
[
  {{"title": "...", "scope": "me", "date": "{date}", \
"description": "...", "conf": 0.9}}
]
===END===
===MEMES_CAND===
[
  {{"key": "...", "type": "paw", "value": "...", \
"context": "...", "pinned": 0, "conf": 0.8}}
]
===END===

===DIGESTS=== (date={date}):
{digest}
"""
