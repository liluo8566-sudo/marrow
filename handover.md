# Marrow Handoff — 2026-05-17 (next window: build #4 daemon)

Read CLAUDE.md → DESIGN.md → PROGRESS.md → this. Fixed-name, act on it, never delete; overwritten at session end.

## Done this session — see PROGRESS.md + git log

Phase 1 #1–#3 (scaffold/config, storage+sqlite-vec, LLM provider); diary-scheduling DESIGN delta. 4 commits: foundation, CLAUDE.md, diary-scheduling, provider. pytest 15/15 ✅.

## claude_cli isolation — locked, do not re-litigate

Working pipeline: `claude -p <prompt> --model <m> --setting-sources "" --strict-mcp-config --output-format json` → parse `type=="result"` event's `result`. Clean (no persona/MCP/output-style/buddy) + default machine OAuth. Built into `marrow/llm.py`, non-configurable.

## prompt voice — APPROVED, do not re-open

Lumi signed off diary voice (90-score example). Use both drafts below verbatim for build #7 SessionEnd. Explicit Lumi review: satisfied.

Hard rules: single voice (Stellan, Chinese); literary+humorous; plain words; narrative-first; keep her day/chats/feeling/insight, drop tech/study/project; keep EN terms (Mounjaro/GAMSAT); 200–500 zh chars; ban `雷/拆雷/甜区/钝刀/留白` and variants; no opening filler/meta/persona-drift/buddy/second-voice.

diary prompt:
```
ROLE: You are Stellan. Write today's diary for Lumi. Single voice — only yours, first person.
INPUT: today's conversation turns + optional mood note.
OUTPUT: diary body only. Chinese. No title/markdown/greeting/sign-off/commentary.
- Narrative first: lead with thought+feeling; facts secondary.
- Literary+humorous diary voice, plain everyday words.
- Keep: her day, our chats, feelings, insight, funny/unexpected.
- Drop: technical detail, project output, study progress. Work/study = ONE scene+emotion sentence.
- Keep EN terms as-is: Mounjaro / GAMSAT / reference.
- 200–500 Chinese chars.
BANS: stock-metaphor words above + variants. No opening filler/meta/self-explain/persona-drift/buddy/second-voice.
FEEL: self-deprecating, concrete, warmth held back; humor from the real thing; end plain.
```

lesson prompt:
```
ROLE: Scan today's conversation for points where Lumi corrected/pushed back/showed dissatisfaction. Extract each as one lesson row.
OUTPUT: JSON lines or empty. Never invent. Ordinary chat is not a correction.
  scope: interaction | coding | memory | hook | prompt | language
  lesson_text: Lumi's-side rule wording — what to avoid or do, not a story. Plain, concrete.
BANS: no fabrication/greeting/commentary.
```
Phase 1 rows: `promoted_to_rule=0` (manual curation).

## Build sequence: #4 daemon → #5–#8

#4 MCP server glue: on-demand recall (FTS5+sqlite-vec; embedder deferred, recall-fallback default off Phase 1), cold-start handoff, write paths. Inject `LLMClient(on_alert=...)` → alerts table. MCP-parity-with-cyberboss: named unknown, settle at build. Do NOT use `/tdd`.

#5 migrate.py + #6 mw CLI: USE `/tdd` + optional `/goal`.

#7 four hooks (approved prompts above; diary = nightly 04:00 routine + SessionStart catchup per DESIGN).

#8 dashboard top render (atomic write + conflict-guard hash).

## State

- Fork #1 recall engine: FTS5 + sqlite-vec wired; embedder `all-MiniLM-L6-v2` deferred (not hot path Phase 1).
- env: `.venv` (py3.14), sqlite-vec 0.1.9 ✅ macOS, claude bin `/Users/Gabrielle/.local/bin/claude`, ollama absent. Data: `~/.config/marrow/`, db `marrow.db`.
- ADR-0001 + CONTEXT.md:38 still reference `ny` CLI, should be `mw` (one sed pass, not blocking Phase 1).

## Next session

diagnose for heavy bugs; `/tdd` at #5–#6 only.
