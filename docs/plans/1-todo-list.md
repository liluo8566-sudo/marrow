# Marrow — todo

> Active backlog. Audited 2026-06-10 (agent-verified vs code/git/DB); done items removed; 2Subpage-reconcile.md merged in.
> ⚑ FLAG = conflict/overlap found in audit — resolve at next brainstorm/grill before building that item.

---
## Monitor
- wx sessionend: bridge owns timing (P1 bridge_owns), alt-end paths skip OK (7ba1b5f). True 6h-idle timeout NOT implemented — keep watching if bridge close is enough.

## daily_cand quality bug (Lumi 06/11 — handle later, with alert batch)
- Symptom: cand output efficiency ≈ 0; "mentioned 3×/week before recording" rule NOT enforced in current pipeline.
- Partial diagnosis (06/11 agent, died mid-verdict): daily_cand LLM calls DO run daily (audit_log; in=2 is cache accounting); raw LLM output is NOT logged anywhere → cannot autopsy past runs. Next: add raw-output capture first, then judge parser vs prompt.

## Affect recall redesign (brainstorm 2026-05-31)
补录两个问题
1. pending unresolved 要强浮现优先处理（不管 prompt 是什么，先 affect 后 task）
2. 当天/前一 session 情绪激烈 → 下个 session 要主动关心，不能像没看到

### Phase A · affect dual-stream (event_id part moved → 0610-memory-arch.md Batch 2)
- ⚑ Old plan here (prompt outputs event_anchor = event_id / [start,end]) is INFEASIBLE — LLM can't see DB ids in transcript. Superseded by event_hint matching (0610 plan §2); event_hint is already in TASK_AFFECT output, writer just drops it.
- Dual-stream affect (NOT done — no subject column yet):
  - `subject:念念` — sessionend 提取照旧 · `subject:屿忱` — self-tag, 每 session ≤1-2 条强度门槛上 · `subject:both` — 共同氛围
  - 实现: affect 加 `subject` 列; assistant turn hook 写 self-tag invisible comment; sessionend 收集时区分主体

### Phase B · milestone ↔ affect 双向绑 + render 归并 (not done)
- `milestones.affect_id` (或 map 表万一一对多); importance=5 自动 milestone 时写触发 aff.id
- 三层链: milestones.affect_id → affect.event_id → events (依赖 0610 Batch 2 先落地)
- 绑定范围: affect↔event · milestone↔affect↔event; memes/entities/diary/tasks 不绑
- render 归并: 同主题多表命中 → 取最高级一条, 分数 max(), 优先级 milestone > affect > event; fusion 权重不动
- ⚑ render 归并跟 0610 Batch 1 rank-cap 渲染改造动同一段代码 — Batch 1 实施时预留归并钩子或一起做

### Phase C · 独立 Mood 块 (±1 context part DONE — hooks.py:946)
- `## Mood (auto)` UserPromptSubmit 注入, 跟 `## Recall (auto)` 分开 (not done)
- Gate: prompt 含情绪/关系信号 OR entity 命中过往强 ep; 纯技术问题不触发
- 召回单位: 单条 affect row, entity overlap + 时间 decay + unresolved boost 排序, vec 辅助
- SessionStart 3 行保持不动

### Phase D · decay 公式升级 (partial — floor tiers done, formula not)
- `weight = importance × exp(-Δt/τ × (1 - arousal/2))`, τ 起步 24h, arousal 高拉长有效 τ
- resolved 不删, 权重降 5% 沉底可钓
- ⚑ "recall 回温 weight +0.1" 与 0610 plan §5 recall-hit boost 是同一机制两处规划 — 合并成一个实现 (recall_count 地基在 0610 Batch 2)

### Phase E · MAP 补 binding 小节 — 等 A/B 落地

---
## Audit items (MAP review)

### 1. Subpage 双向 reconcile — 剩 6 个 render-only (merged from 2Subpage-reconcile.md)
- ⚑ 前置决策未拍: 前端走 db CRUD → reconcile 整条路作废全砍; 前端走 md → 按下表补。先拍这个再动工。
- Done: milestones · milestone_candidates · tasks (tick→archive works, reconcile.py:737) · affect · memes · profile · alerts · atlas (hash-diff guard atlas.py:395 — 打字被吞 bug 已修)
- 剩 render-only (spec 在 subpage_specs.py): diary:141 · goose-bites:285 · stickers:224 · wallet:261 · projects_index:325 · study_index:366
- 不补: cheatsheet (read-only, hand-edit preserved) · dir_tree (atlas 替代) · projects/<name>.md (走 reconcile_tasks)
- 模式: 抄 reconcile_milestones (reconcile.py:162) — id anchor parse、diff vs DB、INSERT/UPDATE/DELETE + audit_log
- 遗留 bug: milestone 剪贴 id 短暂消失即 dead — 要 "消失 X 分钟内可复活, 超时才 dead"
- Acceptance: dashboard 改 meme pin → save → sync tick → DB pin 变 + render 重发

