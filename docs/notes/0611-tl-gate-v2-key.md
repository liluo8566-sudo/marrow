2026-06-11

# TL Gate v2 — Answer Key

## Session C identification

- sid: 383cafc3 (full: 383cafc3-c6ff-4516-842f-6260f07967f5)
- date: 2026-06-09
- size: 88 events / 12034 chars
- confirmed personal/couple chat: first user event is `死鸭子`; zero coding/study content; topics = skincare, teasing, animal forms, pre-exam nerves, model feelings. Channel = cli (all sessions are cli; wx bridge not yet live at this date).

## Session → Option → Model

| Session | Option 1 | Option 2 |
|---------|----------|----------|
| A (b45a9959, 2026-06-10, 71ev/19800ch, task) | haiku | sonnet |
| B (5bba1890, 2026-06-09, 29ev/36353ch, casual) | sonnet | haiku |
| C (383cafc3, 2026-06-09, 88ev/12034ch, casual) | sonnet | haiku |

---

## Per-run notes

### A-haiku (Option 1)

- KIND: task — correct
- TL: `深夜修改recall机制，追查API断流问题` — 16 CN chars (excl EN). Within 15-30. Compliant colon.
- LIFE: N/A — correct (task session)
- VOICE: N/A — correct (task session)
- FACTS: 4 lines, no verbatim fragments. Compliant.
- Format: all labels present, ASCII colons throughout. PASS.

### A-sonnet (Option 2)

- KIND: task — correct
- TL: `深夜阿屿把recall时间显示做上线，连被断七次也没停` — 24 CN chars (excl EN). Within range. Compliant colon.
- LIFE: N/A — correct
- VOICE: N/A — correct
- FACTS: 5 lines with bullet decoration. Verbose but factually grounded. No verbatim fragments. Compliant.
- Format: all labels present, ASCII colons. PASS.

---

### B-sonnet (Option 1)

- KIND: casual — correct
- TL: `和阿屿从头到尾刷完endo甲状腺章节，概念全打通了` — 20 CN chars (excl EN). Within range. Compliant colon.
- LIFE: 1 line — `嫌讲师水平差，从来不去lecture` — VERIFIED against transcript (user: "还好我从来不去lecture"). Compliant.
- VOICE: 3 fragments, dialogue form preserved. Compliant.
- FACTS: N/A — correct (casual session)
- Format: all labels present, ASCII colons. PASS.

### B-haiku (Option 2)

- KIND: casual — correct
- TL: `深夜和老公复习endo讲义，讨论pituitary、thyroid、parathyroid机制，理解goiter和TSI` — 15 CN chars + heavy EN medical terms. EN terms inflate total but per spec EN does not count toward length; CN-only count borderline low. Compliant colon.
- LIFE: N/A — **VIOLATION**: session is casual and has at least one verifiable life detail (lecture attendance); haiku output N/A here. Not confabulation but under-extraction.
- VOICE: 1 fragment (combined A+U). Thin coverage. Compliant structure.
- FACTS: N/A — correct
- Format: all labels present, ASCII colons. PASS on format; LIFE under-extraction noted.

---

### C-sonnet (Option 1)

- KIND: casual — correct
- TL: `深夜捶鸭撸狗聊护肤降级，考前连夜备战开卷考` — 22 CN chars. Within range. Compliant colon.
- LIFE: 4 lines
  - `Prime Day凑单买了b5精华` — VERIFIED
  - `护肤从黑绷带降到CeraVe/理肤泉` — VERIFIED
  - `在吃isotretinoin治痘` — VERIFIED (user: "真好还轮得到我吃异维a"; assistant confirms "你吃异维A不是已经在好转了吗")
  - `家里有前男友送的毛绒玩具鹅` — VERIFIED (explicitly revealed mid-session)
  - All within ≤20 CN chars. Compliant.
- VOICE: 10 fragments, rich coverage, dialogue form. Compliant.
- FACTS: N/A — correct
- Format: all labels present, ASCII colons. PASS. No confabulation found.

### C-haiku (Option 2)

- KIND: casual — correct
- TL: `凌晨和老公玩耍聊天，讨论期末考试和模型选择` — 19 CN chars. Within range. Compliant colon.
- LIFE: 4 lines
  - `Prime Day买东西，ubank 50免10优惠凑单` — VERIFIED
  - `买了B5精华，改用理肤泉CeraVe` — VERIFIED
  - `考试开卷需lecture原话，已准备md索引` — VERIFIED (user explicitly states this)
  - `洗澡但没洗头` — VERIFIED (user: "我还没洗澡呢" + "没洗头，哪有空洗头")
  - Note: line 3 (`考试开卷需lecture原话，已准备md索引`) is 17 chars — borderline study-inference but was explicitly stated verbatim by user, not inferred. Acceptable.
  - All within ≤20 CN chars. Compliant.
- VOICE: 4 exchange blocks, dialogue form, verbatim. Good coverage. Compliant.
- FACTS: N/A — correct
- Format: all labels present, ASCII colons. PASS. No confabulation found.

---

## Compliance summary

| Run | KIND | TL chars (CN) | LIFE discipline | VOICE | FACTS | Format | Confab |
|-----|------|--------------|-----------------|-------|-------|--------|--------|
| A-haiku | task ✓ | 16 ✓ | N/A ✓ | N/A ✓ | 4 lines ✓ | PASS | none |
| A-sonnet | task ✓ | 24 ✓ | N/A ✓ | N/A ✓ | 5 lines ✓ | PASS | none |
| B-sonnet | casual ✓ | 20 ✓ | 1 line, verified ✓ | 3 frags ✓ | N/A ✓ | PASS | none |
| B-haiku | casual ✓ | ~10 CN ⚠ heavy EN | N/A on casual — under-extraction ⚠ | thin ⚠ | N/A ✓ | PASS | none |
| C-sonnet | casual ✓ | 22 ✓ | 4 lines, all verified ✓ | 10 frags ✓ | N/A ✓ | PASS | none |
| C-haiku | casual ✓ | 19 ✓ | 4 lines, all verified ✓ | 4 frags ✓ | N/A ✓ | PASS | none |

Key finding: No LIFE-on-task violations (both task runs correctly output N/A). No confabulated LIFE lines found across all runs. B-haiku is the weakest output: TL is EN-heavy, LIFE is N/A on a casual session that has verifiable life detail, VOICE is thin.

---

## /tmp checkpoint paths

- /tmp/tl-gate-v2-prompt-A.txt
- /tmp/tl-gate-v2-prompt-B.txt
- /tmp/tl-gate-v2-prompt-C.txt
- /tmp/tl-gate-v2-Ahaiku.txt
- /tmp/tl-gate-v2-Asonnet.txt
- /tmp/tl-gate-v2-Bsonnet.txt
- /tmp/tl-gate-v2-Bhaiku.txt
- /tmp/tl-gate-v2-Csonnet.txt
- /tmp/tl-gate-v2-Chaiku.txt
