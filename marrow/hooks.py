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
# valence: Low ≤ -0.3 < Neu ≤ 0.3 < High
# intensity (arousal): Calm < 0.4 ≤ Intense
_VALENCE_LABEL = [(-0.3, "Low"), (0.3, "Neu"), (float("inf"), "High")]
_INTENSITY_LABEL = [(0.4, "Calm"), (float("inf"), "Intense")]

BACKDROP_MAX_CHARS = 350
SESSION_START_HARD_CAP = 6000


def _vlabel(v: float) -> str:
    for threshold, label in _VALENCE_LABEL:
        if v <= threshold:
            return label
    return "High"


def _ilabel(a: float) -> str:
    return "Calm" if a < 0.4 else "Intense"


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
    lines.append(f"② Now {cur['date']} ep{cur['ep']}: {cur_tag}{cur_label}")

    # ① recent past: top-N by importance (exclude the current row), last 7d
    past = [r for r in rows[1:] if r.get("source") != "pending"]
    past_top = sorted(past, key=lambda r: r["importance"], reverse=True)[:3]
    if past_top:
        segs = []
        for r in past_top:
            tag = f"{_vlabel(r['valence'])}/{_ilabel(r['arousal'])}"
            lbl = f"({r['label']})" if r.get("label") else ""
            segs.append(f"{r['date']} ep{r['ep']} {tag}{lbl}")
        lines.insert(0, "① Recent: " + " · ".join(segs))

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
        trend = "Stable" if wstd < 0.2 else ("Wavy" if wstd < 0.45 else "Stormy")
        wmean_tag = _vlabel(wmean)
        lines.append(f"③ 7d trend: {wmean_tag} / {trend} (σ={wstd:.2f})")

    # ④ emotional-pending: rows with source='pending', newest first
    pending = [r for r in rows if r.get("source") == "pending"]
    if pending:
        segs = []
        for r in pending[:2]:
            lbl = r.get("label") or f"ep{r['ep']}"
            segs.append(lbl)
        lines.append("④ Pending: " + " · ".join(segs))

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
        except PermissionError as e:
            # TCC-protected Desktop / unauthorized context: skip the full
            # re-render (lossless — next authorized session_end rewrites it).
            # Alert so the operator sees the TCC block instead of a silent
            # stale dashboard (DESIGN L33: every step writes alert on fail).
            repo.add_alert(
                "warn", "dashboard",
                f"session_end skipped dashboard write: {e}",
                source="hooks.py", db=db,
            )
        # Auto-embed events freshly archived this session so recall stays
        # current without a manual MCP call. Fail-soft: embedder absence or
        # any runtime error must never block session_end.
        try:
            from . import recall as recall_mod
            recall_mod.embed_pending(conn, batch=200)
        except Exception as e:
            repo.add_alert(
                "warn", "embed",
                f"session_end embed_pending failed: {e}",
                source="hooks.py", db=db,
            )
    finally:
        conn.close()
    return 0


def user_prompt_submit() -> int:
    """Inject top-K recall hits as UserPromptSubmit additionalContext.

    Config flag: [recall] vector = true (default on). Set false to disable.
    Fusion weights come from [recall] in config; recall.recall_fusion blends
    vec + bm25 + recency + affect. Fail-soft: any error falls through to a
    no-op so the user prompt always reaches the model.
    """
    inp = _read_input()
    cfg = config.load()
    if not cfg.get("recall", {}).get("vector", False):
        return 0

    prompt_text = (inp.get("prompt") or "").strip() if isinstance(inp, dict) else ""
    if not prompt_text:
        return 0

    rcfg = cfg.get("recall", {})
    try:
        from . import recall as recall_mod
        conn = storage.connect(config.db_path())
        try:
            hits = recall_mod.recall_fusion(
                conn, prompt_text,
                limit=int(rcfg.get("limit", 5)),
                budget_chars=int(rcfg.get("budget_chars", 2000)),
                w_vec=float(rcfg.get("w_vec", 0.55)),
                w_bm25=float(rcfg.get("w_bm25", 0.30)),
                w_recency=float(rcfg.get("w_recency", 0.15)),
                w_affect=float(rcfg.get("w_affect", 0.10)),
                min_score=float(rcfg.get("min_score", 0.35)),
            )
        finally:
            conn.close()
    except Exception:
        return 0  # fail-soft: never break the user turn

    if not hits:
        return 0

    lines = ["## Recall (auto)"]
    for h in hits:
        ts = (h.get("timestamp") or "")[:10]
        snippet = (h.get("content") or "").replace("\n", " ")[:300]
        lines.append(f"- [{ts}] {snippet}")
    ctx = "\n".join(lines)

    json.dump(
        {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }},
        sys.stdout,
    )
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
