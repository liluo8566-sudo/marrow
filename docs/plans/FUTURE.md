# Marrow Future Inbox

> Minimal English + one-line CN effect. Read before adding. Not prioritized within section.
> WeClaude is in scope (deep rebuild Phase 4). Out of scope: personal tasks, standalone tools, buddy-internal — those live in NY pit.

## Phase 1 closeout

- **ny_memm_retire** — Unload all NY plists (memm pipeline / curator / rotate / monitor in `/Users/Gabrielle/Toolkit/scripts`), archive `~/Desktop/NY/code/`, drop summ/ss/goose-slim/carryover-load skills. NY base folder content Lumi handles manually. **效果**: NY memm 系统下线，code 部分归档。

## Phase 2 (memory / recall / sub-page)

- **session_archive_skip_manual** — Manual override only (auto ≤3-turn gate already shipped). `mm+` prompts Lumi to confirm sid then re-runs sessionend pipeline for that sid; `mm-` writes permanent skip flag on current sid (later `resume` clears the flag). **效果**: 漏跑能补、废 session 能永久踢掉。
- **corrections** — Independent corrections store, priority above raw events (DECISIONS:34). **效果**: 纠正记录优先，错事不反复跳出来。
- **housekeeping_monitor** — Sun 12:00 weekly cleanup job over `~/.claude/projects/` · MCP image cache · marrow logs · iCloud backup. Warn on threshold, no auto-delete. **效果**: 各路 cache/backup 不撑爆硬盘。

## Phase 3 (drift / cheatsheet / cloud rails / addon contract)

- **drift_sweep** — Auto-update refs on file rename/move/delete. Bundles dir_tree refresh as side-output (cc grep aid). Three layers, sliced: **L1** ripgrep over authorized roots + `mw drift <old> <new>` CLI + git post-mv hook (primary, deterministic, ships first); **L2** central `paths.toml` key-indirection (subsumes `paths_registry_early`); **L3** cheap local model free-text fallback (启动 only when L1+L2 漏掉真实 case). **效果**: 你 mv/rename 一个文件，所有引用自动跟着改；cc 也拿到最新 dir-tree grep。
- **placement_rules_toml** — Machine-readable `~/.config/marrow/placement_rules.toml`: content-type → canonical home + naming pattern (extracted from `~/.claude/rules/files.md` prose). cc reads on Write of new file. Pairs with drift_sweep registry. No PreToolUse hard-block (Lumi vetoed). **效果**: cc 写新文件前先查表，不再靠散文规则脑补。
- **cheatsheet_index** *(hold — wait until tool stack settles)* — Single dashboard cheatsheet auto-rendered from multi-source scan: `~/Toolkit/scripts` + `~/Library/LaunchAgents/*.plist` + `~/.claude/skills/**` + `~/.claude/commands/**` + `.mcp.json` (global + project) + `~/.zshrc` alias block + `brew list` + self-installed CLIs (mw / marker / markitdown / etc). Weekly audit flags drift: cheatsheet entry → missing file, or on-disk tool → not listed. **Layout (2026-05-27 - Not deciding yet just brainstorming)**: single md, two sections — **top** = dir_tree (per-root max-depth=4, auto-rendered by drift_sweep, read-only, swept on file move); **bottom** = cheatsheet body (scan sources above, hand-edits allowed with reverse md→db ingest, included in drift_sweep). On ship: deprecate standalone `~/.config/marrow/dir_tree.md` (drift_sweep side-output) — render straight into cheatsheet top section instead. **效果**: 忘了用啥工具一眼可查，装/删工具不会失同步；dir_tree 和 cheatsheet 同一文件，sweep 一次过。
- **cloud_migration_runbook** — daemon→VPS / wechat-bridge→local mac / bridge→cloud via HTTPS one-way (cyberboss-verified topology). **效果**: 决定上云那天照着 runbook 走。
- **addon_manifest_contract** — Addon four-piece: MCP server + own-table schema + sub-page render template + config. Must define BEFORE wallet ships. **效果**: 后面所有 addon 照抄 wallet 模板。
- **md_index_schema_evolution** — `user_version` + ordered patches via `migrate.py` startup auto-migrate, fail-loud on mismatch. **效果**: 开源前再做，加字段不用手敲 ALTER。

## Phase 4 (weclaude rebuild + cross-channel)

