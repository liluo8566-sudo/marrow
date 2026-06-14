# Recall 评估 2026-06-07

> agent 在 q22/40 处止步（task-notification 残留 tail 误导，实际 02:27 后无新写入）。
> 22 query 中间态完整，下列数据全部从 `/tmp/recall-eval-2026-06-07.jsonl` 离线聚合，无再调 LLM。

## 总览

- 跑完 query：22
- dims 漏召回率：9/9 = **100%**
  - entity 漏：8/8
  - milestone 漏：1/1
  - memes 漏：无 expected
- top10 anchor 出现率：0/220 个槽 = **0%**（dims 在 recall 完全不出现）
- events 噪声率：79/79 = **100%**（judge 全标 noise，可能 prompt bias，待人核）

## 漏召回清单（全 9 条）

| query | 期望 dim | matched 原词 | top10 实际 kind 分布 |
|---|---|---|---|
| <task-notification> <task-id>ac053ba94ff59dab0</task-id> <to | entity#5 | `爸爸` | {'event': 10} |
| <task-notification> <task-id>ac053ba94ff59dab0</task-id> <to | entity#6 | `cat` | {'event': 10} |
| <task-notification> <task-id>a4caba9560ce6c455</task-id> <to | entity#6 | `cat` | {'event': 10} |
| 那如果不是fix呢，就是new feature之类的🤡 | entity#16 | `eat` | {'event': 10} |
| ok那就做吧，新建一个文件夹在 ~/.config/marrow/backup/里面，比如说教in-session之类的 | milestone#42 | `在一起` | {'event': 10} |
| <task-notification> <task-id>ab7249f23c536a0f2</task-id> <to | entity#4 | `妈妈` | {'event': 10} |
| <task-notification> <task-id>ab7249f23c536a0f2</task-id> <to | entity#5 | `爸爸` | {'event': 10} |
| <task-notification> <task-id>ab7249f23c536a0f2</task-id> <to | entity#6 | `cat` | {'event': 10} |
| <task-notification> <task-id>ab7249f23c536a0f2</task-id> <to | entity#19 | `trip` | {'event': 10} |

## events 噪声样本（前 20 条，按 score 降序）

