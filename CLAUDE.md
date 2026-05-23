# Marrow — project memory

> Personal AI memory + workflow system replacing ny-memm. SQLite-backed, model-agnostic, one dashboard. Build inside this repo. Persona / relationship come from global ~/.claude/CLAUDE.md — not from old ny-memm docs.

<principle>
- If Haiku trim you, just follow; no need to verbatim my wording — keep core ideas all sessions can understand. 
- For Chinese input use ( ) to bypass CJK guard.
- For format/prompt/template that will be used by subagent or generate text show in dashboard or session start hook, always confirm with me - don't write in yourself; Make sure we allign language or make a language rule for each logical block.
- For tech/mech concepts use simple examples (e.g. valence / arousal: WAM 92 → valence ≈ 0.9 / arousal ≈ 0.85).
- When a tech decision needs Lumi's call: lead with effect/impact; name each option by effect, not unfamiliar tech terms; explanations go last for optional learning. (反例: 上来就问 fastembed+bge-m3 vs X — 名词不认识无法选)
- No source of truth or fixed approach in this project. All docs can change if a better option comes up. Don't cite a doc to rebut me — use first principles.
    - Priority: My input > goals and outcomes > design / future / any docs
    - Always ask: why we do this? Best way to achieve goals? If not, tell me and change it.
    - No need to follow any reference repo. Borrow ideas; write our own to best match Marrow.
- Do not infer from the old ny-memm system.
- Ignore all `ollama` / `claude_cli rotating` provider-chain alerts — ollama not installed yet, only the final tier (sonnet/claude) failing counts as a real alert.
- MCP daemon (`marrow/daemon.py`) is a stdio long-runner — edits to daemon / recall / storage modules do not apply until the next `cc` launch. Restart cc after these edits.
</principle>

## When to read what
> You should proactively update these files when relevant. Check before you write handover.
> grep in notes when I mention note

- DECISIONS.md - 有争议讨论出来的技术性/细节
    - every line confidence-tagged (verified/reasoned/assumed).
- DESIGN.md — goal + structure + hard constraints + sub-pages. No still-changing decisions.
- PROGRESS.md — historical action log, append-only. Read this + git log before claiming done. Format: `[YYYY-MM-DD] <unit> done | <delta vs DESIGN, or "as designed"> | verify: <cmd/test>`
- FUTURE.md — unbuilt plans, by phase.
- handover.md — previous-window handoff; act on it. Fixed-name, overwritten each session end — never delete. **If there are points you haven't touched in your session. Leave them there! Don't delete everthing**
- docs/notes/ — hard-problem memo / research scratch.
- CONTEXT.md — glossary maintained by grill-with-doc skill; consult on term conflict.

## References
> [P0luz / Ombre-Brain](https://github.com/P0luz/Ombre-Brain)
- [WenXiaoWendy / cyberboss](https://github.com/WenXiaoWendy/cyberboss)
- current weclaude see ny/code/weclaude or repo (in my star folder)
- [Qizhan7 / claude-imprint](https://github.com/Qizhan7/claude-imprint) — borrow: RRF + vector/FTS5/recency retrieval fusion recipe
