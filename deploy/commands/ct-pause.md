---
description: Cortex — pause/resume one shell's autonomy without touching duty. Args: pause|resume cli|tg|all.
---

⚙️ [CMD ct-pause] Throw or release a temporary breaker on top of the duty roster. This never changes who is on duty — it only stops or restarts autonomous activity for the shell(s) named.

Setup: read `venv_python` and `repo_root` from `[cortex]` in `~/.config/marrow/config.toml` (fall back to marrow's `config.default.toml` if a key is blank/missing). Run everything via Bash with cwd `<repo_root>`.

Args: `pause|resume cli|tg|all` — `all` = omit `--shell`.

1. **pause cli|tg** — `<venv_python> -m cortex.ctl pause --shell <cli|tg>`. Holds one shell. Scopes MERGE with whatever already stands (pause cli, then pause tg -> both held); a second pause never releases the shell the first one holds.
2. **pause all** — `<venv_python> -m cortex.ctl pause`. Holds both.
3. **resume cli|tg** — `<venv_python> -m cortex.ctl resume --shell <cli|tg>`. Lifts the breaker for one shell only.
4. **resume all** — `<venv_python> -m cortex.ctl resume`. Lifts the breaker for both.

The breaker sits OVER the duty hold, not instead of it: if duty still holds the shell after resume, the shell stays quiet — the breaker layer is off, but duty's own hold is untouched. Changing who is on duty is /ct-duty's job, never this command.

Do not rotate, kick, or start any shell yourself — this command never kicks anyone. Rotating duty, kicking a shell awake, or starting one on duty -> /ct-duty, never map that onto pause/resume.

Report the one-line stdout verbatim — it already carries the full world state (`duty: cli <icon> / tg <icon>`). To inspect state first: `<venv_python> -m cortex.ctl status`.