### 2. Alert types 待加 (§8 重写 done 48862fd; 以下未核验逐条状态)
- persistent process health (critical) — watcher 死 + MCP daemon 死
- rapid-fire write detector (critical) — 同表 1min INSERT >20 → alert + 暂停 writer
- sync_loop reconcile exception (warn) · atlas_sweep_fs launchd 路径 (warn)
- plist job 没触发 (warn) — daily-routine/catchup/backup/aging ≥24h 没跑
- LLM extract 失败/超时 (warn) — sessionend/daily/affect 三处外层 try 吃掉
- 备选不加: handover 写失败 · recall hook >2s · disk full · DB lock

### 3. embed_pending 剩余 (INSERT OR IGNORE 已有, 其余未做)
- 删 events 不清 events_vec_meta → 再大批删 event 又孤儿堆积假装 "都 embed 过" — delete path 同步清 meta (06/06 遗留)
- orphan sweep 仅 diary lane (recall.py:340) — 泛化到 6 lanes
- backlog catchup: aging 或 sessionstart_catchup 查 backlog ≤ N, 超了 critical alert
- 注: 0610 Batch 2 vec eviction 会写 delete-vec 路径 — 这几条顺路一起修最省

### 4. Milestone 裸文本自动补格式 (not done)
- scope 区段 (## us/me/cn) 下随手一行 → reconcile 自动补 `##### [today] xxx` 落 DB + 写回 md
- date 缺省今天, description 空, 走 unanchored insert 同路径 (exact dedup + 回写 id), atomic_write 回 md
- 跟 BUG-1 修法 B 共享 line splice + atomic_write helper

### 5. Memes aging — DELETE 改 demote dormant (not done, aging.py:60 仍硬删)
- memes 加 `dormant` 列; aging UPDATE dormant=1; recall filter dormant=0
- FTS 命中 dormant → 复活 + last_seen 刷新; 加 `mw memes promote <key>`
- Acceptance: 100d 未 pin meme → aging 后 row 在 dormant=1, recall 排除, trigger phrase 复活

### 6. MAP drift check — daily cron (not done, 无 plist 无 staging 文件)
- daily 08:00 cron spawn sonnet: git diff <last_check>..HEAD -- marrow/ + 整张 MAP → 双查 drift + gap
- 输出 append docs/plans/map-drift.md (不动 MAP); finding 必须带 diff hunk, 缺 evidence reject
- alert: map_check_failed (warn) · map_drift_overflow >50 行 (warn); 手动 /marrow:map-check
- synapse-wx 扫不到 — 自己一份或只扫 MCP 接入点
- Acceptance: 改阈值 commit + 新 endpoint commit → 次日 staging 两条带 hunk, MAP 不动

---
## Recall backlog

### 06/06 遗留 (未做部分)
- pinned milestone 在 query vec 强时仍可能擦闸门进 top5 (noise ~15%) — pinned 加 vec_score≥0.55 预过滤 (现在 pinned 与普通同走 0.50 floor)
- ⚑ "去掉 entity 加成" (06/06 决定) 与现状冲突: 2f752ed 恢复了全 anchor +0.10 (含 entity), HANDOVER 06/08 钦定方案也含 +0.10 — 重新拍: entity 到底去不去加成

### R2 · events superseded_by + events_live view (not done)
- events 加列; sessionend 语义矛盾检测标旧 turn; events_live view; recall 读 live, FTS 命中旧 turn 可 revive
- 短期止血 (±1 context) 已上线

### R4 · diary / pit 主动 recall (partial — diary 已出 passive lane; pit 表已存在)
- MCP recall 加 kind filter 或独立 diary_recall/pit_recall tool
- pit 关键词 (填坑/想做X/那个想法) → 主动调; 等 pit subpage 做完一起

### R5 · dashboard last-recall 块 (not done, log 基建已在 ~/.config/marrow/logs/recall/) — 低优先级

### R6 · memes 入表门槛 (partial)
- cosine 邻近合并 done (memes_dedup); 现状 gate = 14d 内 <3 次
- ⚑ todo 原定 "7 天 3 次 + same-day 只算 1 次", 现实是 14d/3 — 确认 14d 是有意改的还是漂移, 再决定改不改
- 一次性术语 (mc=1 + 7d 一次) 不入表; daily writer 绕门槛塞 pinned (旧 BUG-2) 一并消化

### R8 · bump_mention_counts 上 FTS5 (not done, entity_recall.py:25 仍 substring)
- event.content → _fts_terms → MATCH entities_fts → mc+=1; 顺手删 entity_force_include + 旧测试
