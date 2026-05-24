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
below. Extract from the digest text only; do not paraphrase into Stellan's \
voice. Each block is independent — emit all three even if one is empty.

─────────── ENTITY_CAND ───────────
People / preferences / places mentioned with clear evidence in the text.
- name: canonical short string (CN names exact, no transliteration)
- kind: one of person / pref / place
- conf: 0.0 to 1.0 — how certain this is a real entity vs casual mention
- note: optional short fact (role, location). May be "".
- aliases: list of every other way 念念 might refer to this entity in a \
query — CN translation if name is EN, EN translation if name is CN, \
nicknames, abbreviations, sport / brand / topic terms, singular & plural \
variants, common shorthand. Cross-language coverage is mandatory: an EN \
name (e.g. Colours) must include CN ("颜色") and the singular form \
("colour"); a CN nickname (e.g. 南南) must include the real name (Allen) \
and any relationship label (gay bestie). Be liberal — false positives \
are cheap, misses are expensive. May be [] only when no plausible \
alternate term exists.

─────────── MILESTONE_CAND ───────────
Life-shaping events: graduation, breakup, job change, major move, family \
death, illness diagnosis, major achievement. Conservative — gate is high \
(conf ≥ 0.85), only clear-signal events.
- title: short phrase naming the event
- scope: me / us (relationship-level vs personal-level)
- date: ISO date if known, else {date}
- description: 2-3 sentences (50-100 words) — what happened, why it matters
- conf: 0.0 to 1.0

─────────── MEMES_CAND ───────────
Memes / inside jokes / coined terms / viral quotes / topical news (DESIGN \
line 47). Hot memes first.
Include:
- inside jokes, coined terms, persona shorthand, recurring private phrases \
between 念念 and 屿忱.
- viral quotes either side repeats verbatim (memes from outside that \
landed inside the relationship).
- topical news / event mentions worth tracking — current affairs, public \
events, named happenings 念念 brings up.

- key: short term / phrase / name as used
- type: meme / cipher / nickname / phrase / quote / news
- value: what it means or refers to
- context: short example of how it was used
- pinned: 0 or 1 — 1 for private anchors (persona names, ciphers, intimate \
relationship memes that should never decay); 0 for public / viral / topical \
items that may age out. When in doubt, 0.
- conf: 0.0 to 1.0

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
  {{"key": "...", "type": "meme", "value": "...", \
"context": "...", "pinned": 0, "conf": 0.8}}
]
===END===

===DIGESTS=== (date={date}):
{digest}
"""
