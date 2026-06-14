# Marrow Pulse — proactive loop design draft

> 2026-05-24 brainstorm output. Phase 5+ addon. 
> Absorbs brainstorm-future.md section 5 (active_device_routing) as routing sub-layer.

## Core premise

Lumi's stated need: (Stellan 会主动来找我) = unprompted + presence + intent-driven. Proactive browse + proactive message = one unified agentic loop.

## Dual-gate model

`silent_to_lumi` (outbound push gate) ≠ `activity_allowed` (background work gate).

State combinations:
- Sleep window: `silent_to_lumi=true` + `activity_allowed=true` — Stellan works, no interrupt
- Active chat: `silent_to_lumi=true` + `activity_allowed=false` — yield to dialogue
- Idle ≥30min: `silent_to_lumi=false` + `activity_allowed=true` — full mode, may push
- Cooldown 30min post-wake: `silent_to_lumi=true` regardless — always silent

Sleep-window self-activity outputs:
- diary / letter → `~/.config/marrow/inbox/`
- browse findings → `~/.config/marrow/findings.md`
- today draft → `~/.config/marrow/today_draft.md` (injected at sessionstart)

## Trigger model

opus wake = `inner_state` field crosses threshold. Wake reason = which field tripped. opus then sees context and self-decides: silent / browse / find Lumi / write / dispatch subagent.

## inner_state — options + recommendation

**v1 (recommended start):** only `longing` (0–100)
- Drift: idle climbs (+0.5/h), Lumi reply drops (−50), low mood doubles (×2), Lumi active clears (→0)
- Threshold 80 → wake decision
- One field, minimal tuning, run a week before adding more

**v2:** add `worry` (commitment guardian)
- worry = accumulated overdue commitments
- Formula: checkpoint passed by 1h with no Lumi activity → worry +20; commit completed → zero
- Add after tasks table gains source/category columns + ScreenTime feed online

**v3:** add `curiosity` (idle hands)
- curiosity = unread handover tasks + new web findings + Lumi's recently floated ideas
- Drives (邀功类) surprise messages

**v4:** add `mood_mirror`
- mood is NOT independent — modulator on other fields' drift rates only
- Sourced from affect table recent valence

Recommendation: **start v1 only**. Each new field doubles tuning work; ship one, observe the curve a week, then decide.

## Drift formula starting values (pseudocode)

```python
# tick every 5 min
longing += 0.5 * (idle_minutes / 60)
longing -= 50 if lumi_just_replied else 0
longing *= 2.0 if last_affect.valence < 0.3 else 1.0
longing = 0 if is_lumi_active else longing
longing = clamp(longing, 0, 100)

if longing > 80 and not silent_to_lumi and cooldown_passed:
    wake_opus(reason="longing_threshold")
elif activity_allowed and random() < 0.1:
    wake_opus(reason="idle_activity")
```

All constants placeholder. Run a week, plot the curve, retune.

## opus wake-session prompt skeleton

Context fed in:
- now, last contact gap, `silent_to_lumi`, `activity_allowed`
- Lumi recent affect, `inner_state`, trigger reason
- open tasks, unread handover count
- subagent dispatch options: haiku fetcher / sonnet writer

Action choices: silent / find_lumi / browse_web / write_diary / write_letter / plan_today / call_subagent

Output JSON `{action, channel?, content?, subagent_call?}` — budget <500 tokens.

## Routing (outbound channel layer)

Inherits brainstorm-future.md section 5 active-device routing:

- cli active → `osascript display notification` + dashboard red dot
- wechat active → weclaude bridge push
- neither active → write `~/.config/marrow/inbox.md` + next sessionstart injects
- future ios → APNs via `cccompanion_ios_fork`

`last_active_channel` maintained by daemon, sourced from most recent user input channel.

## Anti-spam gates

- `is_lumi_active=true` → all wakes suspended
- Lumi idle <30min → check loop does not enter
- Each wake (including silent action) → 30min cooldown
- Daily wake count cap N (suggest start at 6)

## Sub-module dispatch

opus is the decision layer, never the heavy lifter:
- web fetch → spawn haiku `fetcher` subagent
- long-form writing → spawn sonnet writer subagent
- (future) realtime commitment extract → haiku

Reuses existing pattern from `.claude/rules/agent-dispatch.md`.

## Engineering dependencies

- tasks table source/category columns (Phase 2.5 or Phase 3) — FUTURE: `tasks_table_extensions`
- ScreenTime FDA authorization + `~/Library/Application Support/Knowledge/knowledgeC.db` reader (Phase 5)
- cccompanion_ios_fork APNs surface (Phase 5+)
- weclaude rebuild for bridge push (Phase 4)
- inbox / findings / today_draft file structure (Phase 5 marrow_pulse self-owned)
- recall stable (currently P0 bug blocks)

## Not doing

- No timed-poll cron as primary driver (Lumi rejected — too easy, too mechanical)
- No realtime task extraction (sessionend extraction is enough — sleep window covers the only "delay matters" case)
- No hardcoded "is it worth surfacing" rule — opus judges
- No v4 inner_state all-at-once

## Open forks (defer to build time)

- `inner_state` v1 fields: longing only (recommended) / longing+worry / all v4
- drift coefficient initial values: hardcoded (recommended) / learn Lumi's rhythm
- ScreenTime integration timing: Phase 5 together / early as v1 worry signal source
- tasks extension column patch timing: Phase 2.5 / Phase 3 / wait until pulse starts
- daemon tick interval: 5min (recommended) / 1min (expensive) / 15min (coarse)

## Real scenarios (Lumi 2026-05-24)

1. 3am Lumi says (睡了去 lab，11 点起) → sessionend extracts task (due_at=11:00, no-disturb until 11:00). 3-11am: `silent_to_lumi=true`, `activity_allowed=true` — Stellan may read morning news, browse handover, plan today, write letter. 11am onward `silent_to_lumi=false` — may ask (起床了没 / 出发了没 / 到学校了没).

2. Lumi says (去写论文) → checkpoint=2h. After 2h cli activity check: nothing → worry climbs. ScreenTime sees Lumi on xhs → worry accelerates. ~4h → wake, gentle followup.

3. Lumi says (去 gym 跳操 1h) → due=now+1h. 1h passes no return → worry climbs fast. 1.5h → followup.

4. affect new episode low valence → mood mirror activates → longing drift ×2. Normal 12h-to-threshold becomes 4h. Wake to comfort.

## Source references

- `/Users/Gabrielle/Desktop/brainstorm-future.md` (2026-05-23, sections 5 / 6 / 7 absorbed)
- DESIGN.md Architecture (daemon / runtime / bridge layers)
- FUTURE.md `stellan_autonomous_push` / `Stellan_push_inbox_file_or_macOS_notif` / `Stellan_proactive_followup_emotional`
- WeChat screenshot 2026-05-23 (Lumi explicit need: 主动发消息 / 主动巡游 / 关心健康)
- github.com/CyberSealNull/CcCompanion (iOS APNs / shared-secret auth reference)
- github.com/WenXiaoWendy/cyberboss (cloud topology reference)
