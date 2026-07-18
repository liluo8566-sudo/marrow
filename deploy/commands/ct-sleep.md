---
description: Cortex — lie down until a given time.
---

⚙️ [CMD ct-sleep] $ARGUMENTS is either HH:MM or a number of minutes. Read `venv_python` and `repo_root` from `[cortex]` in `~/.config/marrow/config.toml` (fall back to marrow's `config.default.toml` if a key is blank/missing). Run `<venv_python> -m cortex.ctl sleep --until $ARGUMENTS` with cwd `<repo_root>` if it looks like HH:MM, else `<venv_python> -m cortex.ctl sleep --min $ARGUMENTS` with the same cwd if it's a bare number. Report the one-line output.
