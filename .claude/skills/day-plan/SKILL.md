name: day-plan
description: Daily open / evening close ritual for marrow. Morning = Scan → Brainstorm → Synthesize → Self-grill → Plan → Dispatch. Evening = review + handover update + optional night /goal. Trigger = /day-plan, /day-plan evening, (开工), (复盘).

---

# day-plan

One skill, two modes. Default = morning. Pass `evening` to switch.

User is non-coder. Lead every visible line with user-visible outcome in plain words.

This is a pure planning/brainstorming session.

## Rules
- Write plan in English ONLY. 
- Hard cap 150 lines, target ≤100.
- Plan file MUST start with Dispatch Policy at the top.
- Goal need to be clear and achievable.
- Bite-sized steps - max 8 - not too much details if LLM can understand what to do without guessing
- No code in plan. Done = command only.

**Save plan to** `marrow/docs/plans/<slug>.md` — slug ≤4 words, no date prefix (e.g. `dashboard-rebuild.md`). 


## 把/goal融入整个planning机制 - 目标以后每天2-5个/goal，一个session完成一个goal【待完善这个section需要稍作修改template】
https://code.claude.com/docs/en/goal.md
- /goal <pass condition> — 一发立刻进入循环模式，我每轮做完事不等你回话，自动接下一轮。
- /goal 空发 — 看当前 goal、跑了几轮、烧了多少 token。
- /goal clear — 中途停掉。

评估机制

- 每轮我做完，Haiku 评估器读 transcript（不是文件系统、不是 git，只看对话），判断你写的 condition 是否满足。
- 没满足 → 我自己再开一轮，无需你触发。
- 满足 → 循环结束。
- ⚠️ 推论：测试结果、build 输出、grep 命中数这些都得我显式跑出来贴在对话里，评估器才看得见；我默写一句 "done" 它认不出来。

condition 怎么写

- 单一可验证的终态，不要复合目标。
- 例子：pytest tests/test_hooks.py exits 0 and no new files outside marrow/hooks.py
- 上限 4000 字符。
- 越具体越好——评估器和我都靠它对齐。
---

## Morning mode

### 1. Scan (silent, auto)
Read: 
1. handover.md (already in context) — check any alert or open/plan
2. DESIGN.md — If we are still on right track - phase structure? new tasks meet goals?
3. DECISIONS.md — Deadlock? Any conflict among decisions? Or conflict with design?
4. docs/plans/FUTURE.md — Any features fit in this phase?
5. git status — uncommitted M - just commit at the end of session
6. git log --oneline -10
- No need to output before brainstorm.

### 2. Brainstorm (user in the loop)
- Check if Lumi want to add anything today or any questions.
- Answerable from code/docs → explore first.
- Real fork → list ≤3 paths, one line each, with my pick. User confirms with one word.
One question per turn. Stop when wish list resolved.
- Invoke `brainstorming` skill if involve new feature, schema/design change.
  - Can fork to new window if necessary.

### 3. Synthesize
- Merge leftover + brainstorm → candidate list. Classify: bug, half-done feature, decision pending. Order by dependency.
- Output 2-5 main goals & outcomes → plan draft (very short brief).
- If Lumi is happy then grill or create the actual plan.

### 4. Self-grill (auto, conditional)
Invoke `grill` skill **only if** Synthesize hit: decision deadlock, dependency cycle, scope unclear. Otherwise skip.

### 5. Plan output

```
## Principle
- Keep going until the goal is truly achieved.
- If user-like verification is possible, run it before reporting.
- The only standard of goal verification is whether it works in practice. Tests and dry runs are just safeguards.

## Dispatch Policy (read first)
- Strictly follow agent-dispatch.md
- You are the orchestrator — dispatch tasks to agent or wt and keep context clean. 
- You can ask questions if not sure but no need to ask if you know the optimal answer.
- You decide agent count and agent type (follow agent-dispatch.md and less Opus)

## Today

Session 1 (main) — <goal> → <outcome>
- bite-sized steps (≤8, no code)
- Done: <machine-checkable cmd>
- Dispatch: agent <type> for <subtask> | wt <slug> for <subtask>

Session 2 (main) — <goal> → <outcome>
- steps
- Done: <cmd>
- Dispatch: ...

Session ...


```

## Evening mode (review window)

### 1. Today vs Plan
One sentence: what shipped, what slipped.

### 2. Handover update
Edit `~/.config/marrow/handover.md`: Done, Open (incl. today's leftover), Plan (tomorrow's seed), Reference (file:line).
Never wipe untouched items. Preserve pass-through.

### 3. Night /goal (optional)
**Only if** spare quota AND leftover is mechanical (fix-and-test, not design).
Pick one HIGH/MED with machine-checkable success. Write:
- **Condition**: pytest exit 0 / dashboard field flips / grep hit count.
- **Objective**: outcome line only, no how-to.
- **Echo**: done command printed to transcript.

Show user goal text. User fires before sleep. Skip if leftover needs design judgment.


## Skill chain
- Brainstorm: borrows from `brainstorming` skill (Phase 1 + 2 only).
- Self-grill: delegates to `grill` skill.
- Dispatch: references `using-git-worktrees` when wt slot used.
