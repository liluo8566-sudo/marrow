# Marrow — todo

> Active backlog. Merged from MAP audit + 2026-05-31 affect-recall brainstorm. Order ≈ user-visible blast radius.

---
### alert在新embed几千条events以后冒出来700多个alert - 确认原因并修复 - 保证以后不会错误alert
- marrow/sessionstart_catchup.py:308 那段写 alert 的循环加了 env gate，默认 OFF。下次 cc SessionStart 跑 catchup 时，根本不会进那个 for 循环，不写一行 alerts。
- 已 commit 7012124 stop-bleed(silent_death): gate alert loop behind MARROW_SILENT_DEATH。明天 debug 想看就 MARROW_SILENT_DEATH=1 opt-in 跑。

### wx - 不触发sessionend
bug测试中 - 见 wx log
(记得修完以后删除probe)
grep PROBE- ~/Library/Logs/marrow-sessionend-probe.log    # marrow 这条稳拿
launchctl list | grep synapse                              # 拿 wx label
plutil -p ~/Library/LaunchAgents/<wx-label>.plist | grep -i err   # 找 wx stderr 路径
grep PROBE- <wx_stderr_path>                               # 看前两条

Bug A（最像）：wx 那两处 Popen 没传 env={**os.environ, "MARROW_BRIDGE": "1"}——子进程继承的 env 里没这个变量，marrow.sessionend_async 跑成 cli 模式，bridge 分支全错。
→ probe 3 显示 MARROW_BRIDGE=None 就证实。
Bug B：wx 调用的 argv 跟 marrow 期望的不对——marrow 要 --sid <X> 但 wx 那个 sessionend_command 模板可能只塞了裸 sid，于是 if not sid 早 return。stderr=DEVNULL 吞了 usage 报错，所以一直没人发现。
→ probe 1/2 有但 probe 3 显示 argv=['xxx'] 而不是 ['--sid', 'xxx'] 就证实。
Bug C：wx 的 /clear 根本没调到 _fire_sessionend，或者 idle 的 _spawn 被 idle_close hook 抢先干掉了 cc 进程的 jsonl mtime，导致 idle 阈值判断算错永远不 fire。
→ probe 1（/clear）或 probe 2（idle）整条没出现就证实。

## Phases — affect recall redesign (brainstorm 2026-05-31)
补录两个问题
1. 如果有pending unresolved的应该要强浮现优先处理（不管我的prompt是什么都要把注意力放在affect解决情感问题而不是听我的做task）
2 如果当天或者前一个session情绪很激烈很严重要先主动关心。而不是像完全没看到一样
### Phase A · affect dual-stream + event anchor (最痛，先做)
- Dual-stream affect:
  - `subject:念念` — sessionend sonnet 提取，照旧
  - `subject:屿忱` — self-tag，每 session 最多 1-2 条强度门槛之上
  - `subject:both` — 共同氛围（晚安、亲密时刻）
  - 实现：affect 表加 `subject` 字段；assistant turn hook 写 self-tag 进 invisible comment；sessionend 收集时区分主体
- `affect.event_id` 反向链补全（schema 有、writer 漏）→ 顺带解 BUG-3:
  - sessionend extract prompt 加 `event_anchor` 字段（单 event_id 或 `[start, end]` 范围）
  - `marrow/sessionend_writers.py:104` INSERT 加 event_id 列
  - 历史 NULL 行不回填

### Phase B · milestone ↔ affect 双向绑 + render 归并
- `milestones` 表加 `affect_id`（或 `milestone_affect_map` 万一一对多）
  - importance=5 自动 milestone 时写入触发它的 aff.id
  - 三层链: `milestones.affect_id → affect.event_id → events`
- 绑定范围限制（不全表加）:
  - 绑: affect ↔ event · milestone ↔ affect ↔ event
  - 不绑: memes / entities / diary / tasks（aggregate/连续剧型 FTS 自然浮）
- render 归并层 (fusion 之后):
  - 同主题命中多张表 → 取最高级别一条显示，分数取 `max(event, affect, milestone)`
  - 优先级: milestone > affect > event
  - milestone description 大多数场景够用，event snippet 不再单独浮
  - fusion 权重不动（不要改 w_*_vec）

