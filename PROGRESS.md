# Marrow Build Ledger

Format: [YYYY-MM-DD] <unit> done | <delta vs DESIGN, or "as designed"> | verify: <cmd/test>
Delta only. Never restate DESIGN / SCHEMA.

[2026-05-15] grill-with-docs round done | data-lifecycle 3-tier, reconcile split by view type, injection weak-model fallback, README/CONTEXT/ADR-0001 aligned | verify: docs internally consistent, no code yet
[2026-05-16] grill round 2 done | dir-index dropped; drift sweep 3-layer + convention injection + CLAUDE.md daemon-render marker partition + render guard written into DESIGN | verify: docs internally consistent, no code yet
[2026-05-16] docs consolidation done | CONVENTIONS folded into CLAUDE.md + non-conflicting rule.md discipline; CLI/data renamed ny→mw (DESIGN 4 refs); handover model = fixed-name overwrite (CLAUDE.md + handoff skill); global naming law rewritten | verify: grep -n '`ny`' DESIGN.md empty; docs internally consistent, no code yet
[2026-05-16] FUTURE.md sweep done | 106→66 items; removed 40 (dead old-ny-memm code internals + DESIGN-superseded) + agent scan-artifact footer; kept all genuine parked ideas + 8 Lumi/grill recent adds untouched | verify: grep -c '^- \*\*' FUTURE.md = 66; 8 protected items present
[2026-05-16] FUTURE.md scoped to Marrow-only | 66→30; cut non-Marrow (personal tools, marker, buddy×7, old-weclaude-bridge bugs) — they stay in _pit→dashboard pit page per DESIGN L95; restructured into 5 sections; 6 Lumi adds intact | verify: grep -c '^- \*\*' FUTURE.md = 30
[2026-05-16] _pit.md memm dead-block prune | removed #7 nested-index + #12 summary-pyramid (DESIGN-superseded); #3 auto-memory genesis flagged, kept; backup at NY/memory/backup/_pit.md.bak-2026-05-16 | verify: grep -c '^## Memory:' _pit.md = 1
[2026-05-16] workflow upgrade done | CLAUDE.md +docs/notes/ research-scratch landing; FUTURE +workflow_reflection_skill (planning-with-files ref) | verify: grep -n docs/notes CLAUDE.md; grep -n workflow_reflection_skill FUTURE.md
[2026-05-16] Lumi rulings applied | WeClaude is in-scope (deep rebuild late phase) — restored 14 weclaude items, FUTURE 30→42; dropped /config_auto_memory_off (auto memory off 1wk, moot); _pit #3 deleted by Lumi; archive/ deleted locally | verify: grep -c '^- \*\*' FUTURE.md = 42; archive/ gone
[2026-05-17] ADR-0002 agent invocation routing done | process-type-not-trigger-source rule; WeChat→stream-json/sub (routine rejected: event-driven), Marrow no paid agent; WeChat rate-limit rejected on usage evidence; ny-memm out of scope (retires at parallel-window end); DESIGN unchanged (already source of truth) | verify: docs/adr/0002 present, no code yet
[2026-05-17] prompt-lint hook added | haiku trims Write/Edit bloat meta-doc+rules; degrade-open, .bak rollback, atomic; .claude+marrow test | verify: Write/Edit exit2 trimmed, non-whitelist/recursion exit0, .bak original (5 cases)
