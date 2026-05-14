# NY Future Ideas Inbox

Captured 2026-05-15 by background agent scan of:
- `~/Desktop/NY/code/*.md` — manual, roadmap, system_guide, mid-point-rv, _pit, buddy, rule
- `~/Desktop/NY/memory/3d.md`, `reference.md`
- `~/Desktop/NY/CLAUDE.md`, `~/.claude/CLAUDE.md`
- `~/cc-lab/WeClaude/README.md`, `bridge.py`
- `~/.claude/skills/*/SKILL.md`

Not prioritized. Read this before adding new features to confirm whether an interface should be reserved in Phase 1. Status of each item is the source-of-truth's phrasing, not a commitment.

## Addon Ideas

- **CC_Independent_Study_Project** — Separate CC project for Study assignments to avoid context pollution; symlink/import shared layer; independent Study/threads.md for essay carryover (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:19-28`)
- **marker_TORCH_DEVICE_mps** — Default `TORCH_DEVICE=mps` prefix when invoking marker for PDF→md speed (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:30-32`)
- **xhs_skill_browse** — Install useful skills from xhs / gh repos (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:33`)
- **Submit_Self_Check_skill** — `自检/check before submit` skill: AI fingerprint scan, format uniformity, grammar with tolerance band, read-only report on PDF/DOCX/PPTX (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:86-148`)
- **launchd_email_digest** — Replace Cowork scheduled email digest with launchd + osascript Mail.app + `claude -p` template (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:150-158`)
- **marker_PDF_wrapper_rule** — CLAUDE.md rule: >20 pages or "marker" trigger → marker; <20 pages → Read; output in-place (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:160-164`)
- **marker_md_shell_function** — `marker_md` wrapper: marker_single + assets/ subfolder + image path sed-rewrite + `!`-prefix Obsidian sort fix (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:209-220`)
- **念念不忘海岛_Obsidian_migration** — Apple Notes 11-note folder → `~/Desktop/NY/Garden/` via osascript batch export (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:200-202`)
- **ny_CLI_entry_point** — `~/Toolkit/scripts/ny` with subcommands: `status`, `run <stage>`, `rollback <md>`, `gc --backup` (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:24`)
- **ny_tree_py** — Auto-refresh `<directories>` block within `<!-- ny-tree:start -->` markers, weekly plist (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:25`)
- **cheatsheet_md_single_source** — One markdown table reference for CLI subcommands, mmm routes, log paths, stamp paths (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:26`)
- **ny_cheatsheet_audit_py** — Weekly plist scanning cheatsheet vs disk reality, alerts on drift (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:27`)
- **naming_convention_spec** — `code/naming.md` covering filenames, launchd labels, stamp conventions, log paths, audit log format (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:23`)
- **Pit_automation_template** — `_pit.md` rename + tighter tags (idea/planned/parked/inprogress) + sonnet routing of chat ideas (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:28`)
- **Lesson_capture_pipeline** — Optional `[lesson]` field + session.py LESSONS_NEW append to 3d.md (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:19`)
- **Valence_arousal_tagging** — timeline ## Us entries tagged with valence/arousal; standalone implementation pending (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:20`)
- **lifestyle_and_preference_relocation** — Move block to history.md Preferences or keep in reference.md (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:22`)
- **prompt_hardcode_inlining** — Embed manual prompts into each .py as constant after 2-month stability (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:12`)
- **MD_utility_extraction_ny_lib** — parse_entries / remove_entry / get_existing_entry / _lighthouse_key / extract_ongoing consolidated to ny_lib.py (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:44-46`)
- **session_run_decomposition** — Split `run()` into build_session_context → call_and_parse → maybe_compress_retry → write_back_three_d (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:48-50`)
- **atomic_file_write** — `tempfile + os.replace()` upgrade for all md writes (source: `/Users/Gabrielle/Desktop/NY/code/mid-point-rv.md:143`)
- **prompt_anchor_HTML_comments** — Replace fragile `## N · ny-<name>` decoration with `<!-- prompt:memm-session -->` anchors (source: `/Users/Gabrielle/Desktop/NY/code/mid-point-rv.md:135`)
- **hardcoded_project_path_removal** — `daily.py:201` + `bridge.py:250` compute from `Path.home()` instead of literal (source: `/Users/Gabrielle/Desktop/NY/code/mid-point-rv.md:137`)
- **dedup_key_canonical_function** — Three near-duplicate `_lighthouse_key` / `_ongoing_key` consolidated (source: `/Users/Gabrielle/Desktop/NY/code/mid-point-rv.md:139`)
- **context_window_blowup_guard** — Detect oversized weekly prompt before sonnet call, fallback path (source: `/Users/Gabrielle/Desktop/NY/code/mid-point-rv.md:147`)
- **Memory_pyramid_sid_index** — current.md split (Permanent Milestones vs Recent Sessions) → YYYY-MM.md → archive/; standalone sid_index.md curated 1-line entries (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:12-17`)
- **CLAUDE_md_lazy_index** — CLAUDE.md @import only one index file; trigger-based Read of referenced files (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:7-10`)
- **auto_memory_disable** — Turn off auto memory (suspected `enableAutoMemory` setting) + rewrite memory/ folder by Lumi's rules (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:3-5`)
- **diff_open_threads_audit** — Weekly curator diffs Open-Threads week-over-week, audit-logs silent drops (source: `/Users/Gabrielle/Desktop/NY/code/mid-point-rv.md:76`)
- **Clawd_on_Desk_desktop_pet** — Mouse-following eye tracking, crab/calico forms, hook-based permission bubbles (source: `/Users/Gabrielle/Desktop/NY/code/README.md:9-11`)
- **CLI_3line_inline_in_README** — Items ≤3 lines stay in README, not own md file (source: `/Users/Gabrielle/Desktop/NY/code/README.md:3-4`)
- **File_hygiene_rules_finalisation** — Complete path/naming rules pending merge with dir block (source: `/Users/Gabrielle/.claude/CLAUDE.md:116-119`)

