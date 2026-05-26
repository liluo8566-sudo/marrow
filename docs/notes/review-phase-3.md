# Phase 3 Review ✅

> 原 review 2026-05-26 03:xx (HEAD=426abeb)
> 复核 2026-05-26 20:35 (HEAD=49a6eb0, +13 commits) — 12 条问题里 8 条已修/虚假，删之；2 条真实，2 条状态不清
> pytest 530 pass / 1 skip · 工作区 clean

---

## TL;DR (复核后)

- Phase 3 Plan M **5 wave 全部合并到 main**，md=SoT 反转完成。`wt-user-edit-fix` 5 commit 已落地（149221a / 1b2366c / 640e088 / cbdfa38 / 6281e5a 全在 main 历史）。
- 测试 530 全绿，代码质量 **good**。
- **真实剩余问题：HIGH-1（已降级）+ MED-3（低风险）+ 2 条状态不清**，可以直接开 Phase 4 或顺手清完。
- 原报告 §10 Lumi 更正块（provider 耦合误判）保留在文末。

---

## 1 · Phase 3 Plan M 交付清单（13 DONE，未变）

| Wave | Commit | 交付 |
|---|---|---|
| md=SoT 基础 | `b426962` | `md_index` 表 + `MdIndex` 类 + watchdog watcher + 三路根 |
| B | `43214d0` | 8 子页面 inserter 模式 + 新模块 `inserter.py` + `subpage_specs.py` |
| D | `5cedc6d` | `write_dashboard` 翻 inserter；hash-skip；`tasks/milestone_cand` 入 RECONCILED；`MdIndex.is_tombstoned()` |
| F | `bc6181e` | `handover_render._new_store()` → MdIndex 5 行 adapter；`mw refresh` scan-first |
| H | `bcaf553` | handover 状态轴；STATE/NARRATIVE 拆 2 call；`lifecycle:start/end` audit marker |
| prompt | `167c8cd` `426abeb` | sessionend_prompts: [N] tag、[P] pin、project max 2/day、prefix 规则、OPEN/PLAN 边界 |
| wt-user-edit-fix | `1b7dc84` 等 | sync_file 拆 full + observe、dashboard task/affect 双向回写、e2e 测试 |

---

## 2 · 真实剩余问题

### 🟠 HIGH-1（已降级）— DB-before-md 写入顺序

**位置**：`marrow/dashboard.py:128,134,141`

**原说法**：`store.record_block(path, bid, _hash(fresh_body))` 在 `_atomic_write(path, ...)` 之前调用，I/O 失败永久 hash desync。

**复核结论**：顺序仍然是 DB-before-md，**但攻击面已被 sync_file_observe 拆分大幅压缩**——watcher 不再覆盖 baseline，只有 inserter 路径会写 hash；inserter 自身的 record_block + _atomic_write 之间仍有 crash 窗口，但实际影响只剩"crash 后下次 render 该 block 被识别为用户编辑、保留旧内容"，不再是「永久 desync」级别。

**建议**：仍值得修——调换为 _atomic_write 成功后再 record_block。30 min 一个 worktree agent。

---

### 🟡 MEDIUM-3（低风险）— `_run_writer` 窄异常 catch

**位置**：`marrow/sessionend_async.py:294`

**问题**：只 catch `(ValueError, RuntimeError, TypeError, KeyError)`，`sqlite3.OperationalError` / `OSError` 逃逸到外层 `except Exception`（sessionend_async.py:279），整个 session 报 `fail` 而非单 writer 标 partial。

**复核结论**：真实，但外层 catch 保护，不会真 silent-death。后果只是粒度粗了一点。

**建议**：改 `except Exception`，audit 行标具体 writer 名。10 min。

---

## 3 · 状态不清（需要确认设计意图）

### LOW — inserter h3/h4 匹配
原报告引用 `marrow/inserter.py:205` 的 `text.find("\n##")`，当前代码该位置已无此 pattern。grep 整个 marrow/ 找不到对应代码——可能重构掉了或挪位置了。**如果仍想确认**：手动跑一次 diary/goose 多 h3 子页 inserter，看新行是否插对位置。

### MISSING — `body_nonempty` filter
原报告说 `marrow/recall.py` 缺 entity recall 安全网。当前 recall.py 无 `body_nonempty` 函数或同义实现。两种可能：(a) 从未实现且仍需要；(b) 设计上不需要。需要 Lumi 拍：entity recall 是不是确实会召回 body 为空的行？

---

## 4 · 已修复 / 虚假（删除清单）

