# Synapse-WX — WeChat dendrite plan

> Independent repo (`/Users/Gabrielle/CC-Lab/synapse-wx/`), Python, MIT, open-sourceable.
> New build from scratch — not a fork. weclaude already archived to `~/CC-Lab/archives/weclaude/`.
> Goal: replace weclaude — solve `/model` passthrough + sessionend waste + multi-channel extensibility + multimodal IO in one shot.

## Goals
1. WeChat uses native cc-equivalent commands (`/model` `/clear` `/rewind` `/stop` etc) — bridge owns slash routing on top of no-p stream-json
2. sessionend reduced to ≤ 3 LLM calls/day (6h inactive trigger), no longer per-message
3. Multi-channel architecture room — provider adapter (cc / Codex / Qwen) × channel adapter (wx / web / iOS) orthogonal
4. Multimodal IO — in: text / image / pdf / voice; out: text / image / file / sticker
5. **Zero code coupling to marrow** — bridge talks to cc only; cc calls marrow through MCP. marrow swappable for any memory backend.
6. **Open-sourceable** — anyone can clone, configure iLink + cc OAuth, run without installing marrow. marrow integration is optional, configured via a hook command string.

## Out of Scope
- iOS / macOS / web channel adapters (separate dendrite repos later)
- Pulse proactive push loop (marrow main + separate launchd cron — Phase F)
- Group chat
- Stellan wallet / other addons
- Claude Desktop / official iOS app MCP (marrow main repo concern)

## Architecture
```
WeChat ──▶ synapse-wx (this repo, Python, independent, MIT)
             │ stdin/stdout (stream-json, no-p)
             │ env: MARROW_BRIDGE=1
             ▼
          cc CLI subprocess (persistent)
             ├──▶ MCP ──▶ marrow daemon (recall / events / sticker)
             └──▶ hooks (SessionStart inject / SessionEnd → see env-gate below)
```

### marrow ↔ synapse-wx contract
- synapse-wx **does not import marrow**
- Two crossing points only:
  1. **Env var `MARROW_BRIDGE=1`** set by bridge on cc spawn — tells marrow SessionEnd hook to skip its popen sessionend_async
  2. **Subprocess fire** — `python -m marrow.sessionend_async <sid>` on 6h timer. Configured as a templated command string in synapse-wx config; empty string = no marrow integration.
