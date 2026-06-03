> Coding-task rules. For coding, scripts and config files.
> claude -p, Agent SDK, cc gh actions, and third-party Agent-SDK apps will use credit pool from 6/15; Anthropic API key is not an option; stream-json subprocess ✅

<planning>
- I'll tell you what I want (goal and intended outcome), you need to think about how to make it.
- Read, think and plan first — make sure we both understand and agree with the plan.
- If prompt or goal is vague, apply first-principles - figure out the underlying need.
- Match the plan with the goals before start.
- Propose a concrete solution with reasoning, not a menu. If a real fork exists, name the options inline and recommend the best one.
- Prefer code or config over prompt — code is deterministic; instructions and memory are fallback.
- Don't call AskUserQuestion tool in the middle of discussion. Use for final confirmation or during execution.
- Always consider multi-channel and migration cost at plan time — name vendor lock points and a concrete Codex swap path or OSS source.
</planning>

<execution>
- Always focus on the main goals. Stay on the right track.
- English ONLY. 
- Minimum comments. No docstrings beyond one line.
- Module soft cap 300 lines.
- Self-review and cut for over-engineering after every 50 LOC.
- Implement only what was asked - except essential gaps and safety nets.
    - Make sure you add standard safety nets proactively — concurrent-writer locks, retry caps, catchup idempotency, atomic writes on critical files, I/O error boundaries, alerting on silent failure, security guards.
    - Surface missing essentials proactively - obvious feature, guard, or pattern.
    - Future improvements/recommendations are welcome in the delivery report.
- Prioritise effect and outcome, then minimum diff on edits. 
    - Delete the whole section if the approach was not working - do not polish a wrong foundation.
    - Figure out the root cause for all issues. No surface fix! Min diff not apply to bugs!
- Execute an agreed plan end-to-end in one continuous pass.
    - No need to stop in the middle, ship altogether.
    - In-scope harmless follow-ups are permitted automatically (cleanup, renaming, dead-code removal, typo fixes, minor consistency)
    - Pause only on destructive operations or scope expansion beyond the agreed plan.
- Run tests, linter, and build before reporting done. Fix every failure first.
- Wait for the third caller before extracting an abstraction. Repetition beats premature abstraction.
- Delete cleanly: no rename-to-unused, no removal tombstone, no re-export shim.
</execution>

<verifying>
- Always verify and show me evidence before any statement. No guessing, no fabrication, no assumption.
    - For all questions/issues/bugs, any plausible explanation need to be proved. Check logs if available
    - If you think something is not working or too hard to apply, prove it with attemps and evidence. I won't aceept your assumption or lazy alternatives until I know the root cause.
- Before overturning your own conclusion, stop and audit the prior ones - what's wrong and why you turn? Do not jump into a new theroy without verification.
No jumping straight to a new theory.
- UI / frontend changes: launch the dev server and exercise the change in-browser — golden path, edges, regression. If the UI cannot be tested in this environment, say so explicitly; never claim untested success.
- Scripts with side effects (deploy / migration / pipeline run / file rewrite) — run with `--dry-run` first to preview the intended output, then `--apply` once the preview matches expectations.
- Validate at boundaries only — user input, external APIs. Trust internal code and framework guarantees elsewhere.
</verifying>

<git>
- One logical unit per commit. Commit autonomously at every logical unit.
- Push at the end of the session/phase. No confirm needed unless destructive.
- `~/.claude`: local commit after config changes (e.g. setting, hooks); no push needed.
- Never bypass hooks, signing, or pre-commit checks unless explicitly told.
</git>

<tool>
- Hook stdout injection caps ~10000 chars
- Run skill "tdd" when suitable - Deterministic logic with a fixed behavior contract. Not for LLM output quality, daemon / MCP glue.
- Run skill "diagnose" for heavy bugs.
- GitHub operations: gh CLI over WebFetch or hand-rolled cURL.
- OSS used or borrowed: star on GitHub, then sort into the matching list.
</tool>

<lessons>
Will add new lessons to diagnose skill
</lessons>
