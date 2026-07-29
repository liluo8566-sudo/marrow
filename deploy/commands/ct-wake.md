---
description: Cortex — release the breaker & wake. Args: tg | cli | all (default all).
---

⚙️ [CMD ct-wake] This is the **release** for `/ct-pause` and for an automatic fuse trip. Scope comes from the argument: `tg`, `cli`, or `all` / empty.

Setup: read `venv_python` and `repo_root` from `[cortex]` in `~/.config/marrow/config.toml` (fall back to marrow's `config.default.toml` if a key is blank/missing). Run everything via Bash with cwd `<repo_root>`.

Pick the mode by argument:

1. **tg** — `<venv_python> -m cortex.ctl resume --shell tg`
   Releases ONLY the tg half; cli stays tripped. No immediate kick — tg wakes on its next pacemaker pass, and any alarm that was due while the breaker stood fires then (it was never consumed).
2. **cli** — `<venv_python> -m cortex.ctl resume --shell cli`
   Releases ONLY the cli half; tg stays tripped. The resident window wakes on the next pass.
3. **all** (or no argument) — `<venv_python> -m cortex.ctl wake`
   Clears the WHOLE breaker (both shells, manual or auto) and immediately wakes the resident window via the standard run_wake pipeline. Autonomous activity resumes fully: scheduled wakes, fed rounds, watchdog reaps.

Never use `ctl wake` for a single-shell request — it always clears BOTH shells. Do not spawn or resume any window yourself — the CLI owns the wake pipeline.

Report the one-line output in plain words (breaker cleared? / already-awake no-op / ear wake / resumed or spawned fresh). To inspect state first: `<venv_python> -m cortex.ctl status`.