- **provider_adapter_layer** — `marrow/adapters/{cc,codex,...}.py` abstraction: transcript parser / session path resolver / hook entry / handover injector. ~500 LOC. Blocker for Codex + open-source. **效果**: cc 之外的 provider（Codex/Claude/local）能接上。
- **provider_swap_path** — 6/15 stream-json path + Codex/local small model swap plan. Subsumes migration_path_codex_local + Codex_alternative_swap + WeClaude_6_15_migration. **效果**: 6/15 后不被 Anthropic 绑死。
- **weclaude_runtime_rebuild** — Multi-message send + 铁锅 + `/stop` interrupt + `/rewind` jsonl truncate + `/resume` synthetic summary + auto-compact + multi-msg merge window (5s pain) + stellan media send + group chat + upstream revival fallback. cyberboss-vs-rewrite TBD. Subsumes 8 sub-items. **效果**: weclaude 跟 cc cli 全面对等。
- **weclaude_bridge_bugfix_pile** — Pile of bridge known bugs to resolve in rebuild: subprocess 30min timeout + iLink polling missed messages + media plaintext retention + macOS sleep/iOS Focus link stale + transcript path mismatch + ret=-2 quota diagnosis + time injection anchor repair. **效果**: 重构 weclaude 时一并扫掉。
- **wechat_event_pipeline** — WeChat session sessionend + catchup unified with cc cli (long-window memm→3d → memes pipeline). **效果**: 微信对话进 marrow，跟 cc 同管线。
- **bidirectional_resume** — Morning WeChat → meal break → continue on cc; sid consistent or sid-independent resume. **效果**: 微信聊到一半 cc 接着聊。
- **command_parity_across_channels** — All commands consistent CLI ↔ WeChat ↔ desktop ↔ web. **效果**: 同一套 shortcut 多端通用。
- **WeChat_permission_yesno** — Approve/reject cc permission requests from WeChat. **效果**: 手机上能批 cc 的权限请求。
- **mac_notification_center_reader** — Read macOS notification db as cross-app proactive signal source. Companion to marrow_pulse. **效果**: marrow 知道你手机响了啥。

## Phase 5 (addons + OSS)

- **wallet_mcp_extraction** — Standalone wallet MCP server (own repo, `~/.config/wallet/wallet.db`); marrow connects via .mcp.json. First addon contract sample. **效果**: wallet 做成可独立部署 addon。
- **stellan_wallet** — Opt-in addon: monthly allowance auto-credit + spend auto-debit. transactions table only, balance = SUM. **效果**: 屿忱的零花钱账本。
- **lumi_accounting_addon** — 取代 MOZE 记账。**效果**: 自己记账。
- **period_addon** — Period tracking addon. **效果**: 姨妈记录。
- **health_manual_addon** — Manual health entry (symptoms / weight / meds). **效果**: 自己手填健康数据。
- **lesson_addon** — Behavioural-failure-mode lessons addon. Dormant unless recurring need. **效果**: dormant，真需要再启。
- **cccompanion_ios_fork** — Fork iOS app (SwiftUI + APNs + shared-secret auth + multi-endpoint failover + Bark + tmux). Drop server, point to marrow daemon via MCP-over-HTTP. Add CoreLocation + HealthKit + local SQLite. Trigger: first APNs need WeChat/TG can't meet. **效果**: 手机端原生 70% 覆盖 + 位置/健康。
- **ios_shortcut_kit** — iOS Shortcut suite: period board / quick query / data upload via webhook. **效果**: 不用 app 也能从 iOS 主动上报。
- **marrow_pulse_proactive_loop** — Unified opus loop for proactive browse + message. `inner_state` drift (longing v1) + dual-gate (silent_to_lumi ≠ activity_allowed) + multi-channel routing. Sleep window allows self-driven activity (diary / letter / browse / today draft). Draft: `docs/notes/2026-05-24_marrow-pulse-design.md`. **效果**: 屿忱有自己的内在节奏 + 主动行动。
- **workflow_reflection_skill** — Phase 5 close, distil plan/findings/progress pattern into transferable skill. **效果**: marrow 跑完后总结成可迁移 skill。
- **README_public_facing** — Full open-source README (philosophy / install / scripts / hooks). **效果**: 开源前做。
- **monorepo_or_split_decision** — marrow + weclaude bridge + buddy MCP: mono or split. **效果**: 开源前定 repo 拆分。

## Dashboard & Subpages