| 原条目 | 状态 | 证据 |
|---|---|---|
| HIGH-2 tombstone 两次事务 | ✅ 已修 | `tombstone.py` 已被 `MdIndexTombstoneStore` 替换，单表 per-row tombstone_at 语义，二阶段写入不会产生孤儿行 |
| MED-1 sessionend tail vs watcher debounce | ✅ 已修 | watcher 改用 `sync_file_observe`（watcher.py:193），不再覆盖 baseline；窗口消失 |
| MED-2 popen_detach fd "泄露" | ❌ 虚假 | `popen_detach.py:26` 注释 `# noqa: WPS515 — intentionally not closed; child owns it`，pipeline §3 fire-and-forget 设计本意 |
| MED-4 silent-death "未感知" | ❌ 虚假 | popen_detach 不审计是设计；silent-death 检测在 `_write_final_audit`（sessionend_async.py:154-192）通过 start 行无 terminal 行检测 |
| LOW goose render 空格 | ✅ 已修 | `subpage_specs.py:302` 已是 `f"- [{r['date']}] {r['bites']}"` |
| DRIFT sync_file 冷启动 | ✅ 已修 | commit `1b7dc84` (merge agent-a97246ef) + md_index.py:184-189 `if not p.exists(): return report` 短路 |
| DOC-DRIFT DESIGN.md:106 Wave E | ❌ 虚假 / 已修 | 当前 DESIGN.md:106 无 "Wave E projects" 字样 |

---

## 5 · Blind reviewer 反推 gap（仍可能有效，未复核）

1. **watchdog 端到端链路冒烟测试** —— watcher launchd 装了，但 "md 改动 → sync_file → inserter 写回" 整链路没有冒烟测试记录。
2. **8 子页面之外的写入面 SoT 状态不明** —— entities / affects / sessions 等写入面有没有走 inserter 翻转，DONE list 沉默。
3. **短会话 / silent-death 场景下 handover SoT 降级** —— md 是 SoT 的前提是 md 有内容；如果 sessionend 没写入，承诺失效。

---

## 6 · 产品视角（保留原文，未复核）

> 这是个绑定 Claude Code 终端的「个人记忆系统」。会偷偷读你每次跟 AI 的对话、提取重点、按主题归档（任务、情绪、人物、梗、日记、里程碑）写进 SQLite，再渲染成每日仪表盘 + 若干分页 md，让下一次对话有「前情提要」。

**用户体感**：桌面 `dashboard.md` 出现 Alerts / Tasks / 里程碑 / 情绪曲线 / 分页链接；点进去看到 AI 自动整理的家人档案、感情线、日记、内部梗、鹅儿子吐槽合集；session 结束几秒后这些自动更新——像有人替你写日记并整理通讯录。

**工程评价**：
- 完成度比一般个人项目高出一截：~2万行 / 531 测试 / SQLite-vec + FTS5 双路召回 / launchd 6 个 plist / atomic write + 文件锁 + audit_log。**不是 hack**。
- 可读性中等偏上，模块切得细（38 文件大多 <300 行），命名一致。
- 测试覆盖到子页渲染 / reconcile / tombstone / cold-start bug 这种细枝末节，说明作者真在用。
- **外人最担心**：单用户单机 SQLite + 本地 launchd，崩了就崩了，没看到云端兜底或多设备同步。
- ~~深度耦合 Claude Code~~ —— **见 §10 更正**，结论错。

---

## 7 · 建议优先级（复核后清理）

| 优先级 | 项 | 工作量 |
|---|---|---|
| P1 | 修 HIGH-1 调换 record_block / _atomic_write 顺序 | 30 min worktree agent |
| P1 | 修 MED-3 异常 catch 范围 | 10 min |
| P2 | 确认 inserter h3/h4 LOW 是否已重构掉 | grep + 1 case 手测 |
| P2 | 确认 recall.py body_nonempty filter 是否真需要 | 与 Lumi 对齐意图 |
| P3 | watchdog 端到端冒烟测试 | 1-2 h 集成测试 |

可以一个 worktree agent 一气修 HIGH-1 + MED-3 + 顺手清两条 LOW/MISSING。

---

## 8 · 整体评分（复核后）

| 维度 | 评级 | 备注 |
|---|---|---|
| Plan M 目标达成 | 🟢 高 | 主路径通，wt-user-edit-fix 5 commit 已合并 |
| 代码质量 | 🟢 good | 模块切分、测试、命名都到位 |
| 安全网 | 🟢 良好 | 原子写 ✓ / WAL ✓ / 文件锁 ✓ / HIGH-2 已修 / HIGH-1 已降级 |
| 测试覆盖 | 🟢 530 pass | 缺 watchdog 端到端冒烟 |
| 文档同步 | 🟢 | DESIGN.md:106 已清 |
| 产品定位 | 🟢 清晰 | 反推完整版形状成立 |
| 外部依赖风险 | 🟡 | 5 个入口文件 cc-bound，核心 13 模块 provider-agnostic，adapter ~500 LOC |

