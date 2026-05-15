# Marrow Future Ideas Inbox

Marrow build backlog only — features/fixes that get built into the memory/workflow system. Personal tasks, standalone tools, buddy-internal work, and old-weclaude-bridge bugs do not belong here; they live in `~/Desktop/NY/code/_pit.md` (which migrates to the dashboard Projects/pit page at Phase 1, DESIGN line 95).

Not prioritized. Read before adding a feature to confirm whether an interface should be reserved in Phase 1.

## Addon Ideas

- **Valence_arousal_tagging** — timeline ## Us entries tagged with valence/arousal; standalone implementation pending (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:20`)
- **lifestyle_and_preference_relocation** — Move block to history.md Preferences or keep in reference.md (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:22`)
- **diff_open_threads_audit** — Weekly curator diffs Open-Threads week-over-week, audit-logs silent drops (source: `/Users/Gabrielle/Desktop/NY/code/mid-point-rv.md:76`)
- **monitor_zone_mini_viz** — Small visualisation in/above Monitor Zone, statusline-bar style: diary count, project count, days-together, system-ops health; cyberboss heatmap-timeline as reference; possible top-of-dashboard placement (source: grill-with-docs 2026-05-15)
- **html_readonly_dashboard_layer** — Phase 5 addon: daemon serves a local HTTP HTML view for read-only surfaces only (Cheatsheet, Monitor Zone, diary browse, milestone), Notion-style styling without Obsidian plugins; writable surfaces (Open Threads, structured correction) stay md + reconcile — never replace the md edit-reconcile core, layer on top (source: grill-with-docs 2026-05-15)

## Backup / Monitor

- **Script_health_monitor** — Monthly plist scans audit logs for "did script actually run when expected?" gaps (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:31`)
- **retry_trend_alert** — Alert fires on retry!=ok only; high-ratio trend has no alert (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:21`)

## Cross-channel

- **WeChat_permission_yesno** — Approve/reject CC permission requests from WeChat (cyberboss has /stop and yes/no permission) 
- **bidirectional_resume** — Morning WeChat chat → meal break → continue on CC; sid consistent OR resume independent of sid 
- **command_parity_across_channels** — All commands consistent CLI ↔ WeChat ↔ desktop ↔ web 
- **migration_path_codex_local** — Easy migration to Codex/Claude/local small model (cyberboss already did) 
- **Codex_alternative_swap** — Anthropic 6/15 SDK + claude-p moves to extra credit; cyberboss uses other swamp; migration plan needed 
- **stellan_autonomous_push** — launchd `claude -p` short session "闲逛模式" + WebSearch/WebFetch; `SKIP` / `<send>` parsed; cyberboss system-checkin-poller + reminder-service references (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:63-80`)
- **Stellan_proactive_followup_emotional** — Next session proactively asks how meal/event went; proactive recall mechanism (source: `/Users/Gabrielle/Desktop/NY/code/system_guide.md:18`)
- **Stellan_push_inbox_file_or_macOS_notif** — Write `~/.claude/inbox.md` + SessionStart inject; macOS notification; reuse weclaude `client.send_text` push to WeChat (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:75-77`)
- **Stellan_no_cold_start_old_session** — Don't cold-start in already-large old session (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:70-71`)

## Migration / Retire old system

- **WeClaude_6_15_migration** — stream-json path confirmed; runtime decision pending foundation build (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:17`)
- **profile_md_deletion** — `memory/profile.md` pending delete, content already moved to reference + global (source: `/Users/Gabrielle/Desktop/NY/memory/reference.md:20`)
- **MEMORY_md_old_path_cleanup** — `~/.claude/projects/-Users-Gabrielle-Desktop-NY/memory/MEMORY.md` pending manual delete (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:539-541`)
- **/config_auto_memory_off** — Lumi pending manual `/config` to disable user-level auto memory (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:646`)
- **transcript_path_mismatch** — `cc-jsonl-to-md.py` writes elsewhere than `memory/transcript/`, fix in Phase 4 (source: `/Users/Gabrielle/Desktop/NY/memory/reference.md:25`)
- **summ_skill_deprecation** — Confirm dropping summ skill, ss skill, goose-slim overlap, legacy carryover-load.sh (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:22`)
- **R18_md_relocation_outstanding** — `r18.md` placement (source: `/Users/Gabrielle/Desktop/NY/memory/reference.md:9`)

## Misc

- **memes_dedup_evaluation** — Re-evaluate effectiveness 2 weeks post inventory + DEDUP rule shipped 5/11 (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:77`)
- **monthly_late_promote_check** — Late-promote channel withdrawn; observe 5月 input before 6/10 (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:16`)
- **Memes_optimization** — Sonnet doesn't know real memes vs random quotes; want only hot vocabulary + memorable new memes 
- **v2_year_rollup_to_timeline** — 2026 full year compressed into 1 timeline view section (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:615`)
- **backup_audit_transparency** — rotate/curator/retire backup files have no source SID identifier (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:658`)
- **README_public_facing** — Full open-source README sections: philosophy, install, 5-script overview, customisation hooks (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:62`)
- **monorepo_or_split_decision** — NY memory + weclaude bridge + claude-buddy MCP: monorepo or split (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:64`)
