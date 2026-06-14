# Marrow — todo

> Active backlog. Audited 2026-06-10 (agent-verified vs code/git/DB); done items removed; 2Subpage-reconcile.md merged in.
> ⚑ FLAG = conflict/overlap found in audit — resolve at next brainstorm/grill before building that item.

---

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

### Phase D · decay 公式升级 (partial — floor tiers done, formula not) - 先跟我确认不要按照下面说的直接写，我要了解一下机制
- `weight = importance × exp(-Δt/τ × (1 - arousal/2))`, τ 起步 24h, arousal 高拉长有效 τ
- resolved 不删, 权重降 5% 沉底可钓
- ⚑ "recall 回温 weight +0.1" 与 0610 plan §5 recall-hit boost 是同一机制两处规划 — 合并成一个实现 (recall_count 地基在 0610 Batch 2)

### Phase E · MAP 补 binding 小节 — 等 A/B 落地

---
## Audit items (MAP review)

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

### R5 · dashboard last-recall 块 (not done, log 基建已在 ~/.config/marrow/logs/recall/)
- 可以做，等稳定了代替log - Monitor zone可以是单独一个subpage

### R6 · memes 入表门槛 (partial)
- cosine 邻近合并 done (memes_dedup); 现状 gate = 14d 内 <3 次
- 一次性术语 (mc=1 + 7d 一次) 不入表; daily writer 绕门槛塞 pinned (旧 BUG-2) 一并消化