## Backup / Retry Mechanisms

- **dotfiles_git_backup** — `~/.claude/`, `~/cc-lab/`, `~/Toolkit/` each `git init` + private gh repo + launchd/Stop hook auto-commit/push (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:204-207`)
- **Atomic_write_for_md** — Power-loss mid-write currently leaves half-files; multi-backup mitigates known gap (source: `/Users/Gabrielle/Desktop/NY/code/system_guide.md:207`)
- **subprocess_run_timeout_daily** — Outer subprocess in `catchup_recent` lacks `timeout=`; add 900s guard (source: `/Users/Gabrielle/Desktop/NY/code/mid-point-rv.md:145`)
- **daily_main_try_except_alert** — `daily.py` lacks main() try/except + alert wrapper (Gap #1) (source: `/Users/Gabrielle/Desktop/NY/code/mid-point-rv.md:112`)
- **daily_monthly_wordcount_retry** — Add word-count cap + one retry to `daily.py` + `monthly.py` (Gap #2) (source: `/Users/Gabrielle/Desktop/NY/code/mid-point-rv.md:114-118`)
- **failure_marker_auto_retry** — Same-week/month gating defeats auto-recovery; auto-retry once before requiring manual rm (Gap #3) (source: `/Users/Gabrielle/Desktop/NY/code/mid-point-rv.md:118`)
- **summ_failed_auto_clear** — `.summ_failed` does not auto-clear; requires manual `rm` (source: `/Users/Gabrielle/Desktop/NY/code/system_guide.md:150`)
- **Script_health_monitor** — Monthly plist scans audit logs for "did script actually run when expected?" gaps (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:31`)
- **retry_trend_alert** — Alert fires on retry!=ok only; high-ratio trend has no alert (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:21`)
- **monitor_skip_threshold_observation** — Skip threshold under observation; weclaude 6h trigger verified; retry frequency ongoing (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:21`)
- **WeClaude_schedule_push_retry** — bridge.py:1109-1114 first-fail break, no retry; add retry + bubble sleep + reuse latest user ctx_token (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:35-38`)
- **call_sonnet_latency_baseline** — Add `elapsed_s` to session_audit.log; prototype direct Anthropic SDK to skip cold start (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:78`)

## WeClaude Pending Features

