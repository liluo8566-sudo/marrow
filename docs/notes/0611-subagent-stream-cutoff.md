2026-06-11

# Draft: Background subagent stream silently cuts off mid-task — runner reports "completed" with no end_turn

> STATUS: DRAFT — do not post. Evidence verified from live transcripts 2026-06-11.

---

## Title (proposed)

`[Bug] Background subagent stream silently cuts off mid-task — runner reports "completed" with no end_turn stop_reason`

---

## Environment

- **Claude Code version:** 2.1.170
- **OS:** macOS Darwin 25.5.0 (arm64)
- **Subagent model:** `claude-sonnet-4-5` (background, via the `Agent` tool / `claude -p` SDK mode)
- **Invocation:** background subagents dispatched from a main orchestrator session

---

## Symptom

Between approximately 23:48 and 00:37 AEST on 2026-06-10/11, **7 out of ~12 background subagents** died silently mid-task. Each agent was actively reading files and writing code when the stream stopped. The runner reported each task as **"completed"** with mid-task narration as the result string. No error was surfaced to the orchestrator.

---

## Evidence

### stop_reason distribution across 11 analysed transcripts

All transcripts live at:
`~/.claude/projects/-Users-Gabrielle-CC-Lab-marrow/<session-id>/subagents/agent-<id>.jsonl`

| Agent ID (truncated) | Lines | Last entry type | `end_turn` | `tool_use` | `None` | Notes |
|---|---|---|---|---|---|---|
| a013e29d | 43 | `user` (tool_result) | 0 | 8 | 16 | **dead** |
| a01945480 | 85 | `user` (tool_result) | 0 | 9 | 42 | **dead** |
| a39b3b81 | 122 | `user` (tool_result) | 0 | 17 | 53 | **dead** |
| a43aa5bc | 75 | `user` (tool_result) | 0 | 12 | 32 | **dead** |
| a959962a | 59 | `user` (tool_result) | 0 | 6 | 26 | **dead** |
| ab34e510 | 48 | `user` (tool_result) | 0 | 9 | 18 | **dead** |
| abdd4083 | 93 | `user` (tool_result) | 0 | 16 | 41 | **dead** |
| ac99c44e | 31 | `user` (tool_result) | 0 | 5 | 12 | **dead** |
| af8aefab | 30 | `user` (tool_result) | 0 | 6 | 11 | **dead** |
| a1d7c438 | 122 | `assistant` | **1** | 11 | 55 | survived (reached end_turn) |
| a2822a19 | 98 | `assistant` | 0 | 23 | 38 | completed normally (final report written) |

**Key observations:**
- Every dead agent: last transcript entry is a `user` message carrying a `tool_result`, followed by **no subsequent assistant message**.
- Every dead agent: `stop_reason` counts contain **zero `end_turn`** across all assistant messages.
- The surviving agent `a1d7c438` is the only one with `end_turn = 1` and appropriately ends on an `assistant` entry.
- All `meta.json` files for dead agents show `status=null, exit_code=null, result=""` — the runner has no record of failure.

### Sample transcript tail — dead agent `a013e29d` (representative)

```
[assistant] ts=2026-06-10T14:39:58.785Z  stop_reason=None
  text: "Now I have everything I need. Let me write the tests for Commit A (vec eviction):"

[assistant] ts=2026-06-10T14:39:59.430Z  stop_reason=tool_use
  (tool call: Read file)

[user]      ts=2026-06-10T14:39:59.481Z
  tool_result: <file contents returned successfully>

<< transcript ends — no assistant reply, no error >>
```

The pattern is identical across all 9 dead agents: assistant announces intent → issues a tool call → tool result arrives → stream stops.

### Timeline

All dead-agent last timestamps fall in the UTC range `13:50–14:39` (= AEST `23:50–00:39`), consistent with the 23:48–00:37 AEST window observed at the time.

---

## What we ruled out

- **Our own hooks:** Inspected all transcript rows for hook injection markers — 0 injections found in any dead transcript. PreToolUse / PostToolUse hooks were running in the session but did not fire on the cutoff turn.
- **Tool failure:** Every cutoff occurs *after* a successful `tool_result` is delivered to the agent. The tool itself returned valid output.
- **Context-length limit:** The dead agents ranged from 30 to 122 lines (small to medium transcripts). The 122-line agent with `end_turn` survived; shorter ones died. No correlation with transcript length.
- **Task content:** Dead agents were doing routine read/write coding tasks — no anomalous tool types, no external network calls.

---

## Expected behaviour

After a `tool_result` is delivered, the subagent should resume and produce the next `assistant` message, continuing until natural `end_turn` or a real error. The runner should report failure if the stream terminates abnormally.

## Actual behaviour

The stream terminates silently after the `tool_result`. No next `assistant` message is written. The runner reports the task status as `completed` (or null) with whatever mid-task text the agent had produced as the result, giving the orchestrator a false success signal.

---

## Workaround

**Commit-per-step + resume-dispatch:** structuring agents to commit after each logical unit of work, then manually resuming killed agents from the last committed checkpoint. This recovers forward progress but does not prevent the cutoff.

---

## Reproduction notes

- Occurred across a burst of ~12 parallel/sequential background subagent dispatches in a single main session.
- All agents used model `claude-sonnet-4-5`.
- CC version 2.1.170, no version change between surviving and dead agents.
- Could not reproduce on demand; the cutoffs appear intermittent and time-correlated (all within a ~50-minute window).

---

## Additional context

- The main orchestrator session itself remained healthy throughout.
- No API error codes or rate-limit messages appear anywhere in the transcripts.
- The `meta.json` runner state (`status=null, exit_code=null`) suggests the runner loop itself exited cleanly, not via an exception path.

---

*Draft prepared 2026-06-11. Evidence sourced from live transcripts at `~/.claude/projects/-Users-Gabrielle-CC-Lab-marrow/b45a9959-c3e5-4378-8ee5-e3153574dd13/subagents/`. Personal file paths and project content redacted beyond what is necessary to characterise the bug.*