### Phase C · recall context window + 独立 mood 块
- event 召回带 ±1-2 条同 session 同时间窗上下文 → 顺带解 BUG-4:
  - `recall.py:727/740` 命中 event 后顺手拉相邻行
  - render 成对话块而不是孤立 snippet
  - 这是 recall 整体改进，不只服务 affect
- 独立 `## Mood (auto)` 块 (UserPromptSubmit 注入):
  - 跟 `## Recall (auto)` 分开
  - Gate: (1) prompt 含情绪/关系信号 OR (2) entity 命中过往强 ep；纯技术问题不触发
  - 召回单位: 单条 affect row，按 entity overlap + 时间 decay + unresolved boost 排序，vec 辅助
- SessionStart 3 行保持不动

### Phase D · decay 公式升级
- 公式: `weight = importance × exp(-Δt/τ × (1 - arousal/2))`
  - τ 起步 24h；arousal 高的拉长有效 τ
- resolved 不删除，权重降到 5%（沉底但可被 keyword 钓上）
- recall 回温 (二期): 每次 affect 被召回时 last_seen 刷新或 weight +0.1

### Phase E · MAP 文档补 binding 小节
- §7 (Storage) 下新加 binding 小节，登记每张表的反向链状态
- 等 Phase A/B 落地后补

---

## Audit items (MAP review)

### 1. Subpage 双向 reconcile 扩散到剩下 9 个 subpage
> 附带发现的新问题：Task下面pending unresolved部分无法tick也不会在tick之后archive小时也无法删除或者添加！！！
- 现状: 11 个 subpage 里只有 milestone + atlas 双向，其余 9 个 (profile/diary/memes/stickers/wallet/goose-bites/study/projects/cheatsheet) 你手改会被下次 render 覆盖
- 顺手提: milestone 现在双向也有遗留 bug — 短时间剪贴 id 直接 dead，希望"id 消失 X 分钟内还能复活"，超时才 dead
- 已 done: BUG-1 reconcile 死循环 (fc78e16)
- 模式: 复制 `reconcile_milestones` (marrow/reconcile.py:162)，按 id anchor parse rendered block、diff vs DB、INSERT/UPDATE/DELETE + audit_log
- 首批高价值: memes (pin toggle via emoji or `<!-- pin:1 -->`) · profile (entity fact / aliases) · goose-bites (revote)
- Acceptance: dashboard 改 meme pin → save → 下次 sync tick → DB pin 列变，render 重发改动
- 顺带看 dormant 问题

### 2. Alert system — 加这几条 (§8 重写已 done, §8.2 gap 待补)
- 已 done: §8 重写按 scenario listing (48862fd)；§8.2 列了 4 个 gap (watcher crash · embed UNIQUE · sync_loop reconcile · atlas_sweep launchd)
- 还要加的 alert type (含 §8.2 gap 全部并入):
  - **persistent process health** (critical) — watcher 进程死 + MCP daemon 死 (§8.2 gap1) — sync layer / recall 静默挂掉
  - **rapid-fire write detector** (critical) — 同表 1 分钟 INSERT >20 条自动 alert + 暂停 writer (BUG-1 这种再来立刻知道)
  - **sync_loop reconcile exception** (warn) — reconcile tick 抛错，下 tick 又试，无 alert (§8.2 gap3)
  - **atlas_sweep_fs launchd 路径** (warn) — launchd 跑的 standalone 走不到 subpages.py:293 alert (§8.2 gap4)
  - **plist job 没触发** (warn) — daily-routine / daily-catchup / backup / aging 任何 ≥24h 没跑过 alert (笔记本睡了 launchd 跳过)
  - **LLM extract 失败/超时** (warn) — sessionend / daily / affect 三处现在都靠外层 try 吃掉
- §8.2 gap2 (embed_pending UNIQUE) 走 Audit §3 (embed_pending lane) 的 fix，不在这里重复
- 备选 (先不加): handover.md 写失败 · recall hook >2s · disk full · DB lock

