# 1M context probe + token census — 2026-05-19

> diary.py 需要1M

Empirical verification for single-call diary design.

## Context window (claude_cli stream-json sonnet)
`modelUsage.contextWindow: 200000` is metadata, NOT enforced. Flags: `--output-format stream-json --input-format stream-json --verbose --model claude-sonnet-4-6 --setting-sources "" --strict-mcp-config`.

Two-needle test (NEEDLE-ALPHA @~197K depth + NEEDLE-OMEGA @~305K depth):
- 800K filler / 372,718 tokens / both needles found
- 1M filler / 456,288 tokens / both found

Real >200K window confirmed.

## Token census (haiku, stream-json, baseline=30,513)
- 2026-05-17: 52K chars / 34,984 net tok → 0.6675 tok/char
- 2026-05-18: 229K chars / 151,497 net tok → 0.6607 tok/char (heaviest observed)
- 2026-05-19: 159K chars / 93,783 net tok → 0.5897 tok/char

Representative ratio: **0.66 tok/char**.

## Policy refusal — content-type dependent, NOT token length
- Plain EN prose: refused at 136K / 162K tokens (`stop_reason: "refusal"`, "violate our Usage Policy")
- Numeric/neutral filler: passed at 269K / 372K / 456K tokens
- Real CN+EN diary material (2026-05-18, 230K chars): passed at 171K tokens, `stop_reason: end_turn`

"Prompt is too long" was a paraphrase, not the raw error.

## Single-call guard
**303K chars (~200K net tokens)** = 30% headroom over heaviest real day. Pre-call early-exit threshold; mid-stream refusal still needs post-call sentinel (see DECISIONS, P2 adjudication).