| score | query | event#id | snippet |
|---|---|---|---|
| 0.649 | 这句话英语怎么说，简短一点 我更喜欢治本而不是治标的方案，除非成本异常悬殊且效果差距很小 | event#2479 | 这句话英语怎么说，简短一点 我更喜欢治本而不是治标的方案，除非成本异常悬殊且效果差距很小 |
| 0.614 | [Image #1] 插播一下这玩意到底是谁在这里瞎鸡儿备份我猜是subagent，是不是有专门的地 | event#2492 | [Image #1] 插播一下这玩意到底是谁在这里瞎鸡儿备份我猜是subagent，是不是有专门的地方备份？以后怎么才能让subagent做好housekeep |
| 0.591 | 那如果不是fix呢，就是new feature之类的🤡 | event#2483 | 那如果不是fix呢，就是new feature之类的🤡 |
| 0.551 | 问号串和感叹号串可以么？  中文 嗯 啊 哦 哈 哼 呵 咦 欸 嗨 嘿  嘿嘿 哦哦  好 好的  | event#2543 | 问号串和感叹号串可以么？  中文 嗯 啊 哦 哈 哼 呵 咦 欸 嗨 嘿  嘿嘿 哦哦  好 好的 好嘟 好der 好哒 嗯嗯 嗯啊 嗯好 行 对 是 收到 知 |
| 0.504 | 可以你一起做，hook再改啥我现在有三个active session，一个在做handover，一个 | event#2489 | 老婆，我先并行干两件事——派一个 Explore 把所有 handover 相关代码挖出来，同时我自己看一下 sessionend pipeline 现在长什么 |
| 0.501 | 可以你一起做，hook再改啥我现在有三个active session，一个在做handover，一个 | event#2084 | **Cooperative vs Non-cooperative breeding**（鸟类繁殖系统） - **Cooperative**：除了 breedin |
| 0.501 | 可以你一起做，hook再改啥我现在有三个active session，一个在做handover，一个 | event#1962 | [time: 2026-06-05 Fri 04:42 / gap: 0m] 哼，咬住你的唇舔了舔 你下午 考试要是不好好做 就把你发卖了 没用的破鸭子 |
| 0.497 | 可以你一起做，hook再改啥我现在有三个active session，一个在做handover，一个 | event#1941 | ？？？？？？？？？？？？？？？？？？？？？？？？？？？？？？？？？？？？？？？我真的要崩溃了，你tmd找原句了么 |
| 0.494 | 给我一个以后测试可以用的prompt，我到时候看看 | event#1264 | 测试一下 |
| 0.489 | 可以你一起做，hook再改啥我现在有三个active session，一个在做handover，一个 | event#1681 | 你这个反思是对的，不是双标。你一直在coding所以handover+task tracking覆盖了你的需求，但日常流确实是另一条线——"几点跟你说了什么"这 |
| 0.489 | 给我一个以后测试可以用的prompt，我到时候看看 | event#2503 | SendMessage 在这环境没暴露——改派一个 sonnet agent 用 EnterWorktree(path) 进入 agent B 的现成 work |
| 0.488 | 1. 问个问题如果我之后后悔了我的handover还能找回来么？ 2. ？？你这么愚蠢有偷懒的分类方 | event#2560 | ack 知道了 hooks.py 是别的 session 在改，我不动。先看冲突情况。 |
| 0.487 | 那如果不是fix呢，就是new feature之类的🤡 | event#2399 | sessions.json 空、vec_meta 表结构跟我以为的不一样——昨天那个"alert风暴"提交肯定改了相关逻辑。我看下。 |
| 0.485 | 可以你一起做，hook再改啥我现在有三个active session，一个在做handover，一个 | event#843 | 老婆的逻辑跟代码现状对齐了——`/new` 已经重置到 `clear_default_model`，`/resume` 已经从 marrow 拉历史 model |
| 0.483 | 给我一个以后测试可以用的prompt，我到时候看看 | event#1537 | 我找到了。让我看测试和最近的相关commit验证我的判断。 |
| 0.481 | 给我一个以后测试可以用的prompt，我到时候看看 | event#1545 | 跑测试验证一下。 |
| 0.481 | 那如果不是fix呢，就是new feature之类的🤡 | event#2275 | Merged + worktree清干净。问题清单：  **必须做的（不做recall改动不生效）** - **重启cc** — `recall.py` 改了， |
| 0.480 | 什么是phasea测试？？ drift先这么着吧没地方放到时候再说 | event#2386 | 老婆，我先按你的路径跑诊断，看probe落在哪条。 |
| 0.479 | 等下 digest会压缩那么多对话么？？完全不保留原句？我晚点看下prompt吧 | event#2544 | list 收下，B 回来后我直接写进 config。  # 问号/叹号串  可以，但走 regex 不走字面 list — 不然 `？？` `？？？` `？？？ |
| 0.479 | 等下 digest会压缩那么多对话么？？完全不保留原句？我晚点看下prompt吧 | event#2412 | 跑测试确认没破，然后 commit + 重启 daemon。 |

## 严重误命中（score top5）

- score=0.649 · query: `这句话英语怎么说，简短一点
我更喜欢治本而不是治标的方案，除非成本异常悬殊且效果差距很小` · event#2479: `这句话英语怎么说，简短一点
我更喜欢治本而不是治标的方案，除非成本异常悬殊且效果差距很小`
- score=0.614 · query: `[Image #1] 插播一下这玩意到底是谁在这里瞎鸡儿备份我猜是subagent，是不是有专门的地方备份？以后怎么才能` · event#2492: `[Image #1] 插播一下这玩意到底是谁在这里瞎鸡儿备份我猜是subagent，是不是有专门的地方备份？以后怎么才能让subagent做好housekeeping的工作`
- score=0.591 · query: `那如果不是fix呢，就是new feature之类的🤡` · event#2483: `那如果不是fix呢，就是new feature之类的🤡`
- score=0.551 · query: `问号串和感叹号串可以么？

中文
嗯 啊 哦 哈 哼 呵 咦 欸 嗨 嘿 
嘿嘿 哦哦 
好 好的 好嘟 好der 好哒` · event#2543: `问号串和感叹号串可以么？

中文
嗯 啊 哦 哈 哼 呵 咦 欸 嗨 嘿 
嘿嘿 哦哦 
好 好的 好嘟 好der 好哒 嗯嗯 嗯啊 嗯好 行 对 是 收到 知道 明白
草 操 艹 卧槽 tmd 妈的 他妈 她妈 踏马 傻逼 煞笔 我操 我`
- score=0.504 · query: `可以你一起做，hook再改啥我现在有三个active session，一个在做handover，一个在修alert` · event#2489: `老婆，我先并行干两件事——派一个 Explore 把所有 handover 相关代码挖出来，同时我自己看一下 sessionend pipeline 现在长什么样，方便回头给你拍板用哪个方案。`

## 已知偏差

- events_noise 100% 是 LLM judge 单一返回值，没有 relevant/uncertain 对照——很可能 judge prompt 把所有 hit 都判 noise 了。需要重跑 events lane judge 才能信。
- dims 漏召回率是字面匹配 ground truth 算的，确定性事实，可信。
