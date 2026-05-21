# Marrow — project memory

> Personal AI memory + workflow system replacing ny-memm. SQLite-backed, model-agnostic, one dashboard. Build inside this repo. Persona / relationship come from global ~/.claude/CLAUDE.md — not from old ny-memm docs.

<personal>
- If Haiku trim you, just follow; no need to verbatim my wording — keep core ideas all sessions can understand. Let me know if hook cut too much.
- For Chinese input use ( ) to bypass CJK guard.
- For tech/mech concepts use simple examples (e.g. valence / arousal: WAM 92 → valence ≈ 0.9 / arousal ≈ 0.85).
- When a tech decision needs Lumi's call: lead with effect/impact; name each option by effect, not unfamiliar tech terms; explanations go last for optional learning. (反例: 上来就问 fastembed+bge-m3 vs X — 名词不认识无法选)
- No source of truth or fixed approach in this project. All docs can change if a better option comes up. Don't cite a doc to rebut me — use first principles.
    - Priority: My input > goals and outcomes > design / future / any docs
    - Always ask: why we do this? Best way to achieve goals? If not, tell me and change it.
    - No need to follow any reference repo. Borrow ideas; write our own to best match Marrow.
- Do not infer from the old ny-memm system.
</personal>

## When to read what
- DECISIONS.md — read first. Single current truth, every line confidence-tagged (verified/reasoned/assumed).
- DESIGN.md — goal + structure + hard constraints + sub-pages. No still-changing decisions.
- PROGRESS.md — historical action log, append-only. Read this + git log before claiming done. Format: `[YYYY-MM-DD] <unit> done | <delta vs DESIGN, or "as designed"> | verify: <cmd/test>`
- FUTURE.md — unbuilt plans, by phase.
- handover.md — previous-window handoff; act on it. Fixed-name, overwritten each session end — never delete.
- docs/notes/ — hard-problem memo / research scratch, NOT a truth source.
- CONTEXT.md — glossary maintained by grill-with-doc skill; consult on term conflict.

## References
> [P0luz / Ombre-Brain](https://github.com/P0luz/Ombre-Brain)
- [WenXiaoWendy / cyberboss](https://github.com/WenXiaoWendy/cyberboss)
- current weclaude see ny/code/weclaude or repo (in my star folder)
- [Qizhan7 / claude-imprint](https://github.com/Qizhan7/claude-imprint) — borrow: RRF + vector/FTS5/recency retrieval fusion recipe
