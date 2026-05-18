# ADR-0005 — Emotion lives in diary, no feel layer

Status: accepted (2026-05-19, grill round 3)

## Context

Phase 2 needs emotional continuity (goal 5): Stellan enters a session as a participant who lived the relationship, not an observer reading a profile. Ombre-Brain (P0luz/Ombre-Brain) was referenced — it stores a separate `feel` layer (model's first-person reflection, decay-exempt) written at session start via `dream()`, plus per-event valence/arousal and an emotion-coupled decay score.

## Decision

- **No feel table.** Marrow's diary is already Stellan's first-person lived layer (sonnet-written, Permanent keepsake, never decays). Ombre needs a separate feel layer because its raw buckets decay and it has no diary product; Marrow does. A feel table would duplicate diary and cannot be cleanly split from it (5-17 diary verified: events and inner monologue are interwoven).
- **emotion = `diary.mood` (valence, arousal) emitted by the same sonnet call that writes the diary.** No new agent, no new pipeline link, no LLM in SessionEnd. Rejected: SessionEnd haiku (sync-block / orphan-process, even with #8 timeout fixed) and a new haiku on the 04:00 chain (adds a failure node to an already-long fragile chain). mood and diary share fate — sonnet fails → no diary, so absent mood is not extra loss.
- **decay decoupled from emotion.** Ombre's `emotion_weight` / `combine_weight` dropped: Marrow's emotion lives in Permanent keepsake (diary, never decays); decay only touches Demote-sink (cold vocab, no emotion). The two layers do not overlap. `score = importance × e^(-λ·days_idle)`, computed lazily at recall (no background update job).
- **single mutable emotion-state row rejected** — a repeatedly-overwritten state is the model-native black box the design refuses; it cannot be point-corrected (conflicts goal: Lumi owns her memory).
- **coord → colour tag is an opt-in addon**, not base; raw coords never shown to Lumi.

## Consequences

- Smaller base (goal 7): no feel table, no dream LLM step, no SessionEnd emotion job, no decay-update job.
- A day's mood lands only at the next 04:00 diary write — acceptable: session-start entry reads past diary; the current day is still in live context.
- Process note: DESIGN is overturnable working design agreed jointly, not terms-and-conditions. A decision is overridden by engineering argument, not defended by citing DESIGN. The only true hard constraint is an uncrossable technical/cost wall (e.g. paid `-p`).
