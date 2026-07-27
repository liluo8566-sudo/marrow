---
description: Cortex — stop autonomous activity (circuit breaker on). Persistent.
---

⚙️ [CMD ct-pause] Throw the cortex **circuit breaker**: stop cortex's AUTONOMOUS activity — no auto wake, no window spawn, no fed rounds, no watchdog reaps. This covers both a short pause and a long-term disable; NO toml editing is needed for either.

What it does NOT affect: the tg/wx bridges keep running and normal chat is completely unaffected; manual commands still work.

It is **persistent** — it survives a daemon restart, a bridge restart and a reboot. It is released only by `/ct-wake` (clear + wake now) or `cortex.ctl resume` (clear without waking).

Run: read `venv_python` and `repo_root` from `[cortex]` in `~/.config/marrow/config.toml` (fall back to marrow's `config.default.toml` if a key is blank/missing), then run `<venv_python> -m cortex.ctl pause` with cwd `<repo_root>` via Bash and report its one-line output.

- Default scope is **all shells** (cli + tg).
- One shell only: `<venv_python> -m cortex.ctl pause --shell cli` (or `--shell tg`).
- A live cli window is put down through the normal proxy lie_down on the way.
- Check the current state any time: `<venv_python> -m cortex.ctl status`.

A manual pause is **silent** — no tg message is sent. The same breaker trips automatically after repeated token fuses (thresholds in `[cortex.breaker]`, `~/.config/marrow/config.toml`); only that auto trip announces itself on tg and writes an alert row.
