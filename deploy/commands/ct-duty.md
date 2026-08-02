---
description: Cortex — rotate duty and/or kick shell. Args: cli | tg | off | all.
---

⚙️ [CMD ct-duty] Decide which cortex shell is **on duty**. At most one runs; the other is held. This is also the release for a manual pause or an automatic fuse trip — the command clears the breaker first.

Setup: read `venv_python` and `repo_root` from `[cortex]` in `~/.config/marrow/config.toml` (fall back to marrow's `config.default.toml` if a key is blank/missing). Run everything via Bash with cwd `<repo_root>`.

One command, scope by argument — `<venv_python> -m cortex.ctl duty <cli|tg|off|all>`:

1. **cli** — cli on duty, tg held. The tg session goes quiet; the resident window wakes now.
2. **tg** — tg on duty, cli held. A live cli window is put down (proxy lie_down, no alarm booked); the bridge gets a due-now round and a socket kick.
3. **off** — both held. Nothing autonomous runs on either side.
4. **all** — nothing held, both kicked (tg first).

Order inside a swap is fixed: the hold lands on disk before any kick, so the two shells are never active at the same instant. The incoming shell resumes its old window unless it is over `[duty].fresh_token_threshold` or older than `[duty].fresh_age_hours` — then it spawns fresh; the retired window is left open for the user to close.

The mode argument is required and must be one of the four; anything else exits non-zero without touching state.

Do not spawn, resume or put down any window yourself — the CLI owns both pipelines. Plumbing that stays available for narrower work: `cortex.ctl pause|wake|resume [--shell cli|tg]` (breaker only, no duty change).

Pausing or resuming a single shell WITHOUT changing who is on duty -> /ct-pause, never map that request onto a duty mode.

Report the one-line output in plain words (breaker cleared? / new mode + hold / which shell woke, fresh or resumed / cli put down). To inspect state first: `<venv_python> -m cortex.ctl status`.
