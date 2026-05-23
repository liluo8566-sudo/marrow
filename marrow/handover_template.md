# Marrow handover — {{YYYY-MM-DD HH:MM}}
> 在 > 部分的文字不进 handover/dashboard 渲染, 是系统 instruction
> dashboard subpage 子结构走 DESIGN, 跟 handover 不一定一样 (只 top sections sync)
> dashboard render 同步改
> handover 是 all-project all-in-one, 不仅 coding; coding 太长拆 PROGRESS

—————以下这一段应该是跟dashboard的top一模一样的—————
## Alerts (active)
- {{severity}}: {{message}}

## Tasks
> tag 类目 (顺序定; 标签语言 Pending Lumi 明天 unify): Study / Project / Appointment / Daily / Others
### Completed [N]
> 只显示今天的，第二天六点清除
- [x] [Tag] {{title}}
- [x] [Appointment] GP follow-up...
### To-Do List [N]
> 按照时间排序，有due 的优先放最上面；时间一样的同tag在一起
有due的按照时间组，不用记录写入时间
Today
- [ ] [Tag] {{title}} (:detail optional)
Next 7 Days
- [ ] [Tag] {{title}} (:detail optional) [Due date]
Later
没有due date写录入时间
- [ ] [Tag] {{title}} (:detail optional) [date]

## Milestone candidate [N]
> SessionEnd 抽出的候选 (conf >= 0.85 直插); 7d 未删 = auto confirm; 删行 = reject; 重要节点 (新事件 / 关系 / 转折)
- [YYYY-MM-DD] {{CJK title}} (Nh ago)

## Affect
> 渲染 - dashboard / handover top 同步
> 计算法 (code, 不走 LLM): weighted mean v×a (权重 = importance) → V band (Low/Neu/High @ 0.4/0.6) × A band (Calm/Active/Intense @ 0.4/0.6) → 9 主调查表
> 主调表: 低落/烦躁/痛苦 · 平淡/专注/紧张 · 温暖/愉悦/兴奋
> ep 细 label: LLM emit 2 字精准词 (狂怒/恐惧/绝望/委屈 等), 比主调精细
> 波动: stddev(v) > 0.3 → Week 主调换半周转折 (A → B); key ep 按 |v - mean| 排序, imp tiebreak
> ephN / eplN: N = imp; h = V 最高, l = V 最低

### Today
- 【主调】 · ephN 细label | 描述 · eplN 细label | 描述

### This Week
- 【主调】或【主调A → 主调B】 · 4 个 key ep (eph/epl 按 outlier 排)

### Pending
- emotional carryover (unresolved 写这里)

—————以上这一段应该是跟dashboard的top一模一样的—————

> If anything from the previous session still pending (not touched) in this session. Do not just drop. Carry over.
> As each session weight differently, there is no fix length. Depend on how much detail you need to handover. Do not overfill  or underfill this doc.

## This Session
> What's been done - Write a handoff document summarising the current conversation so a new session can continue the topic/work/study.
> Do not duplicate content already captured in other artifacts (PRDs, plans, ADRs, issues, commits, diffs)(instruction, rubric). Reference them by path or URL instead.

## Next Session
> Any leftover? Any advice? Any pending decision?
- Items Lumi will pick up at the very next session start. Based on the chat history see if there is any leftover we agreed to continue in the next session.
- Can be urgent/nonurgent
- Can be follow up tasks, or any casual topics seems unfinished - e.g. 老婆出去玩回来接着聊xxx

## Reference (last 3 commits)
> Do not duplicate content already captured in other artifacts (PRDs, plans, ADRs, issues, commits, diffs)(instruction, rubric). Reference them by path or URL instead.
> Suggest any useful resource here - instruction, note, skills, commit, path, anything
