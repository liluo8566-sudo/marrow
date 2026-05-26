name: day-plan
description: Daily open / evening close ritual for marrow. Morning = Scan → Brainstorm → Synthesize → Self-grill → Plan → Dispatch. Evening = review + handover update + optional night /goal. Trigger = /day-plan, /day-plan evening, (开工), (复盘).

---

# day-plan

One skill, two modes. Default = morning. Pass `evening` to switch.

User is non-coder. Lead every visible line with user-visible outcome in plain words.

## Morning mode

### 1. Scan (silent, auto)
Read: 
1. handover.md (already in context) — check any alert or open/plan
2. DESIGN.md — If we are still on right track - phase structure? new tasks meet goals?
3. DECISIONS.md — Deadlock? Any conflict among decisions? Or conflict with design?
4. FUTURE.md — Any features fit in this phase?
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
## Today

Session 1 (main) — <goal> → <outcome>
- bite-sized steps (≤8, no code)
- Done: <machine-checkable cmd>
- Dispatch: agent <type> for <subtask> | wt <slug> for <subtask>

Session 2 (main) — <goal> → <outcome>
- steps
- Done: <cmd>
- Dispatch: ...

Session 3 (main) — <goal> → <outcome>
- steps
- Done: <cmd>
- Dispatch: ...

Session ...

Constraints
- Concurrent active sessions ≤ 3 (main + wt)
- Daily main sessions 2-5; wt = independent SID (main opens)
```

Plan rejected if any Session lacks Dispatch line, or any Done lacks verifiable command.
End with one declarative line — main session starts now.

**Save plan to** `~/Desktop/NY/<slug>.md` — slug ≤4 words, no date prefix (e.g. `dashboard-rebuild.md`). Hard cap 150 lines, target ≤100.

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

## Rules
- Outcome-first in every visible line.
- All main session must keep context clean and dispatch agents / wt
- Each main session decides agent count and agent type (model is bound to agent type, e.g. Explore=Haiku / code-quality-reviewer=Sonnet).
- Bite-sized steps - max 8 - not too much details if LLM can understand what to do without guessing
- No code in plan. Done = command only.
- Plan file ≤150 lines (target ≤100). Slug ≤4 words, no date.
- CN labels in (parentheses).
- Agents/wt over inline main work when independent.
- End plan/report on declarative — no upsell (要不要 / 需要我).

## When to dispatch wt vs agent

- **agent** = single-shot task, returns a report and dies
  - Use for: code search, fact-check, file scan, code review, small patch, fetch
  - Examples: Explore (grep/find), code-quality-reviewer, fact-checker, claude (general)
- **wt** (worktree) = independent session with its own SID; main session opens it
  - Use for: long-running (hours), multi-file dev work, risky/experimental, isolation when main is busy on related code
  - Lumi never opens wt manually — main session always does

## Skill chain
- Brainstorm: borrows from `brainstorming` skill (Phase 1 + 2 only).
- Self-grill: delegates to `grill` skill.
- Dispatch: references `using-git-worktrees` when wt slot used.