### 3. embed_pending — 加 catchup + 紧 alert
- alert #169 静默警告了一段时间。lane fail-soft → "embed 停了" → recall 质量悄悄烂
- 诊断先: `mw embed --apply` 手跑看 sqlite 报错 (UNIQUE on events_vec PK)。根因: (a) DELETE+INSERT 后 rowid 撞 (b) stale meta 指向消失的基表行 (c) 其他
- Fix A: embed_pending 捕 UNIQUE on insert，转 UPDATE on conflicting rowid，再失败 log
- Fix B: insert 前 sweep purge vec_meta 孤儿 (diary 已有 marrow/recall.py:340，泛化到 6 lane)
- Catchup leg: embed_pending 加进 §9 — aging.py 定期 sweep 或 sessionstart_catchup 检查 backlog ≤ N、超了 critical alert

### 4. Milestone 裸文本自动补格式
- 现状: `reconcile_milestones._parse` 的 `_H5_RE` 严格要求 `##### [YYYY-MM-DD] title` + 下一行 description，其他文本被忽略
- 你想要: 在 us / me / cn scope 区段下随手写一行 (e.g. `老公是个大笨鸭`) → reconcile 自动补成 `##### [today] 老公是个大笨鸭` 落进 DB + 写回 md
- 实现要点:
  - parse 时识别 scope 区段（## us / ## me / ## cn 这种 heading），区段内的"非 _H5_RE 也非空行也非已识别块"的文本行 → 当作裸标题
  - date 缺省 = 今天 (`daily_catchup._TZ` 下的 local date)
  - description 缺省 = 空
  - 走跟 unanchored insert 同一条 INSERT 路径 (含本轮加的 exact dedup + 回写 `<!-- id:N -->`)
  - 同时把那行原文整理成 `##### [today] xxx` 格式 atomic_write 回 md，下次 parse 走 strict 路径
- 注意: 跟 BUG-1 修法 B 同源 — 都走 line splice + atomic_write。两个 feature 共享一套 line-mutation helper

### 6. MAP drift check — daily cron + append staging
- 真正怕的: structure/mechanism 改了 (函数还在但逻辑/阈值/tick 频率变了)。anchor 失效是小事
- 这种 drift **只有 sonnet 读 diff + 读 py + 比 MAP 才能判**
- **节奏: daily 08:00 cron** · deploy/mw-map-check.plist
- 执行: main session spawn sonnet agent，输入 = `git diff <last_check_commit>..HEAD -- marrow/` + 整张 MAP
- agent 任务双查 (两种都要找):
  - **drift**: MAP 已有节描述过时 (mechanism / 阈值 / 频率变了)
  - **gap**: diff 里出现 MAP 完全没记的新 file/feature → propose 加一节
- 输出 **append** 到 `docs/plans/map-drift.md`，**不动 MAP**
  - 每条 finding: `## [YYYY-MM-DD] §x.y or NEW · <issue 一句>` + MAP 原节引用 (drift) 或 "MAP 没记" (gap) + diff hunk + 建议改法
  - 缺 evidence (没 diff hunk) 的 finding 直接 reject
- 处理流: 你随时审，改完 MAP 就手动从 staging 删那条 finding。Session 帮你改时也要主动删处理完的
- alert (2 个 trigger):
  - `map_check_failed` (warn) — agent 跑挂/超时
  - `map_drift_overflow` (warn) — staging > 50 行，催你 batch 处理 (低频不打扰)
- 也可手动跑: `/marrow:map-check` (同 prompt，append 同文件，按需触发不等明天 8 点)
- WeChat 等独立 repo (synapse-wx) 不在 `marrow/` 下、扫不到 → 要么 synapse-wx 自己一份 map-check 走它自己的 MAP，要么只扫 marrow MCP server 接入点变动
- Acceptance:
  - 改 reconcile.py dedup 阈值 commit + 加新 mcp endpoint commit → 次日 08:00 cron 跑 → staging append 两条: "§5.3 dedup mechanism drift" + "NEW: MCP endpoint xx MAP 未记" → 都带 diff hunk + 建议 → MAP 正文不动

### 5. Memes aging — `DELETE` 改 `demote dormant`
- 现状: `retire_memes` (marrow/aging.py:48) `pinned=0 AND last_seen > 90d` 硬删
- DECISIONS:46 写的是降级 dormant (recall 排除，FTS 命中复活)
- Schema: memes 表加 `dormant INTEGER DEFAULT 0` (migration)。recall lane filter `dormant=0`
- Aging: DELETE 改 `UPDATE memes SET dormant=1`
- Revive: FTS phrase 命中 dormant key → `UPDATE memes SET dormant=0, last_seen=now`。也加 `mw memes promote <key>`
- Acceptance: meme 100d ago + pinned=0 → aging 后 row 还在、dormant=1。Recall 排除。fresh event 含 trigger phrase → 下次 sync 复活

