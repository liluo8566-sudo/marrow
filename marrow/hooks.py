"""Thin CC hook entrypoints. `python -m marrow.hooks <event>`.

Code-only, no LLM. Parallel-safe with the legacy ny-memm hooks —
marrow registers ALONGSIDE them, never replaces. Logic lives in the marrow
package; this only does hook I/O (stdin JSON in, stdout JSON for
SessionStart additionalContext, side effects for SessionEnd).

  session_start      -> inject open threads + alerts + affect backdrop
  session_end        -> clean transcript, archive events, regen dashboard top
  user_prompt_submit -> deterministic vector recall fallback (scaffold; default off)

PreToolUse is the global prompt-guard.py (scope already covers
~/cc-lab/marrow/), not duplicated here.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from . import config, dashboard, repo, storage, transcript

# ── affect label lookup ──────────────────────────────────────────────────────
# valence: 沉 < -0.3 ≤ 暖 ≤ 0.3 < 亮
# intensity (arousal): 轻 < 0.4 ≤ 重
_VALENCE_LABEL = [(-0.3, "沉"), (0.3, "暖"), (float("inf"), "亮")]
_INTENSITY_LABEL = [(0.4, "轻"), (float("inf"), "重")]

BACKDROP_MAX_CHARS = 350
SESSION_START_HARD_CAP = 6000


def _vlabel(v: float) -> str:
    for threshold, label in _VALENCE_LABEL:
        if v <= threshold:
            return label
    return "亮"


def _ilabel(a: float) -> str:
    return "轻" if a < 0.4 else "重"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── affect heartbeat ─────────────────────────────────────────────────────────

def _affect_heartbeat(conn: sqlite3.Connection) -> str | None:
    """Return block line if a day in last 7d had events but no affect, else None.

    DECISIONS line 37: fires ONLY on a day that HAD events but NO affect.
    Checks the past 7 calendar days (UTC date boundary).
    """
    today = _now_utc().date()
    gap_day: str | None = None
    for delta in range(1, 8):
        d = (today - timedelta(days=delta)).isoformat()
        has_events = conn.execute(
            "SELECT 1 FROM events WHERE date(timestamp) = ? LIMIT 1", (d,)
        ).fetchone()
        if not has_events:
            continue
        has_affect = conn.execute(
            "SELECT 1 FROM affect_live WHERE date = ? LIMIT 1", (d,)
        ).fetchone()
        if not has_affect:
            gap_day = d
            break  # report the most recent gap only
    if gap_day:
        return f"[⚠ (情感记录可能中断): {gap_day}]"
    return None


# ── affect backdrop ──────────────────────────────────────────────────────────

def _affect_backdrop(conn: sqlite3.Connection) -> str:
    """Build the 4-element affect backdrop. No LLM. ≤5 lines ≤350 chars.

    ① recent past episodes' emotion summary (top by importance, last 7d)
    ② current emotion (single most-recent row)
    ③ calm-vs-swing trend (≤7d weighted, 1 line)
    ④ emotional-pending (unresolved-between-us; source='pending')
    """
    today = _now_utc().date()
    cutoff = (today - timedelta(days=7)).isoformat()

    # All live affect from last 7 days, newest first.
    rows = conn.execute(
        "SELECT date, ep, valence, arousal, importance, label, source "
        "FROM affect_live "
        "WHERE date >= ? "
        "ORDER BY date DESC, ep DESC",
        (cutoff,),
    ).fetchall()

    if not rows:
        return ""

    # Convert sqlite3.Row to dicts for .get() support
    rows = [dict(r) for r in rows]

    lines: list[str] = []

    # ② current emotion = most-recent row
    cur = rows[0]
    cur_tag = f"{_vlabel(cur['valence'])}/{_ilabel(cur['arousal'])}"
    cur_label = f" ({cur['label']})" if cur.get("label") else ""
    lines.append(f"② 当前 {cur['date']} ep{cur['ep']}: {cur_tag}{cur_label}")

    # ① recent past: top-N by importance (exclude the current row), last 7d
    past = [r for r in rows[1:] if r.get("source") != "pending"]
    past_top = sorted(past, key=lambda r: r["importance"], reverse=True)[:3]
    if past_top:
        segs = []
        for r in past_top:
            tag = f"{_vlabel(r['valence'])}/{_ilabel(r['arousal'])}"
            lbl = f"({r['label']})" if r.get("label") else ""
            segs.append(f"{r['date']} ep{r['ep']} {tag}{lbl}")
        lines.insert(0, "① 近期: " + " · ".join(segs))

    # ③ calm-vs-swing: exponential weighted std of valence over last 7d
    # weight = exp(-days_ago/3)
    scored = []
    for r in rows:
        if r.get("source") == "pending":
            continue
        days_ago = (today - datetime.fromisoformat(r["date"]).date()).days
        w = math.exp(-days_ago / 3.0)
        scored.append((r["valence"], w))
    if len(scored) >= 2:
        wsum = sum(w for _, w in scored)
        wmean = sum(v * w for v, w in scored) / wsum
        wvar = sum(w * (v - wmean) ** 2 for v, w in scored) / wsum
        wstd = math.sqrt(wvar)
        trend = "情绪平稳" if wstd < 0.2 else ("情绪波动" if wstd < 0.45 else "情绪剧烈波动")
        wmean_tag = _vlabel(wmean)
        lines.append(f"③ 7d趋势: {wmean_tag}调/{trend} (σ={wstd:.2f})")

    # ④ emotional-pending: rows with source='pending', newest first
    pending = [r for r in rows if r.get("source") == "pending"]
    if pending:
        segs = []
        for r in pending[:2]:
            lbl = r.get("label") or f"ep{r['ep']}"
            segs.append(lbl)
        lines.append("④ 情感悬挂: " + " · ".join(segs))

    # Enforce ≤5 lines, ≤350 chars
    result = "\n".join(lines[:5])
    if len(result) > BACKDROP_MAX_CHARS:
        result = result[: BACKDROP_MAX_CHARS - 1] + "…"
    return result


# ── session-start payload ────────────────────────────────────────────────────

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
        parts: list[str] = []

        # Heartbeat block goes first so it is never buried.
        heartbeat = _affect_heartbeat(conn)
        if heartbeat:
            parts.append(heartbeat)

        parts.append(_handoff_text(conn))

        backdrop = _affect_backdrop(conn)
        if backdrop:
            parts.append("## Affect\n" + backdrop)

        ctx = "\n\n".join(p for p in parts if p)

        # Hard cap: never exceed 6000 chars total for SessionStart.
        if len(ctx) > SESSION_START_HARD_CAP:
            ctx = ctx[: SESSION_START_HARD_CAP - 1] + "…"
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


def user_prompt_submit() -> int:
    """Deterministic vector recall fallback — SCAFFOLD only, default off.

    Config flag: [recall] vector = true  (config.default.toml, default false)

    When enabled this hook fires on every user turn and injects top-K recall
    hits as additionalContext via the UserPromptSubmit hook protocol.

    TODO: wire to recall.py embedder after worktree C (recall module) merges.
          Call site: embed(prompt_text) -> query_vec -> recall.vector_search(query_vec)
          The embedding fn and recall.vector_search are NOT available yet.
    """
    inp = _read_input()
    cfg = config.load()
    if not cfg.get("recall", {}).get("vector", False):
        # Vector recall disabled (default). No-op — return without output.
        return 0

    # Scaffold: retrieve the user prompt text from the CC hook payload.
    prompt_text = ""
    try:
        # CC UserPromptSubmit payload: {"prompt": "...", "session_id": "..."}
        prompt_text = inp.get("prompt", "")
    except Exception:
        pass

    if not prompt_text:
        return 0

    # TODO: replace this stub with the real call once worktree C merges.
    #   from . import recall as recall_mod
    #   hits = recall_mod.vector_search(prompt_text, limit=5)
    #   ctx = "\n".join(h["content"] for h in hits)
    #   json.dump({"hookSpecificOutput": {
    #       "hookEventName": "UserPromptSubmit",
    #       "additionalContext": ctx,
    #   }}, sys.stdout)
    return 0


_EVENTS = {
    "session_start": session_start,
    "session_end": session_end,
    "user_prompt_submit": user_prompt_submit,
}


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
