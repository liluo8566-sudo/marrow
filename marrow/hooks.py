"""Thin CC hook entrypoints. `python -m marrow.hooks <event>`.

Phase 1, code-only, no LLM. Parallel-safe with the legacy ny-memm hooks —
marrow registers ALONGSIDE them, never replaces. Logic lives in the marrow
package; this only does hook I/O (stdin JSON in, stdout JSON for
SessionStart additionalContext, side effects for SessionEnd).

  session_start  -> inject open threads + alerts as additionalContext
  session_end    -> clean transcript, archive events, regen dashboard top

UserPromptSubmit must-never-fade has no Phase-1 content source (the
convention-injection layer is DESIGN Pending); no hook is wired for it
until that lands. PreToolUse is the global prompt-guard.py (scope already
covers ~/cc-lab/marrow/), not duplicated here.
"""
from __future__ import annotations

import json
import sys

from . import config, dashboard, repo, storage, transcript


def _read_input() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def _handoff_text(conn) -> str:
    h = repo.handoff(conn)
    lines = ["# Marrow handoff", "", "## Open Threads"]
    if h["threads"]:
        for t in h["threads"]:
            due = f" [Due {t['due']}]" if t.get("due") else ""
            nxt = f" — {t['next_step']}" if t.get("next_step") else ""
            lines.append(f"- [{t['category']}] {t['title']}{nxt}{due} #{t['id']}")
    else:
        lines.append("- none")
    lines += ["", "## Alerts"]
    if h["alerts"]:
        for a in h["alerts"]:
            lines.append(f"- #{a['id']} [{a['severity']}] {a['message']}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def session_start() -> int:
    _read_input()
    db = config.db_path()
    conn = storage.connect(db)
    try:
        ctx = _handoff_text(conn)
    finally:
        conn.close()
    json.dump(
        {"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }},
        sys.stdout,
    )
    return 0


def session_end() -> int:
    inp = _read_input()
    tpath = inp.get("transcript_path")
    if not tpath:
        return 0
    if transcript.is_headless(tpath):
        return 0  # spawned claude -p fires SessionEnd too; not our session
    db = config.db_path()
    conn = storage.connect(db)
    try:
        rows = transcript.clean(tpath)
        if rows:
            repo.archive_events(conn, rows)
        state = str(config.DATA_DIR / "state")
        dash = inp.get("marrow_dashboard") or config.dashboard_path()
        try:
            dashboard.write_dashboard(dash, conn, state_dir=state, db=db)
        except PermissionError:
            pass  # TCC-protected Desktop / unauthorized context: skip this
            # full re-render (lossless — next authorized session_end rewrites
            # it). Sibling of alert#11's clean() FileNotFoundError no-op.
    finally:
        conn.close()
    return 0


_EVENTS = {"session_start": session_start, "session_end": session_end}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in _EVENTS:
        print(f"usage: python -m marrow.hooks {{{'|'.join(_EVENTS)}}}",
              file=sys.stderr)
        return 2
    try:
        return _EVENTS[args[0]]()
    except Exception as e:  # hook must never break the session
        try:
            repo.add_alert("warn", "hook", f"{args[0]} failed: {e}",
                           source="hooks.py", db=config.db_path())
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
