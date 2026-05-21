# CC 2.1.142 / 2.1.143 — two regressions, pin to 2.1.141

## Bug A — toolcall parse-fail (2.1.143 only)
GitHub #60033 / #59787. Pure 2.1.143 regression.

## Bug C — empty-turn-after-thinking (2.1.142+)
Symptom: thinking block → `stop_reason=end_turn` with no text/tool_use.
Scan (2026-05-18, 600 jsonl / 8712 turns): 2.1.116–2.1.141: 0 cases; 2.1.142: 6, 2.1.143: 19 (~0.5%, model/context-independent).
Recovery: reply any token (`.`/`继续`).

## Pin to 2.1.141

Native install: `~/.local/bin/claude` → `~/.local/share/claude/versions/<ver>`. Updater retops symlink to highest version on every start.

Actual pin methods:
- `~/.claude/settings.json` → `"env": { "DISABLE_AUTOUPDATER": "1" }`
- `~/.claude.json` → `autoUpdatesProtectedForNative: false`
- `DISABLE_UPDATES` (blocks manual updates)
- `minimumVersion` = FLOOR not pin

Pin steps (order matters):
1. Close ALL claude sessions
2. Terminal:
   - `rm -rf ~/.local/share/claude/versions/<bad-version>*`
   - `ln -sfn ~/.local/share/claude/versions/2.1.141 ~/.local/bin/claude`
   - `claude --version` → 2.1.141
3. Reopen claude

Official downgrade: `curl -fsSL https://claude.ai/install.sh | bash -s <version>` (do steps 1–2 first).

Re-scan: Glob `~/.claude/projects/*/*.jsonl`, aggregate `type==assistant` by `message.id`, bucket by version.
