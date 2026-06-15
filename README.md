# Marrow

Personal AI memory system. SQLite-backed, model-agnostic, one dashboard. Hooks into Claude Code to remember conversations, track tasks, and surface relevant context automatically.

## What it does

- Captures every conversation into a local SQLite database
- Extracts tasks, emotions, entities, milestones, and memes at session end
- Writes a daily diary aggregating the day's sessions
- Surfaces relevant memories when you mention something related (recall)
- Renders a live dashboard + sub-pages in markdown (Obsidian / VSCode / any editor)
- WeChat bridge available via [synapse-wx](https://github.com/Jaynechu/synapse-wx) (optional)

## Requirements

- macOS (launchd scheduler, sips image processing)
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) with active subscription
- Obsidian, VSCode, or any markdown editor for viewing the dashboard

## Setup

### 1. Clone and install

```bash
git clone https://github.com/Jaynechu/marrow.git ~/CC-Lab/marrow
cd ~/CC-Lab/marrow
uv sync
```

### 2. Initialize

```bash
uv run python -m marrow.cli init
```

This creates `~/.config/marrow/` with:
- `config.toml` (your config, copied from defaults)
- `marrow.db` (SQLite database)

### 3. Configure persona

Edit `~/.config/marrow/config.toml` and add your persona:

```toml
[persona]
user_name = "YourName"
assistant_name = "AssistantName"
user_aliases = ["Nick1", "Nick2"]
assistant_aliases = ["AltName"]
relationship_terms = []
anchor_keys = ["YourName", "AssistantName"]
```

- `user_name` / `assistant_name`: how you and your AI appear in diary, timeline, transcripts
- `user_aliases`: other names that refer to you (for entity exclusion + recall)
- `anchor_keys`: meme keywords that never age out
- All other persona context (personality, interaction style) goes in `~/.claude/CLAUDE.md`

### 4. Configure paths

Still in `~/.config/marrow/config.toml`:

```toml
[paths]
dashboard = "~/path/to/your/dashboard.md"
db_pages = "~/path/to/your/db-pages"
```

Default: `~/Desktop/NY/dashboard.md` and `~/Desktop/NY/db-pages/`. Change to wherever you want your markdown files.

### 5. Register hooks in Claude Code

Add to your `.claude/settings.json` (or project settings):

```json
{
  "hooks": {
    "SessionStart": [{ "command": "uv run python -m marrow.hooks session_start" }],
    "SessionEnd": [{ "command": "uv run python -m marrow.hooks session_end" }],
    "UserPromptSubmit": [{ "command": "uv run python -m marrow.hooks user_prompt_submit" }],
    "PreToolUse": [{ "command": "uv run python -m marrow.hooks pretool_use" }]
  }
}
```

### 6. Register MCP server

Add to your `.claude/settings.json`:

```json
{
  "mcpServers": {
    "marrow": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/marrow", "python", "-m", "marrow.daemon"],
      "cwd": "/path/to/marrow"
    }
  }
}
```

### 7. Download embedding model

Marrow uses [bge-m3](https://huggingface.co/BAAI/bge-m3) (600MB ONNX) for semantic recall. First daemon launch downloads it automatically. To pre-download:

```bash
uv run python -c "from marrow.recall import _load_model; _load_model()"
```

If download fails or you skip this step, recall gracefully degrades to text search (FTS5) — everything works, just no semantic matching. 24GB+ RAM recommended for best performance.

### 8. Install launchd jobs

```bash
uv run python -m marrow.cli install-launchd
```

This sets up: watcher (live md sync), daily routine (07:00), catchup (19:00), backup (03:00), aging (weekly).

## Customization

See [CUSTOMIZE.md](CUSTOMIZE.md) for recall weights, LLM provider chain, embedding model, sub-page layout, and backup paths.

The diary writing style lives in `marrow/daily.py` (DIARY_PROMPT). Edit directly if you want a different tone or format.

## Updating

```bash
cd ~/CC-Lab/marrow
git pull
uv sync
```

Your `~/.config/marrow/config.toml` is outside the repo and won't be overwritten. New config keys from `config.default.toml` are auto-merged on load.

Restart the watcher after updating:

```bash
launchctl kickstart -k gui/$(id -u)/com.marrow.watcher
```

## Architecture

- DESIGN.md: goals, outcomes, constraints
- MAP.md: how each feature works (speed-read for AI sessions)
- DECISIONS.md: debated technical choices with rationale