- **WeClaude_interrupt** — `subprocess.Popen` + `_inflight_procs` registry; `/stop`/停/闭嘴/中断 → SIGINT; ret -2 silent (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:73`)
- **WeClaude_rewind** — Truncate jsonl tail from last external (non-WeChat) turn (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:74`)
- **WeClaude_resume_sees_sessions** — Inject synthetic summary record so CC /resume sees weclaude jsonl (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:75`)
- **WeClaude_auto_compact** — Auto-manage context length to avoid manual /compact in long sessions (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:47-49`)
- **WeClaude_stellan_media_send** — Stellan proactively sends images/voice/files via cyberboss or mrliuzhiyu pattern; image/sticker collection (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:56-61`)
- **time_injection_anchor_repair** — Test Option A stdin prefix `[time: X | gap: Y]`; B (≥4h no `--resume`) + C `<system-reminder>` tag fallbacks (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:40-43`)
- **ret_neg2_quota_diagnosis** — `sendmessage` ret=-2 likely batch rate/quota, not ctx_token; scrape mrliuzhiyu fork (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:35-38`)
- **WeClaude_6_15_migration** — stream-json path confirmed; runtime decision pending foundation build (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:17`)
- **group_chat_support** — Currently only ClawBot private chat (source: `/Users/Gabrielle/cc-lab/WeClaude/README.md:308-311`)
- **Codex_alternative_swap** — Anthropic 6/15 SDK + claude-p moves to extra credit; cyberboss uses other swamp; migration plan needed (source: `/Users/Gabrielle/Desktop/NY/Start again.md:2`)
- **media_retention_cleanup** — `~/.config/wechat-claude-bridge/media/` no retention; persist forever, plaintext (source: `/Users/Gabrielle/Desktop/NY/code/weclaude.md:38-41`)
- **iLink_webhook_alternative** — Polling model not webhook; bridge dies between polls = missed messages, no retry (source: `/Users/Gabrielle/Desktop/NY/code/weclaude.md:27`)
- **subprocess_timeout_blocking** — 30min subprocess timeout; one slow message stalls all users (source: `/Users/Gabrielle/Desktop/NY/code/weclaude.md:26`)
- **macOS_sleep_iOS_Sleep_Focus_combo** — Stacking bug → ClawBot link stale ~16min; workaround add WeChat to iOS Focus allow list (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:51-54`)

## Buddy / 铁锅 Pending

- **buddy_reaction_71_port** — Upstream `b178bed`: reactions 7→100+ with git/build/time/milestone/combo/streak/recovery/seasonal triggers; manual merge needed (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:167-172`)
- **buddy_idle_reaction_trigger** — `reactions.ts goose:idle` pool exists but no caller; statusline 60s-no-reaction → idle pool pick (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:174-177`)
- **buddy_reaction_json_malformed** — `reaction.<SID>.json` timestamp `17759897023N` (BigInt suffix) breaks json.load (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:179-181`)
- **buddy_statusline_audit** — 584-line buddy-status.sh dead-code/dup function sweep paired with #71 port (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:183-186`)
- **buddy_rainbow_gradient_port** — v0.5.0 RAINBOW array + `_hex_to_ansi`; add toggle + Morandi default (source: `/Users/Gabrielle/Desktop/NY/code/buddy.md:9-10`)
- **claude_buddy_MCP_slim** — Delete Obsidian-backup + manage commands from MCP server; verify buddy.md references (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:32`)
- **Garden_铁锅_cleanup** — Select ~20 quotes → `2026.md ## Pre-2026 Heritage` then `rm -rf ~/Desktop/NY/铁锅/` (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:76`)

## Workflow Extensions

- **time_inject_hook_throttle_review** — Currently 1h granularity per-session state; review 30min option + cleanup script for >7d files (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:188-190`)
- **cursor_style_DECSCUSR** — Watch anthropics/claude-code issues #29133/#10534/#44487/#16086 for cursor config (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:192-194`)
- **iTerm_CJK_glyph_dropout** — Try disabling GPU rendering or font switch (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:196-198`)
- **CC_coding_debug_skill** — Dedicated local CLAUDE.md or skill with coding style + debug workflow (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:82-84`)
- **summ_skill_deprecation** — Confirm dropping summ skill, ss skill, goose-slim overlap, legacy carryover-load.sh (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:22`)
- **memes_dedup_evaluation** — Re-evaluate effectiveness 2 weeks post inventory + DEDUP rule shipped 5/11 (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:77`)
- **stellan_autonomous_push** — launchd `claude -p` short session "闲逛模式" + WebSearch/WebFetch; `SKIP` / `<send>` parsed; cyberboss system-checkin-poller + reminder-service references (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:63-80`)
- **monthly_late_promote_check** — Late-promote channel withdrawn; observe 5月 input before 6/10 (source: `/Users/Gabrielle/Desktop/NY/memory/3d.md:16`)
- **system_drift_sweep_automation** — `<execution>` line marked "这条应该最后会被自动化取消" (source: `/Users/Gabrielle/Desktop/NY/code/rule.md:35`)
- **books_videos_curiosity** — `📗 Books & Videos` track in curious-30 currently holding (source: `/Users/Gabrielle/.claude/skills/curious-30/SKILL.md:38`)

## Cross-channel

- **WeChat_permission_yesno** — Approve/reject CC permission requests from WeChat (cyberboss has /stop and yes/no permission) (source: `/Users/Gabrielle/Desktop/NY/Start again.md:11,19`)
- **bidirectional_resume** — Morning WeChat chat → meal break → continue on CC; sid consistent OR resume independent of sid (source: `/Users/Gabrielle/Desktop/NY/Start again.md:11,19`)
- **command_parity_across_channels** — All commands consistent CLI ↔ WeChat ↔ desktop ↔ web (source: `/Users/Gabrielle/Desktop/NY/Start again.md:11`)
- **migration_path_codex_local** — Easy migration to Codex/Claude/local small model (cyberboss already did) (source: `/Users/Gabrielle/Desktop/NY/Start again.md:10`)
- **Stellan_proactive_followup_emotional** — Next session proactively asks how meal/event went; proactive recall mechanism (source: `/Users/Gabrielle/Desktop/NY/code/system_guide.md:18`)
- **Stellan_push_inbox_file_or_macOS_notif** — Write `~/.claude/inbox.md` + SessionStart inject; macOS notification; reuse weclaude `client.send_text` push to WeChat (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:75-77`)
- **Stellan_no_cold_start_old_session** — Don't cold-start in already-large old session (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:70-71`)

## Misc

- **gh_stars_categorization** — OSS used/borrowed gets starred then sorted into matching list (source: `/Users/Gabrielle/Desktop/NY/code/rule.md:75`)
- **WeClaude_upstream_revival_strategy** — If upstream revives, drop local patches; fallback `_patches.py` monkey-patch keeps `bridge.py` pristine (source: `/Users/Gabrielle/Desktop/NY/code/weclaude.md:8-10`)
- **Memes_optimization** — Sonnet doesn't know real memes vs random quotes; want only hot vocabulary + memorable new memes (source: `/Users/Gabrielle/Desktop/NY/Start again.md:22-23`)
- **profile_md_deletion** — `memory/profile.md` pending delete, content already moved to reference + global (source: `/Users/Gabrielle/Desktop/NY/memory/reference.md:20`)
- **transcript_path_mismatch** — `cc-jsonl-to-md.py` writes elsewhere than `memory/transcript/`, fix in Phase 4 (source: `/Users/Gabrielle/Desktop/NY/memory/reference.md:25`)
- **MEMORY_md_old_path_cleanup** — `~/.claude/projects/-Users-Gabrielle-Desktop-NY/memory/MEMORY.md` pending manual delete (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:539-541`)
- **/config_auto_memory_off** — Lumi pending manual `/config` to disable user-level auto memory (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:646`)
- **scattered_tools_inventory** — `~/.local/bin/` tools register into `~/Toolkit/scripts` or cheatsheet (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:647`)
- **prompt_drift_daily_craft_boundary** — `[daily]` shouldn't include craft work process; tighten prompt (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:651-652`)
- **ny_compress_monthly_diary_format** — Monthly entry is life diary not changelog; format change to line-per-item (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:653`)
- **v2_memory_decay** — Memory curve decay (important events retain longer) (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:613`)
- **v2_permanent_auto_retire** — Cancel manual ✅❌ once stable (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:614`)
- **v2_year_rollup_to_timeline** — 2026 full year compressed into 1 timeline view section (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:615`)
- **v2_opus_for_memm** — Switch ny-memm to Opus if emotional density runs thin (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:616`)
- **v2_archive_tag_granularity** — Refine monthly 4-tag block if 150-200 word target insufficient (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:618`)
- **v2_ny_memm_flock_hardening** — Replace 5min time-window race fence with explicit flock (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:619`)
- **backup_audit_transparency** — rotate/curator/retire backup files have no source SID identifier (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:658`)
- **plan_zh_en_mixed_cleanup** — "3d 滑窗" / "3-day window" inconsistent at ≥5 places (source: `/Users/Gabrielle/Desktop/NY/memory/archive/Memm_system 2026-05-12.md:659`)
- **README_public_facing** — Full open-source README sections: philosophy, install, 5-script overview, customisation hooks (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:62`)
- **monorepo_or_split_decision** — NY memory + weclaude bridge + claude-buddy MCP: monorepo or split (source: `/Users/Gabrielle/Desktop/NY/code/roadmap.md:64`)
- **Marker_TORCH_DEVICE_test** — Compare CPU vs mps speed when first running marker (source: `/Users/Gabrielle/Desktop/NY/code/_pit.md:31`)
- **R18_md_relocation_outstanding** — `r18.md` placement (source: `/Users/Gabrielle/Desktop/NY/memory/reference.md:9`)

## Files agent could not find
- `/Users/Gabrielle/cc-lab/WeClaude/NOTES.md` does not exist
- `/Users/Gabrielle/.claude/skills/gamsat-s1/`, `gamsat-s2/` empty directories