**结论**：Phase 3 Plan M **可以收尾**，剩余两个真实问题（HIGH-1 / MED-3）+ 两条 unclear（inserter / recall）可以一个 worktree agent 顺手清完，然后开 Phase 4。

---

## 9 · Agent 报告归档

- 原 review 5 agent：fact-checker `ab82cdd57e1ddd407` · blind-reviewer `a1511131b570267b0` · design-traceability `a566645b21ecd8dca` · code-quality `a3d54b0f47678fabc` · product-blind `a84d5ac26e9c17513`
- 复核 agent：Explore（2026-05-26 20:35）

---

## 10 · Lumi 更正块（2026-05-26 03:27，保留原文）

> product-blind agent (`a84d5ac26e9c17513`) §5 / §8 关于"深度耦合 cc，换客户端基本要重写"的结论与代码事实不符。原因：派 agent 时 prompt 未限制其下架构边界判断，agent 凭命名密度（sessionend_* / lifecycle / launchd / `~/.claude/`）盖章，未纳入 `llm.py` dispatch 结构和 `config.toml` 显式 provider chain。

### 反证
- `~/.config/marrow/config.toml:2` 注释原话：(Swap the conversation/pipeline provider by editing one chain entry below.)
- `marrow/llm.py:124-127` 显式 dispatch：`if kind == "claude_cli"` / `raise LLMError(f"unknown provider kind: {kind}")` — 结构留给多 provider。
- 配置文件已有 `[llm.claude_cli]` + `[llm.ollama]` 两段，`default` / `emergency` 可切换。

### 真实耦合面（36 py 模块）
- **cc-bound 入口层（5 文件）**：`hooks.py` / `sessionend_async.py` / `sessionend_prompts.py` / `sessionend_writers.py` / `sessionstart_catchup.py` / `transcript.py` / `cli.py`（mw 自己 CLI 无关）
- **内容层（1 文件）**：`subpages_render.py` 渲染 `~/.claude/skills/hooks/scripts` 速查表（cheatsheet 子页面，本来就是给 cc 用户的内容）
- **provider 适配（1 文件）**：`llm.py` 加新 `kind` 分支
- **核心 provider-agnostic（13 模块）**：`md_index` / `dashboard` / `inserter` / `subpage_specs` / `storage` / `reconcile` / `tombstone` / `aging` / `candidates` / `daily` / `handover_render` / `top_sections` / `recall` / `migrate` — **0 行改动**

### Codex 接入成本
Codex CLI/app 原生 hook 覆盖：`PreToolUse` / `PermissionRequest` / `PostToolUse` / `PreCompact` / `PostCompact` / `UserPromptSubmit` / `SubagentStop` / `Stop` / `SessionStart` / `SubagentStart` — 比 cc 更全。Codex plugin 体系也成熟。

预估 adapter 工作量：
- `transcript.py` 重写 Codex 日志 parser：~200 LOC
- `hooks.py` + sessionend/sessionstart hook 入口适配：~100-200 LOC
- `llm.py` 加 `openai_*` kind：~50 LOC
- session path / id hard-code 抽出来：~50 LOC
- 总计 **~500 LOC**，集中在 5 个入口文件。**不是重写。**

### 当前缺口（开源前要做）
marrow 现在 cc 路径是 hard-code，没有显式 `marrow/adapters/cc.py` 抽象层。开源前要做的事：
- 抽 `marrow/adapters/{cc,codex,...}.py`，每个 adapter 暴露 transcript parser / session path resolver / hook entry / handover injector
- 核心调 adapter API，下面是谁不知道
- 1-2 个 worktree agent 工作量，未列入 DESIGN/FUTURE — 开源动议时再开 Phase。

### 流程教训
- product-blind agent 不应被允许下"外部依赖风险 / 架构耦合度"这种依赖架构边界理解的判断
- review prompt 加约束：只反推产品形态，不评估架构边界 / migration 成本 / provider 耦合度
- 或者另派一个 architecture-blind agent，prompt 明确"评估 provider 抽象层 / adapter 边界"

---

*原 report 由主 session 整合 5 agent 输出写出；2026-05-26 20:35 由复核 Explore agent 逐条核对当前 HEAD=49a6eb0，删除 7 条已修/虚假项，保留 §10 更正块。*