- Any user without marrow installed: config skips the hook command, env var is harmless (marrow hook doesn't exist to read it).

---

## Status (2026-06-02)

- **Phase 0 ✓ done** — P0.A realpath landed (marrow `4eeea14`); P0.B archive completed pre-session; P0.D env gate landed (`4eeea14` + `cddddec` 12h TTL); P0.C dropped.
- **Phase A ✓ done** — A1–A8 all landed in `~/CC-Lab/synapse-wx/`. 14 commits, 152 pytest passing, ruff clean, `scripts/live_verify.py` 29/29 PASS (incl. AlertSink → marrow alerts table round-trip via new `mw add-alert` CLI). WeChat live verified 2026-06-02 04:03: `/info` returned `default | SID-— | ?(5h) ?(7d) | 0.0k` to Lumi's phone from the synapse-wx LaunchAgent.
- **Marrow-side change shipped** — `mw add-alert <warn|critical> <type> <message> [--source]` added to `marrow/cli.py` (`94b499d`) so synapse-wx's `AlertSink.marrow_repo_cmd` has a real endpoint.

## Next session backlog (capture, not yet planned)

- **sid display name** — cc's session picker shows LLM-summary titles (e.g. "Initialize synapse-wx Phase A project structure"); current bridge `/info` shows only `SID-xxxxxxxx`. Want either a label (LLM-summarised) or `[YYYY-MM-DD HH:MM]` prefix surfaced in `/info` and any future `/ss` list. Discuss design before building.
- **Monorepo CLAUDE.md move** — Lumi wants `~/CC-Lab/CLAUDE.md` shared across marrow/synapse-wx so cc cwd anywhere under CC-Lab inherits the same project memory. cc only walks cwd → git root for project-level CLAUDE.md, so this needs either a symlink trick, a build step, or splitting global vs. project memory differently. Discuss before touching.
- **`wx` alias** — add `alias wx="cd ~/CC-Lab/synapse-wx && claude"` to `~/.zshrc`, symmetric with existing `mm`/`ny`/`nyr`/`study`. Trivial; do once monorepo CLAUDE.md decision is settled.
- **typing indicator** — port weclaude `send_typing` so WeChat shows "正在输入" again. ~30 LOC; Phase B candidate.
- **`/info` field redesign** — Lumi wants real 5h usage %, not "hours until reset". cc 2.1.159 stream-json does NOT expose `percentUsed` (only `resetsAt` + `isUsingOverage`); cc's own statusline reads quota via internal endpoint not exposed here. Options surfaced (pick later): (A) display cumulative `total_cost_usd` + reset countdown — cheap, immediate; (B) short `claude -p "/status"` subprocess to parse cc's own status — ~3-5s blocking + extra LLM call + may not work in -p mode; (C) wait for cc to re-add `percentUsed` in stream-json. Lumi to decide what else she wants surfaced before we touch this — possibly token total / session label / cost-this-window in one line.

---

## Phase 0 — preflight (do before A1) ✓ done

### P0.A — `marrow/_atomic.py` realpath
- Add `path = os.path.realpath(path)` as first line of `atomic_write()`
- Effect: `os.replace` lands on symlink's true target, not the symlink itself
- Safe predictive measure; existing canonical paths are real files, realpath is no-op there

### P0.B — archive weclaude
- `mv ~/CC-Lab/external/weclaude ~/CC-Lab/archives/weclaude`
- Retired, no upstream pulls
- Dead code stays inside archive (historical reference)

### P0.D — marrow SessionEnd env gate (cross-cutting)
- **Why**: bridge will kill+respawn cc on every `/model` / `/clear` / `/rewind`. cc fires its SessionEnd hook on each kill → marrow archives + spawns popen sessionend_async → wastes one LLM call per command. Bridge wants sole authority over sessionend timing.
- **Design** (reuse existing manual_skip control plane, do NOT invent new audit row type):
  - `marrow/hooks.py` SessionEnd: if `os.environ["MARROW_BRIDGE"] == "1"` → archive events (cheap, local) + write `lifecycle:end` + write manual_skip marker `bridge_owns` + return (no popen)
  - `marrow/sessionstart_catchup.py`: precondition — if sid has `bridge_owns` marker AND no newer ok row, classify=skip; else fall through to existing 7-state logic (so fail rows from real bridge-fired sessionend_async still trigger state-5 retry)
- **Failure tolerance**:
  - Bridge 6h timer fires sessionend_async → LLM fails → writes fail/partial row (newer than bridge_owns marker) → next SessionStart catchup state-5 retries once
  - Bridge crashes before firing → next cc SessionStart catchup sees stale bridge_owns + ppid dead → ??? must NOT silently lose sessionend forever. **Decision**: bridge_owns marker has a TTL — if `now - marker_ts > 12h` AND no newer ok row, catchup treats it as stale and falls through to state-5 spawn. Belt-and-suspenders.

### ~~P0.C — handover.md iCloud move~~ — **dropped**
- No demonstrated cross-device read need; DB backup already in iCloud via launchd. Revisit if iPhone access becomes a real ask.

---

## Phase A — MVP ⭐ ✓ DONE (target: WeChat + 屿忱 fresh conversation working)

> Landed 2026-06-02. All 8 subtasks ✓. Exit criteria all met (`/info` round-trip confirmed live from Lumi's WeChat).
> Commits: `8e40cf6` A1 · `56a976b` deps · `47f85e5` A7 · `f77ca13` A6 · `9913350` A2 · `53ae901` A3 · `9ca8c63` A4 · `e6c8737` A5 · `ef1dca7` A8 · `f91a812` entry-module fix · `954c6f7` capability harness · `6df12a0` AlertSink --source flag + marrow round-trip · `526144d` finish_phase_a.sh · `a5c74ef` launchd PATH `~/.local/bin`.


### A1 — repo bootstrap
- Location: `/Users/Gabrielle/CC-Lab/synapse-wx/`
- Python 3.12 venv · ruff · pytest · MIT license
- `README.md` + `pyproject.toml` + `.gitignore`
- Initial layout: `synapse_wx/{providers,ilink,commands,channels}/` + `tests/`
- git init, no initial commit until A2 spike validates

### A2 — provider adapter
- `synapse_wx/providers/base.py` — abstract:
  - `spawn(env={})` — start subprocess
  - `send(msg: str)` — write user message
  - `recv()` — generator yielding events until result; raises on subprocess death
  - `cancel()` — best-effort interrupt (Phase A implementation = kill subprocess)
  - `close()` — graceful stdin.end → SIGTERM → SIGKILL (cyberboss 3-stage pattern)
- `synapse_wx/providers/cc.py` — first impl:
  - args: `--output-format stream-json --input-format stream-json --verbose --permission-mode bypassPermissions [--model X] [--resume SID]`
  - **No** `--setting-sources "" --strict-mcp-config` (those are marrow pipeline isolation; bridge needs persona + MCP + hooks alive)
  - Spawn env merges `MARROW_BRIDGE=1` into existing os.environ
  - line-delimited JSON parse, dispatch by `type`: `system` / `assistant` / `user` / `result` / `control_request`
  - `control_request` (permission prompts) plumbed up — Phase A bypasses all so this path mostly dormant; Phase E (yes/no relay) hooks here
  - **No stdin interrupt protocol** (cc Issue #41665 not yet shipped) — `cancel()` = kill+respawn-with-resume
- Mock echo provider for tests (`providers/mock.py`) — send → recv loop without real cc
- Test: spawn → send → recv events → assert result text → close clean

### A3 — iLink client
- `synapse_wx/ilink/client.py` — salvage `weclaude/ilink_client.py` field fixes + add retry framework
- `synapse_wx/ilink/cursor.py` — polling cursor persistence (resume on restart)

### A4 — main message loop
- Inbound: iLink poll → 5s debounce accumulate → flush → `provider.send`
- Hold-word window extension: any bubble in buffer matches hold word → debounce extends to 10s (any new hit resets timer)
  - Initial list: `等` `稍等` `等等` `先` (exact single-bubble match, no substring; avoids "等下" false positive)
  - List config-file driven; tune by observed miss/false-positive rates
- Outbound: `provider.recv` stream → semantic bubble split (≤30-50 char · newline > sentence-end > CN comma > hard cut) → iLink send
- Time anchor injection (stdin prefix `[time: YYYY-MM-DD Day HH:MM | gap: Nh]`) — salvaged from weclaude

### A5 — command routing
- `synapse_wx/commands/registry.py` — three-tier:
  1. bridge handlers (`/model` `/clear` `/rewind` `/stop` `/info` etc) — intercept, do not forward
  2. natural alias shortcuts (`4.7` / `4.8` / `sonnet` / `haiku` / `opus`) — route to `/model` handler with mapped id
  3. fallback — forward as user message
- No cc-native passthrough exists in stream-json mode (cc doesn't parse slash commands when it's reading user JSON over stdin). Every command is bridge-implemented.

**Phase A commands**:
- `/info` — format: `Opus 4.7 [1M] | SID-xxxxxxxx | 12%(5h) 30%(7d) | 118.0k`
  - Model: from current spawn args
  - SID: from `system` event init
  - total token: running sum from `assistant.message.usage` (cyberboss process-client.js:152 pattern)
  - 5h % / 7d %: **dump-and-discover** during A1 — spawn cc, log every event type to disk, find where `rate_limit_event` (or equivalent) surfaces in current cc version. If found → wire it. If not found (cc Issue #57699 says the field was dropped 2026/3) → display `?(5h) ?(7d)` until cc adds it back. No stub, no delay.
- `/model X` and aliases — kill cc subprocess (graceful close) → respawn with `--model X --resume <sid>` → confirm "Switched to <human-name>"
- `/clear` — kill cc → respawn WITHOUT `--resume` → confirm "New session"
- `/rewind N` (Phase E, dormant for now) — jsonl truncate + respawn with resume
- `/stop` — kill cc → respawn with `--resume <sid>` (no message sent) → confirm "Stopped, session kept"
  - Best available approximation of Esc; cc has no mid-stream stdin interrupt protocol
  - Cost: ~2s cold start. Trade-off accepted.

### A6 — sessionend trigger
- 6h inactivity timer per session
- On fire: subprocess `python -m marrow.sessionend_async <sid>` detached (4-flag popen_detach pattern), then clear local buffer
- cc subprocess **does NOT exit** — bridge fires sessionend out-of-band; conversation can continue
- New message arriving → timer resets
- `/clear` → real session close, timer void
- One sid can fire sessionend pipeline multiple times per day, each a separate snapshot
- **Config option**: `sessionend_command` template in synapse-wx config (default `python -m marrow.sessionend_async {sid}`); empty string disables — open-source users without marrow set it empty

### A7 — launchd
- `com.synapse-wx.bridge.plist` — RunAtLoad + KeepAlive on Crash + throttle 30s
- Logs `~/Library/Logs/synapse-wx.{out,err}.log`

### A8 — retry framework + alerting + sleep-detect
- Unified retry: iLink ret=-2 / network timeout / cli crash → exponential backoff, cap 5 failures → write alert to marrow alerts table (via subprocess `python -m marrow.repo add_alert ...` or marker file — TBD during A8)
- Bridge death → launchd restart + bridge self-check → "我重启了" message to File Transfer Helper
- cc death → bridge fallback bubble "cc 连不上稍等"
- **sleep-detect (must-have)**: pyobjc-framework-Cocoa observer on `NSWorkspaceWillSleepNotification` / `NSWorkspaceDidWakeNotification`
  - Will-sleep: mark bridge state paused, stop iLink poll, do NOT close cc subprocess (let it freeze with the OS)
  - Did-wake: force iLink reconnect + cursor catchup (pull messages sent during sleep) + verify cc subprocess still alive (resurrect if dead)
  - Replaces all the "never sleep / caffeinate" workarounds Lumi was relying on

**Phase A exit criteria**: chat with 屿忱 via WeChat · `/model` aliases work · `4.7` switches model · 6h inactive fires marrow sessionend · sleep/wake cleanly handled · death self-heals · alert on systemic fail.

**Verification (2026-06-02)**:
- Unit: 152 pytest, ruff clean.
- Live harness `scripts/live_verify.py` 29/29 PASS — slash commands (6/6), idle fire + real popen → marrow row (5/5), idle live integration (2/2), sleep/wake handler chain (3/3), AlertSink + HealthGate (6/6), AlertSink → marrow alerts round-trip (3/3), provider echo (4/4).
- WeChat: `/info` answered live from Lumi's phone via synapse-wx LaunchAgent (sid `8934`, then re-loaded).
- launchd PATH fix: `__USER_HOME__/.local/bin` prepended so `claude` CLI resolves under the LaunchAgent environment.

---

## Phase B — multimodal inbound + buddy
### B1 — inbound media
- image / pdf / voice: iLink download → voice transcript (iLink built-in) → image/pdf local path fed to cc vision
- cc subprocess `--image path` arg or stdin embed
- Storage: marrow `media` table (FUTURE.md `marrow_media_store`) — cc vision auto-generates description + tags + embedding, retention managed by marrow (anchored=permanent / loose=90d age-out)
- Bridge does IO only, not retention/storage placement

### B2 — buddy bubble filter
- Strip `<!-- buddy: ... -->` from outbound at split time
- Time-window mute: `BUDDY_MUTE_WECHAT = "22:00-08:00"` default
- cc statusline / buddy MCP in cc unaffected

### B3 — weclaude session switch port
- `/ss` list sessions · `/use N` switch — port from archived `weclaude/bridge.py:665-791` jsonl scan logic

---

## Phase C — multimodal outbound + sticker catalog
### C1 — outbound media
- image / file via iLink — translate cyberboss `src/adapters/channel/weixin/media-send.js` to Python (includes AES-ECB)

### C2 — sticker catalog (marrow main collaborates)
**Storage**
- marrow `stickers` table: `id / path / sha256 / desc / vec384 / source(wechat/finder) / created_at / last_used`
- Real path `~/Desktop/NY/stickers/`, symlink `~/.config/marrow/stickers/` for daemon use
- Flat naming `stk_NNN_desc.{ext}` (jpg/png/gif/webp, no transcoding)
- `_thumb/` subdir caches 240px webp (`_` prefix hides in Finder)
- No tags column — embedding via desc + vec384

**Ingest (cyberboss pattern, main-LLM-driven + watcher fallback)**
- Two entry points feed one watcher:
  - Send sticker to 屿忱 via WeChat → bridge drops file in `stickers/` → watcher catches
  - Finder drop into `stickers/` → watcher catches
- Watcher (Python `watchdog` ~20 LOC) on new file:
  1. SHA256 vs existing → match = silent skip
  2. No match → cc native vision (OAuth, no external endpoint) judges + writes desc → calls `sticker_save(filepath, desc)` MCP tool
  3. daemon inserts row + renames `stk_NNN_desc.{ext}`
- File delete → soft-delete by path/prefix match
- Ingest prompt: TBD by Lumi, placeholder `synapse_wx/prompts/sticker_save.md`
- WeChat ingest confirm card: `✅ stk_115 / 描述: orange cat holding phone sassy`

**Outbound (embedding retrieval, two-call)**
- Main LLM inline `<sticker query="..." />`
- Bridge parses → embedding top-5 → tool returns `[{id, desc}]`
- LLM picks ID → `sticker_send(id)` → iLink **sticker-format** payload (not generic file)

**Management**
- Browse: Finder large-icon view on `~/Desktop/NY/stickers/`
- Edit desc: WeChat → tool `sticker_update(id, desc)`
- Delete: Finder rm OR WeChat → tool `sticker_delete(id)`
- No dashboard subpage / reconcile until frontend Phase

**Seed**: empty (no cyberboss 75-tag list)

**Credit**: README → cyberboss for LLM-save flow + SHA256 dedup + silent skip + confirm card

### C3 — CLI text stickers (dormant)
`【心如止水.jpg】` text placeholder, render layer later

---

## Phase D — marrow main companion work (separate plan, marrow repo)
### D1 — channel router
- events table gains `channel` column (wechat / cc / desktop / ios)
- channel-agnostic recall / write / pulse interfaces
- Same phase as affect recall redesign (Doing #1)

### D2 — handover atomic write — done in Phase 0 P0.A

### D3 — daemon-side MCP client session tracker
- For Desktop / iOS MCP clients: monitor connect/disconnect + idle timeout → fire sessionend
- Sets the stage for Ombre-Brain mode (client LLM self-calls recall tool)

---

## Phase E — post-MVP nice-to-have
- **WeChat permission yes/no relay** — cc Bash/Edit prompt (with `--permission-mode default` swap) → bridge pushes to WeChat → phone replies `/yes` `/no` `/always` → bridge sends `control_response` back. Off in Phase A (we use `bypassPermissions`).
- **WeChat conversation log** — port weclaude `memory_store.py`-style daily MD log (`~/.config/synapse-wx/memory/YYYY-MM-DD.md`); per-day file, append User/Bot pairs. ~30 LOC. Open-source value: lightweight history without marrow.
- `/back N` — jsonl transcript truncate (phone-side `/rewind`)
- Cross-channel handover — sid shared, WeChat ↔ cc resumable
- **continuation thinking** — mid-stream new msg → abort+merge+resend; instant release if cc just returned `result` event. Prereq spike: confirm cc no-p stream-json can mid-stream abort a single request without killing the subprocess (cc Issue #41665 must land first). Sensing layer (punctuation / length / `/go` / hold-word delta) revisited then.
- Conversation-aware split upgrade — Haiku-driven bubble cut instead of rules
- **Codex provider** — `providers/codex.py` ~200-300 LOC, no marrow env var coupling (codex doesn't run marrow hooks)

---

## Phase F — Pulse integration (marrow main + separate launchd cron)
Depends on Phase A8 outbound stability.
- marrow main: `inner_state` calc + multi-signal monitor (screen time / task followup / health / time-of-day)
- Separate launchd cron (not inside synapse-wx)
- Pushes via synapse-wx outbound send interface
- Same item as FUTURE.md Phase 5 `marrow_pulse_proactive_loop`

---

## Open Brainstorm (not in plan yet)
- WeChat-specific full alert mode — computer off, who tells Lumi? iOS Shortcut external health-check ping?

---

## Risks
- **cc stream-json field drift** — `rate_limit_event` already had a v2.1.80→2026/3 disappearance; `/info` 5h/7d will be best-effort with graceful `?` fallback
- **cc stdin interrupt** — Issue #41665 unshipped; `/stop` accepts ~2s cold start cost
- **iLink upstream single point** — API field changes, retry + version pin + alert; service outage = no WeChat path, no fallback
- **bridge crash leaves bridge_owns marker stale** — 12h TTL in catchup handles this
- **cyberboss not directly portable** — synapse-wx is clean MIT rewrite; cyberboss credit in README

---

## Credit
- **cyberboss** (WenXiaoWendy): persistent stream-json subprocess pattern · `control_request` protocol · sticker LLM-save flow · SHA256 dedup · sync-buffer concept · system-checkin-poller (Phase F)
- **weclaude** (Jaynechu fork of allenhuang0, now archived): time anchor injection · iLink field fixes · conversation log MD format (Phase E)
