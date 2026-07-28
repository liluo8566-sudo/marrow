---
description: Cortex — wake the resident window now; Clear the circuit breaker.
---

⚙️ [CMD ct-wake] This is the **release** for `/ct-pause` and for an automatic fuse trip: it clears the cortex circuit breaker (whole file — all shells, manual or auto) and then wakes the resident window.

After this, cortex's autonomous activity resumes normally: scheduled wakes, fed rounds and watchdog reaps all come back, and any alarm that was due while the breaker stood fires on the next pass (it was never consumed).

Run: read `venv_python` and `repo_root` from `[cortex]` in `~/.config/marrow/config.toml` (fall back to marrow's `config.default.toml` if a key is blank/missing), then run `<venv_python> -m cortex.ctl wake` with cwd `<repo_root>` via Bash and report its one-line output in plain words (breaker cleared? / already-awake no-op / ear wake / resumed or spawned fresh). Do not spawn or resume any window yourself — the CLI owns the wake pipeline.

To clear the breaker WITHOUT waking, run `<venv_python> -m cortex.ctl resume` with the same cwd. To see the current state, `<venv_python> -m cortex.ctl status`.
