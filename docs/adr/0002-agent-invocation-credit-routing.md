# Agent invocation bills by process type, not trigger source

`claude -p` (incl. Agent SDK, gh actions) → $100 credit pool. Interactive cc / stream-json subprocess / cc-native routine → OAuth subscription. Trigger (hook, plist, manual) does not change which pool. Provider chain itself: DESIGN.md "LLM provider abstraction" — not restated here.

- WeChat clawbot → stream-json on subscription (`-p` pool cannot hold Opus chat volume).
- Marrow → no dedicated paid agent: events run in-session/daemon (subscription), scheduled via cc-native routine (subscription), headless batch via `-p` (pool).
- ny-memm → out of scope, retires at Phase 1 parallel-window end.

## Rejected

- WeChat on cc-native routine — routine is cron-shaped, clawbot is event-driven.
- WeChat rate-limit / peak downgrade — measured usage <60%/wk; reinstate only on new usage evidence.
