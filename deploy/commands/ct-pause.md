---
description: Cortex — DND on.
---

⚙️ [CMD ct-pause] Read `venv_python` and `repo_root` from `[cortex]` in `~/.config/marrow/config.toml` (fall back to marrow's `config.default.toml` if a key is blank/missing). Run `<venv_python> -m cortex.ctl pause` with cwd `<repo_root>` via Bash and report its one-line output. Exit DND later with `/ct-wake` (wakes AND unpauses) or `<venv_python> -m cortex.ctl resume` with the same cwd (unpauses without waking).