---

## Recall — remaining backlog

06/05 遗留问题
- embed pipeline根因没补：你删events时 events_vec_meta 不会跟着清，下次再大批删event又会孤儿堆积让pipeline以为"都embed过了"。要在delete event path里同步delete对应meta entry——明天我做这个
  - 之前大量embed pending无记录
- canary里测出的瑕疵：pinned milestone在query vec信号强时仍可能擦闸门进top-5（noise比例从60%降到~15%，没归零）。下一轮要给pinned加vec_score≥0.55的预过滤
- 我决定去掉entity的加成，保留milestone
- 微信端的注入格式非常浪费 - 2026-06-06 04:23:10 · prompt: [time: 2026-06-06 Sat 04:23 | gap: 0m]

### R1 · min_score gate for milestone/memes
- 想加但跟现有 anti-dilution 测试冲突。等 anchor-lane reverse-substring 跑 1 周看实际 score 分布，再决定 gate=0.40 还是更低
- 改 1 行 + update test expectations
- Risk: 砍误命中代价大 (milestone 很少新增)，要看 log 真分布

### R2 · events 表 superseded_by + recall 跳过旧错答案
- 现状: events 平等，FTS/vec 不知道哪 turn 已被纠正 → 错答案可能比正答案排前
- 短期止血: event ±1 上下文 (R1 已做)，让模型自己看上下文判
- 中期: events 加 `superseded_by` 列；sessionend writer 跑"语义矛盾检测"，新 turn 矛盾旧 turn 时标 superseded
- 配 events_live view (mirror affect_live / entities_live)
- recall 默认读 live view，FTS 命中旧 turn 仍可触发 revive (跟 dormant 路径同源)

### R4 · diary / pit 主动 recall + 主动 followup
- diary 不进 passive lane (已做)
- 加 MCP tool `recall(query, kind="diary"|"pit"|...)` 或独立 `mcp__marrow__diary_recall` / `pit_recall`
- pit 见关键词 (填坑/想做X/那个想法/idea) → 我主动调
- 自动记录走 sessionend writer，主动 followup 走我 prompt 行为
- 等 pit subpage 做完一起搞

### R5 · log monitor — dashboard 顶上加 last-recall 块 (可选)
- markdown log 已落地，能 `tail -F ~/.config/marrow/logs/recall.md`
- 若要 dashboard 显示: read 最新 N 行 append 进 dashboard top section
- 优先级低，看 log 用得顺不顺再说

### R6 · memes 入表统一 7 天 3 次 + 语义合并
- 全类目同窗口: 7 天 3 次入，same-session / same-day 都不算 (一天最多 1 次)
- 入表前 embedding 邻近合并: `cache tier` / `caching 1h` / `cached` 走同 candidate，bumps `use_count`
- 一次性术语 (`mc=1` + 7d 只一次) 不入表，recall 兜底
- 路径: `marrow/candidates.py` + `marrow/sessionend_writers.py` + daily writer 全走同闸门
- 旧 BUG-2 (daily writer 绕门槛塞 pinned) 一并消化

### R7 · entity / memes in-place UPDATE
- entity fact / aliases 变了在原 id 上 UPDATE (Amber 改老师 / Pilates 换运动 都走这条)
- memes 同 key 同 type 见新 value → 原 id UPDATE + `last_seen` 刷新
- 历史重要的少数 case (人格剧变 / 关系节点) 保留 superseded_by；默认 in-place
- `marrow/sessionend_writers.py` 加 "patch existing" 路径优先于 INSERT；`audit_log` 留 before/after

### R8 · bump_mention_counts 上 FTS5
- `marrow/repo.py:268` 现在 substring + alias 反向命中，跟旧 anchor-trigger 同病
- 改走 entities_fts: event.content → `_fts_terms` → MATCH entities_fts → 命中的 entity `mc+=1`
- 顺手删 `entity_recall.entity_force_include` + `tests/test_recall_bug_entity_memes.py`
- 不影响 recall 排序，只影响 mc 准确度