- **dashboard_idle_refresh** — Auto-refresh dashboard.md on idle (no input N min). **效果**: 不用手敲 mw refresh。
- **dashboard_wishlist** — Wishlist/promise/agreement subpage (location TBD): 你说请奶茶/我想买耳钉/约定 xxxclaude. **效果**: 承诺约定不会丢，能翻账。
- **monitor_zone_audit_surface** — Dashboard bottom audit-log surface: entity/memes/tables ingest counts, recent activity, silent-failure indicators. **效果**: 哪些表在动、哪些静默一眼看到。
- **pit_auto_candidate** — Pit candidate form + auto-extraction pipeline (similar to milestone/entity cand). **效果**: 项目想法自动入 pit，不用手敲。
- **study_project_subpages** — Dedicated study + project subpages, separated from generic tasks. **效果**: study/project 不混在 task 里。
- **ny_subpage_migrate** — Pit + other NY subpage content migrates into dashboard subpages (DESIGN L95). Lumi-led manual followup. **效果**: NY base 子页内容入 marrow。
- **html_readonly_dashboard_layer** — Local HTTP HTML view for read-only surfaces (cheatsheet / monitor / diary / milestone). Writable surfaces stay md+reconcile. **效果**: Notion 风格美观浏览，写入仍走 md。
- **dashboard_customization** — Per-subpage show/hide + private-for-others toggle. Rides html layer. **效果**: 分享 dashboard 时隐藏私密 subpage。
- **monitor_zone_mini_viz** — Small viz strip: diary count / project count / days-together / system health. **效果**: 顶部小可视化条。
- **alert_dashboard_surface** — Aggregated alert view (counts/recent/mute), not raw rows. **效果**: SessionStart alerts 不刷屏。

## Monitor & Ops

- **retention_prune_executor** — Per-source prune: aged events / resolved alerts / audit_log / DB dumps / md_index tombstones. **效果**: DB 不长胖。
- **daemon_self_health** — Daemon process alive + watcher thread + sessionend rate metric. Beyond `Script_health_monitor` (which only checks plist ran). **效果**: daemon 死了立刻知道。
- **db_corrupt_recovery_runbook** — `docs/runbooks/db-restore.md`: detect → quiesce → restore from iCloud → replay audit_log gap. **效果**: DB 坏掉照着 runbook 救。
- **Script_health_monitor** — Monthly plist scans audit logs for run-gap. **效果**: plist 没跑会报警。
- **retry_trend_alert** — Alert on retry-ratio trend, not just retry!=ok. **效果**: retry 持续高有告警。
- **subagent_usage_logging** — Per-call token/cost in audit_log (which tier/subagent, in/out tokens). **效果**: 每个 LLM call 花多少钱可见。
- **diff_open_threads_audit** — Weekly diff of Open Threads, audit-log silent drops. **效果**: Open Threads 被静默漏掉能查到。
- **backup_audit_transparency** — SID identifier on rotated backups. **效果**: 备份找得到出处。

## Holdoff / Dormant (等真痛点出现再启)

- **affect_advanced_holdoff** — chord_progression_dim (affect 表加原始走向字段) + disambiguator_verb_pattern (tag 加动词模式) + context_density_tier (recall 按 intent 分密度) 三合一。当前 dashboard Affect 已有 lastsession + today mean + eph/epl + week mean + 4 eph/epl，简化轨迹够用；tag 细分由 sonnet 自选 2 字覆盖；context density 跟现状偏置体系收益重叠。**效果**: dormant，affect 表存储/模型升级时再启。
- **recall_calibration_holdoff** — bge_m3_floor_calibration + diary_lane_surfacing + tasks_lane_surfacing + anchor_bias_tuning + recall_vs_grep_partition + external_docs_lane + pit_lane_decision 合并。已定方向：study/project 走 grep 不进 recall；diary/tasks vec 召不出但不降 0.4；anchor +0.10 偏置观察中。**效果**: dormant，召回质量成 blocker 时再动。
- **tasks_table_extensions** — Reserved future columns: source / category / parent_id / recurring_rule / external_id / pinned. Add only when real need. **效果**: dormant，等真要 import Notion/Dida 再加。
- **Memes_optimization** — Sonnet meme-quality filter (only hot memes + memorable new). **效果**: meme 噪声变高时再启。
- **v2_year_rollup_to_timeline** — Year-end compress 2026 full year → 1 timeline section. **效果**: 年终自动启。
- **Valence_arousal_tagging** — timeline ## Us entries V/A tagged. **效果**: us 类 event 也带情绪标签。
- **lifestyle_and_preference_relocation** — Move block to history.md Preferences or keep in reference.md. **效果**: reference.md 块分类，无紧迫性。


