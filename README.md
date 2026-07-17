# Marrow

Personal AI memory system for Claude Code. SQLite + FTS5 + vector search, markdown surfaces (daybrief, monitor, sub-pages).

Captures every Claude Code session locally — events archived per turn via the Stop hook — then surfaces relevant memories automatically when you open a new session.

## Quick start

```bash
# 1. Fork this repo, then clone your fork
git clone https://github.com/<you>/marrow.git
cd marrow

# 2. Run the installer
python -m marrow install

# 3. Optional: edit your persona/path config
$EDITOR ~/.config/marrow/config.toml

# 4. Open a new Claude Code session — memory is active
```

> The installer creates a venv and installs all deps automatically. No `uv` required.

## What install does

- Creates `~/.config/marrow/` with `config.toml` (from defaults) and `marrow.db`
- Registers 4 Claude Code hooks in `~/.claude/settings.json` (SessionStart, SessionEnd, UserPromptSubmit, PreToolUse)
- Registers the `marrow` MCP server with Claude Code
- Symlinks slash commands and agents into `~/.claude/`
- Installs launchd jobs: watcher (live md sync), backup (03:00), aging (weekly)
- Downloads the bge-m3 ONNX embedding model (~600 MB); gracefully degrades to FTS5-only if absent

To pre-download the model (recommended):

```bash
huggingface-cli download BAAI/bge-m3 --include "onnx/*" "tokenizer.json"
```

The ONNX runtime is used for inference — PyTorch is **not** required.

Run `python -m marrow install --update` after `git pull` to sync hooks/MCP without touching your config.

## Configuration

Reference: [`marrow/config.default.toml`](marrow/config.default.toml)

Key sections in `~/.config/marrow/config.toml`:

| Section | What to set |
|---|---|
| `[persona]` | `user_name`, `assistant_name`, aliases, `anchor_keys` |
| `[paths]` | `db_pages`, `daybrief`, `monitor` (defaults work out of the box under `~/.config/marrow/`) |
| `[llm]` | Provider chain — `claude_cli` default |
| `[recall]` | Fusion weights, vector window, per-rank content caps |

Persona context (personality, tone, interaction style) goes in `~/.claude/CLAUDE.md`, not here.

## Commands

Slash commands installed into `~/.claude/`:

| Command | What it does |
|---|---|
| `/diary` | Read diary context for a requested date |
| `/embed` | Embed pending memory rows |
| `/refresh` | Force-render daybrief + monitor; add `--all` for sub-pages |
| `/switch` | Pick a recent session to resume |
| `/sticker-entry` | Batch-fill sticker descriptions |

For chat-channel commands (WeChat, Telegram), see [synapse](https://github.com/Jaynechu/synapse).

## Architecture

- **4 hooks** — inject recall context on prompt, archive events per turn via the Stop hook
- **MCP daemon** — serves `recall`, `sticker`, and `event_embed` tools to Claude Code
- **SQLite** — events, milestones, entities, memes, stickers; FTS5 full-text index
- **sqlite-vec** — 1024-dim bge-m3 embeddings, 90-day rolling window
- **Surfaces** — auto-rendered markdown pages: daybrief, monitor, sub-pages (Obsidian / VSCode / any editor)

Internal docs: [`MAP.md`](MAP.md) (how each feature works) · [`DESIGN.md`](DESIGN.md) (goals and constraints)

## Updating

```bash
git pull
python -m marrow install --update
```

Your `~/.config/marrow/config.toml` is outside the repo and is never overwritten. New keys from `config.default.toml` are auto-merged on load.

## Uninstall

```bash
python -m marrow install --uninstall
```

Removes hooks, MCP entry, launchd jobs, and `~/.claude/` commands. Does not delete `~/.config/marrow/` (your data).

## Requirements

- macOS (Linux/Windows contributions welcome)
- Python 3.12+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) with active subscription
- `pip`/`venv` support in the Python install

## License

MIT
