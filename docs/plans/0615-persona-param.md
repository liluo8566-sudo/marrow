2026-06-15

# Persona Parameterize

Goal: extract hardcoded persona names from code into config.toml so the repo is fork-ready.

## Config

config.default.toml `[persona]`:
- user_name = "User", assistant_name = "Assistant"
- user_aliases = [], assistant_aliases = []
- relationship_terms = [] (entity exclusion, e.g. old wife/husband terms)
- anchor_keys = [] (memes never-decay list, explicit, not derived from aliases)

config.py: `persona()` helper returns merged dict with sanitized fallbacks.
- Empty string → "User"/"Assistant"; list fields filter non-strings/blanks.
- Derived: `all_user_terms()` = name + aliases + relationship_terms; `all_assistant_terms()` = name + aliases.

## Marrow files

1. config.default.toml — add [persona] section
2. config.py — persona() + derived helpers
3. daily.py:30-70 — DIARY_PROMPT template vars + render_diary_prompt() wrapper; examples with relationship terms
4. daily_prompts.py:46-48,73-93 — entity exclusion + meme/fact rules with Stellan/Lumi refs
5. sessionend_prompts.py:11-13,56,166-177,186,216 — persona contract + transcript labels + examples
6. sessionend_async.py:189 — affect label dict from config
7. candidates.py:33-35,351 — MEMES_ANCHOR_KEYS → function-internal resolve (not import-time freeze)
8. hooks.py:804,823,1029 — error messages + sticker nudge
9. daemon.py:118 — MCP tool docstring (runtime prompt surface)
10. transcript.py:34 — headless-spawn sentinel must track DIARY_PROMPT change

## Synapse-wx files (own [persona] in own config, no marrow import)

11. synapse_wx config — add [persona] section (user_name, assistant_name only)
12. providers/cc.py:80 — sticker system prompt
13. commands/messages.py:231 — ack string

## Cleanup

- eval-results/ → .gitignore + git rm --cached
- Comments with "Lumi" left as dev context (not runtime)

## Approach

- Prompt templates stay in code (product logic, not user config). Names become {placeholders}.
- render_*() functions per prompt module — normalize empty config, join aliases, one format site.
- candidates.py anchor_keys: default arg None → resolve from config inside function body.
- synapse-wx duplicates 2-3 persona strings in own config — cleaner than cross-repo coupling.

## Verify

- grep -rn for all persona terms in .py/.toml post-change — must be zero outside config/comments
- pytest marrow + pytest synapse-wx
- Manual: run daily.py --day with default persona → diary uses "User"/"Assistant"
