# Customize before first run

Marrow ships with author defaults. Walk this list to override what doesn't fit. Everything is config-driven.

## 1. Persona

Edit `~/.config/marrow/config.toml` → `[persona]`:

```toml
[persona]
user_name = "YourName"
assistant_name = "AssistantName"
user_aliases = ["Nick1"]
assistant_aliases = ["AltName"]
relationship_terms = []
anchor_keys = ["YourName", "AssistantName"]
```

Runtime prompts (diary, sessionend, recall labels) read these values. Interaction style / personality goes in `~/.claude/CLAUDE.md` (not this repo).

## 2. cwd → recall bucket

File: `marrow/config.default.toml` → `[recall.buckets]`

```toml
project = ["/cc-lab"]
daily   = ["/desktop/ny"]
study   = ["/study"]
same_boost   = 0.10
diff_penalty = 0.10
```

Substring match against lowercased cwd. Empty list disables that bucket. Clearing all three disables cwd bias.

## 3. Recall weights / threshold

File: `marrow/config.default.toml` → `[recall]`

- `w_vec` `w_bm25` `w_recency` `w_affect` — main fusion weights (sum ≈ 1.0)
- `w_memes_vec` `w_entities_vec` `w_milestones_vec` `w_diary_vec` `w_tasks_vec` — anchor-table weights
- `min_score` — noise floor (0.35 = ship, 0.10 = debug)
- `limit` — max hits per recall call
- `event_max_chars` / `budget_chars` — output caps

## 4. LLM provider chain

File: `marrow/config.default.toml` → `[llm]` + `[llm.<name>]`

- `default` / `emergency` — provider names (chain falls back on failure)
- `[llm.claude_cli]` — uses cc subscription (no API key)
- `[tiers]` — cheap / mid / top model ids; callers pass tiers, not ids

## 5. Channel / bridge

Default channel is `cli`. WeChat / Slack / other bridges set `MARROW_CHANNEL=<name>` before spawning cc. Ignore if cli-only.

## 6. Embedding model

File: `marrow/config.default.toml` → `[embedding]`

- Default: `BAAI/bge-m3` (1024d, CLS-pool, L2-normalized, ONNX)
- Swap by changing `model` + `dim`
- Vec lanes auto-disable if model files absent (recall falls back to BM25 + recency + affect)

## 8. Dashboard / sub-pages

File: `marrow/config.default.toml` → `[paths]` + `[subpages]`

- `dashboard` — main dashboard.md location (default `~/Desktop/NY/dashboard.md`)
- `db_pages` — folder for DB-rendered sub-pages (default `~/Desktop/NY/db-pages`)
- `[subpages]` `top` / `bottom` / `hidden` — render order

## 9. Backup paths

File: `marrow/config.default.toml` → `[paths]` + `[backup]`

- `backup_dir` / `offsite_backup_dir` — local + offsite snapshot directories
- `keep` — daily snapshots retained

## 10. Authorized roots

File: `marrow/drift_sweep.py` → `AUTHORIZED_ROOTS`

Hardcoded list of paths drift sweep may mutate. Edit the constant or config-extract later.

---

Run `python -m marrow.cli init` (creates DB + migrations). No source edits required except item 10.
