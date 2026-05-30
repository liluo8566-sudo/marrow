# MAP build — fill MAP-v2 template

> Run a workflow: 8 agents fill MAP-v2 template in parallel, main session merges to `MAP.md`.
> Goal: any new session reads MAP + DESIGN and knows how marrow works without grepping code.

## Inputs (locked)

- Template: `/Users/Gabrielle/Desktop/MAP-v2.md` (read-only — agents return content, do NOT edit)
- Output: `/Users/Gabrielle/CC-Lab/marrow/MAP.md` (overwrite, old version cleared by Lumi)
- Cap: ≤300 lines total. OPEN QUESTIONS block excluded from cap.
- §4.1 schema list format: `<table>: <what> · <subtypes/states if any> · <retire/aging>` — no Why field
- §11.3 kept: paths + config.toml section catalog (path names + one-line each, no key expansion)

## Agent assignments (8 parallel · sonnet)

| # | section_id | scope | qs | budget |
|---|---|---|---|---|
| a1 | sysmap+infra | §1.1 §1.2 §11.1 §11.2 §11.3 §11.4 | Q11 Q13 Q14 Q15 Q16 | 50 ln |
| a2 | write+read | §2.1 §2.2 §2.3 §3 | Q1 Q9 | 40 ln |
| a3 | storage | §4.1 §4.2 §4.3 | Q9 | 35 ln |
| a4 | surface | §5.1 §5.2 §5.3 §5.4 | Q3 Q4 Q6 Q10 Q12 Q15 Q18 | 60 ln |
| a5 | handover | §6 | Q7 Q18 | 20 ln |
| a6 | jobs+catchup+aging | §7 §9 §10 | Q2 Q5 Q13 | 30 ln |
| a7 | alerts | §8 | — | 15 ln |
| a8 | addons+status | §12 §13 | Q17 | 25 ln |

Total content: ~275 ln + headers ~25 ≈ 300. OPEN QUESTIONS appended after, not counted.

## Agent contract

Returns schema:
```
{ section_id, filled_md, q_answers: [{q_id, answer}], agent_notes: [...] }
```

Hard rules every prompt includes:
- Read template FILL RULES (top of MAP-v2.md) before writing
- COMPONENT block: What/Why/How 1 sentence each (How max 3 sentences). Where ≤5 file:line.
- LIST block: one item per line, " · " separators
- No code, no call traces, plain natural language
- Cite file:line for every factual claim. `??` for unverified, never guess.
- `filled_md` = body content for assigned sections only (no section titles, no FILL comments)
- Stay within line budget. Main session will not trim down further.

## Merge (main session)

1. Concatenate `filled_md` by template section order
2. Strip FILL comments + examples + TOC commentary
3. Place q_answers under OPEN QUESTIONS (Q1–Q18 order)
4. Dedup agent_notes → Agent Notes block
5. Write `/Users/Gabrielle/CC-Lab/marrow/MAP.md`
6. Report total line count + which sections over budget

## Verification

- Lumi spot-checks 3–5 questions answered from MAP alone, no grep
- Wrong / missing → re-fire single agent with tighter prompt
