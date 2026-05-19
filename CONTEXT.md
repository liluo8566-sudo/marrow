# Marrow

Personal AI memory + workflow system replacing the ny-memm pipeline. This glossary fixes terms only where the design docs were ambiguous or self-conflicting.

## Language

### Data lifecycle tiers

**Permanent keepsake**:
Memory that is add-only and never decays — milestones, diary, goose-bites, projects, study, major life facts.
_Avoid_: archive, cold storage

**Demote-sink**:
A row whose weight decays until it sinks below the active set but is never deleted; a keyword hit revives it.
_Avoid_: expire, evict, decay (decay names the weight drop, not the tier)

**Raw-stream**:
High-volume operational data under real retention and prune — event rows, resolved alerts, audit_log, DB dumps, orphaned jsonl, low-use stickers.
_Avoid_: log (overloaded), trash

### Reconcile

**Structured view**:
A rendered view whose rows are inherently structured (Open Threads, milestone, vocab, pit, alerts); reconciled by a visible per-row id.
_Avoid_: table view, list view

**Narrative view**:
A rendered view of free prose keyed by date heading (diary, goose-bites); reconciled by whole-block overwrite or full-block delete, never internally parsed.
_Avoid_: text view, freeform view

**Reconcile**:
The hook step that reads a hand-edited rendered file and writes the differences back into SQLite before re-rendering.
_Avoid_: sync, merge, import

### Emotion (Phase 2)

**valence / arousal**:
Per-episode affect rows (see DECISIONS.md). valence = positive↔negative; arousal = calm↔excited. Orthogonal to **importance** (saliency).
_Avoid_: mood-as-single-value, sentiment

**diary.mood**:
optional code-rollup of daily affect (display only); actual emotion per-episode rows in DECISIONS.md.
_Avoid_: feel, model_valence

**feel**:
An Ombre-Brain term, NOT a Marrow concept. Marrow's diary is the first-person lived layer; there is no feel table.
_Avoid_: using "feel" for any Marrow store

## Relationships

- Every stored record belongs to exactly one of **Permanent keepsake**, **Demote-sink**, or **Raw-stream**.
- A **Structured view**'s primary correction path is md edit; a **Narrative view**'s primary correction path is the `mw` CLI or telling Claude.
- **Reconcile** never splits a **Narrative view** block into new rows.
- **Cold vocab** is **Demote-sink**, not **Permanent keepsake**.
- Only **Raw-stream** is pruned; **Demote-sink** sinks but persists; **Permanent keepsake** is untouched.

## Flagged ambiguities

- "vocab" was placed in **Permanent keepsake** ("never decays") in DESIGN while also described as cold-decaying — resolved: vocab is **Demote-sink** (decays by use_count / last_seen, revived by keyword).
- "model" was used for both the conversation model and the embedding model — resolved: **Conversation model** generates replies and is swappable; **Embedding model** is a fixed local sentence-vector component used only for retrieval, never the conversation model, never a cloud / API embedding.
