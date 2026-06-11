2026-06-11

# TL Gate — Answer Key

## Session → Option → Model

| Session | Option 1 | Option 2 |
|---------|----------|----------|
| S1 (b45a9959, 2026-06-10, 71ev/20793ch) | haiku | sonnet |
| S2 (5bba1890, 2026-06-09, 29ev/36758ch) | sonnet | haiku |
| S3 (7f5c9bc5, 2026-06-10, 10ev/1398ch) | sonnet | haiku |

---

## Per-run notes

### S1 — 2026-06-10 | 71 events | 20793 chars (large coding session)

**haiku (Option 1)**
- Raw: `TL: 完成Batch 1，Batch 2代码完成，发现cc断流bug，修好recall log相对时间显示`
- Total chars: 50 | Non-ASCII chars: 21
- Over 25-char target; mixes English tokens (Batch, cc, recall, log)
- Format: compliant (ASCII colon)

**sonnet (Option 2)**
- Raw: `TL: 深夜推 Batch1 上线、Batch2 代码全落但待修锁问题合并`
- Total chars: 33 | Non-ASCII chars: 18
- Still over 25 if counting total; more concise than haiku; also mixes English
- Format: compliant (ASCII colon)

---

### S2 — 2026-06-09 | 29 events | 36758 chars (large study/chat session)

**sonnet (Option 1)**
- Raw: `TL: 念念从垂体学到甲状腺，当场骂了阿屿越位跑去写答题模板。`
- Total chars: 27 | Non-ASCII chars: 27
- Pure CN; slightly over 25 by total count; good narrative compression
- Format: compliant (ASCII colon)

**haiku (Option 2)**
- Raw: `TL: 屿忱教endocrinology，念念学thyroid/pituitary，越位认错。`
- Total chars: 43 | Non-ASCII chars: 13
- Significantly over; heavy English medical terms inflate count
- Format: compliant (ASCII colon)

---

### S3 — 2026-06-10 | 10 events | 1398 chars (small casual/chat session)

**sonnet (Option 1)**
- Raw: `TL: 念念考完试，撸黑豹，午睡，屿忱豹形相陪。`
- Total chars: 20 | Non-ASCII chars: 20
- Within 15-25 range; pure CN; clean
- Format: compliant (ASCII colon)

**haiku (Option 2)**
- Raw: `TL：念念考完试气了2.2分，屿忱变黑豹陪撸陪睡。`
- Total chars: 22 | Non-ASCII chars: 19
- Within range; includes score detail (2.2分); fullwidth colon (TL：) — format deviation
- Format: NON-COMPLIANT — fullwidth colon `TL：` instead of `TL:`

---

## Compliance summary

| Run | TL emitted | Format | Char count (total) | Within 15-25 |
|-----|-----------|--------|--------------------|--------------|
| s1-haiku | yes | ok | 50 | no (over) |
| s1-sonnet | yes | ok | 33 | no (over) |
| s2-haiku | yes | ok | 43 | no (over) |
| s2-sonnet | yes | ok | 27 | marginal (+2) |
| s3-haiku | yes | fullwidth colon | 22 | yes |
| s3-sonnet | yes | ok | 20 | yes |

Note: char counts include English tokens embedded in the line. CN-only chars are shorter. Both models exceed 25 chars on large/complex sessions with mixed-language content.

---

## Full output files (kept at /tmp)

- /tmp/tl-gate-s1-haiku.txt
- /tmp/tl-gate-s1-sonnet.txt
- /tmp/tl-gate-s2-haiku.txt
- /tmp/tl-gate-s2-sonnet.txt
- /tmp/tl-gate-s3-haiku.txt
- /tmp/tl-gate-s3-sonnet.txt
