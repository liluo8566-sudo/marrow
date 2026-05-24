"""Shared sync renderers for the dashboard/handover top sections.

All functions are pure (no I/O except content list path resolution);
accept a sqlite3.Connection; return markdown. Imported by dashboard.py
and handover_render.py — no duplication.
"""
from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── day boundary ─────────────────────────────────────────────────────────────
# 6AM local day boundary — aligned with daily_catchup._CUTOFF_H and
# sessionend_async._CUTOFF_H.
_TZ = ZoneInfo("Australia/Melbourne")
_DAY_CUTOFF_H = 6

# ── 9-tone table: (V-band, A-band) → main tone ───────────────────────────────
# V band: <0.4 Low, 0.4–0.6 Neu, ≥0.6 High
# A band: <0.4 Calm, 0.4–0.6 Active, ≥0.6 Intense
_TONE = {
    ("Low",  "Calm"): "低落", ("Low",  "Active"): "烦躁", ("Low",  "Intense"): "痛苦",
    ("Neu",  "Calm"): "平淡", ("Neu",  "Active"): "专注", ("Neu",  "Intense"): "紧张",
    ("High", "Calm"): "温暖", ("High", "Active"): "愉悦", ("High", "Intense"): "兴奋",
}
_TAG_ORDER = ["Study", "Project", "Appointment", "Daily", "Others"]


def _vband(v: float) -> str:
    return "Low" if v < 0.4 else ("Neu" if v < 0.6 else "High")


def _aband(a: float) -> str:
    return "Calm" if a < 0.4 else ("Active" if a < 0.6 else "Intense")


def _tone(v: float, a: float) -> str:
    return _TONE[(_vband(v), _aband(a))]


def _wmean(rows: list[dict], key: str) -> float:
    ws = sum(r["importance"] for r in rows)
    return sum(r[key] * r["importance"] for r in rows) / ws if ws else 0.5


def _day_cutoff_utc() -> datetime:
    now_local = datetime.now(_TZ)
    today_local = now_local.date()
    cutoff = datetime(today_local.year, today_local.month, today_local.day,
                      _DAY_CUTOFF_H, 0, 0, tzinfo=_TZ)
    if now_local < cutoff:
        cutoff -= timedelta(days=1)
    return cutoff.astimezone(timezone.utc)


def _week_iso() -> str:
    return (datetime.now(_TZ).date() - timedelta(days=7)).isoformat()


def _rel_time(created_at: str) -> str:
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        h = int(delta.total_seconds() // 3600)
        return f"{h}h ago" if h < 24 else f"{delta.days}d ago"
    except Exception:
        return "?"


def _tag_key(row) -> int:
    cat = (row[0] or "Others").capitalize()
    return _TAG_ORDER.index(cat) if cat in _TAG_ORDER else len(_TAG_ORDER)


# ── section renderers ─────────────────────────────────────────────────────────

def render_alerts(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT severity, message FROM alerts WHERE resolved = 0 "
        "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 "
        "ELSE 2 END, created_at DESC"
    ).fetchall()
    lines = ["## Alerts (active)"]
    lines += [f"- {r[0]}: {r[1]}" for r in rows] if rows else ["- (none)"]
    return "\n".join(lines)


def render_tasks(conn: sqlite3.Connection) -> str:
    cutoff_iso = _day_cutoff_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    done = conn.execute(
        "SELECT category, title FROM tasks WHERE status='done' AND updated_at>=? "
        "ORDER BY updated_at DESC", (cutoff_iso,)).fetchall()

    today_local = datetime.now(_TZ).date()
    next7 = today_local + timedelta(days=7)
    active = conn.execute(
        "SELECT category, title, due, created_at, next_step FROM tasks "
        "WHERE status='active' "
        "ORDER BY (due IS NULL), due, created_at").fetchall()

    buckets: dict[str, list] = {"t": [], "n": [], "l": []}
    for r in active:
        due = r[2]
        if due:
            try:
                d = datetime.fromisoformat(due[:10]).date()
                buckets["t" if d <= today_local else "n" if d <= next7 else "l"].append(r)
                continue
            except ValueError:
                pass
        buckets["l"].append(r)
    for k in buckets:
        buckets[k].sort(key=_tag_key)

    def _row(r, date_str: str | None) -> str:
        tag = r[0] or "Others"
        detail = f": {r[4]}" if r[4] else ""
        date_part = f" [{date_str}]" if date_str else ""
        return f"- [ ] [{tag}] {r[1]}{detail}{date_part}"

    out = [f"## Tasks", f"### Completed [{len(done)}]"]
    out += [f"- [x] [{r[0] or 'Others'}] {r[1]}" for r in done] if done else ["- (none)"]
    out.append(f"### To-Do List [{len(active)}]")
    out.append("Today")
    out += [_row(r, None) for r in buckets["t"]] if buckets["t"] else ["- (none)"]
    out.append("Next 7 Days")
    if buckets["n"]:
        out += [_row(r, r[2][:10] if r[2] else None) for r in buckets["n"]]
    else:
        out.append("- (none)")
    out.append("Later")
    l_due = [r for r in buckets["l"] if r[2]]
    l_nodue = [r for r in buckets["l"] if not r[2]]
    if l_due:
        out += [_row(r, r[2][:10]) for r in l_due]
    elif not l_nodue:
        out.append("- (none)")
    out.append("No date")
    if l_nodue:
        out += [_row(r, r[3][:10] if r[3] else None) for r in l_nodue]
    else:
        out.append("- (none)")
    return "\n".join(out)


# Candidate anchor buttons (DESIGN L60). md shows visible chars; the HTML
# layer wires real clicks. Reconcile semantics (see reconcile.py):
#   ✅ <id>  → pin (move to subpage, scope-aware for milestone)
#   ❌ <id>  → delete + tombstone
#   ✏️ <id>  → edit-in-place
_BUTTONS = "✅ ❌ ✏️"


def render_milestone_candidate(conn: sqlite3.Connection, n: int = 5) -> str:
    rows = conn.execute(
        "SELECT id, date, title, created_at FROM milestones WHERE pinned=0 "
        "ORDER BY created_at DESC LIMIT ?", (n,)).fetchall()
    out = [f"## Milestone candidate [{len(rows)}]"]
    if rows:
        for r in rows:
            out.append(
                f"- [{r[1]}] {r[2]} ({_rel_time(r[3])})  "
                f"{_BUTTONS}  <!-- id:{r[0]} -->"
            )
    else:
        out.append("- (none)")
    return "\n".join(out)


def _ep_phrase(row: dict, side: str) -> str:
    """Format one ep as `ephN <label> | <description>` (or eplN).
    N = importance. label / description fall back to ep number / label.
    """
    n = row.get("importance") or 0
    label = row.get("label") or f"ep{row.get('ep', '?')}"
    desc = row.get("description") or label
    return f"ep{side}{n} {label} | {desc}"


def render_affect(conn: sqlite3.Connection) -> str:
    cutoff_utc = _day_cutoff_utc()
    cutoff_iso = cutoff_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    today_rows = [dict(r) for r in conn.execute(
        "SELECT id, valence, arousal, importance, label, description, ep "
        "FROM affect "
        "WHERE superseded_by IS NULL AND date>=? AND created_at>=? "
        "ORDER BY ep",
        (cutoff_utc.date().isoformat(), cutoff_iso)).fetchall()]

    week_iso = _week_iso()
    week_rows = [dict(r) for r in conn.execute(
        "SELECT id, valence, arousal, importance, label, description, ep, "
        "date FROM affect "
        "WHERE superseded_by IS NULL AND date>=?", (week_iso,)).fetchall()]

    out = ["## Affect", "### Today"]
    if today_rows:
        mv, ma = _wmean(today_rows, "valence"), _wmean(today_rows, "arousal")
        ep_h = max(today_rows, key=lambda r: (r["valence"], r["importance"]))
        ep_l = min(today_rows, key=lambda r: (r["valence"], -r["importance"]))
        tone = _tone(mv, ma)
        if ep_h["id"] == ep_l["id"]:
            # Single ep (or all eps share extreme V); dedup to one side.
            out.append(f"- 【{tone}】 · {_ep_phrase(ep_h, 'h')}")
        else:
            out.append(
                f"- 【{tone}】 · {_ep_phrase(ep_h, 'h')} · "
                f"{_ep_phrase(ep_l, 'l')}"
            )
    else:
        out.append("- (none)")

    out.append("### This Week")
    if week_rows:
        mv, ma = _wmean(week_rows, "valence"), _wmean(week_rows, "arousal")
        simple_mean = sum(r["valence"] for r in week_rows) / len(week_rows)
        std_v = math.sqrt(
            sum((r["valence"] - simple_mean) ** 2 for r in week_rows)
            / len(week_rows)
        )
        if std_v > 0.3:
            srt = sorted(week_rows, key=lambda r: (r["date"], r.get("ep", 0)))
            mid = len(srt) // 2

            def ht(rs: list[dict]) -> str:
                ws = sum(r["importance"] for r in rs)
                mv2 = (sum(r["valence"] * r["importance"] for r in rs) / ws
                       if ws else 0.5)
                ma2 = (sum(r["arousal"] * r["importance"] for r in rs) / ws
                       if ws else 0.5)
                return _tone(mv2, ma2)
            tone_label = f"{ht(srt[:mid] or srt)} → {ht(srt[mid:] or srt)}"
        else:
            tone_label = _tone(mv, ma)
        outliers = sorted(
            week_rows,
            key=lambda r: (-abs(r["valence"] - simple_mean), -r["importance"]),
        )[:4]
        parts = [
            _ep_phrase(r, "h" if r["valence"] >= simple_mean else "l")
            for r in outliers
        ]
        out.append(f"- 【{tone_label}】 · {' · '.join(parts)}")
    else:
        out.append("- (none)")

    out.append("### Pending")
    pending_rows = conn.execute(
        "SELECT description, label, resolved_at FROM affect "
        "WHERE superseded_by IS NULL AND unresolved=1 AND date>=? "
        "ORDER BY created_at, id",
        (week_iso,),
    ).fetchall()
    if pending_rows:
        for r in pending_rows:
            text = r["description"] or r["label"] or "(ep)"
            box = "x" if r["resolved_at"] else " "
            out.append(f"- [{box}] {text}")
    else:
        out.append("- (none)")
    return "\n".join(out)


def render_content(conn: sqlite3.Connection,
                   *, dashboard_path: str | None = None) -> str:
    """Render the `## Content` section listing subpages with md links.

    Top items numbered, then `---` divider, then bottom items unnumbered.
    Links are relative to the dashboard's own directory so Obsidian +
    plain-md readers both resolve them. Hidden keys excluded by
    content_list().
    """
    from . import subpages
    info = subpages.content_list()
    if dashboard_path:
        base = Path(dashboard_path).parent
    else:
        from . import config as _config
        base = Path(_config.dashboard_path()).parent

    def _rel(p: str) -> str:
        try:
            return os.path.relpath(p, start=str(base))
        except ValueError:
            return p

    out = ["## Content"]
    top = info["top"]
    bottom = info["bottom"]
    if not top and not bottom:
        out.append("- (none)")
        return "\n".join(out)
    for i, (label, path) in enumerate(top, start=1):
        out.append(f"{i}. [{label}]({_rel(path)})")
    if bottom:
        out.append("---")
        for label, path in bottom:
            out.append(f"- [{label}]({_rel(path)})")
    return "\n".join(out)


def render_top(conn: sqlite3.Connection,
               *, dashboard_path: str | None = None) -> str:
    """Concatenate all dashboard top sections.

    Order: Alerts → Tasks → Milestone candidate → Affect → Content.
    `## Content` lives just below Affect (DESIGN L60).
    """
    return "\n\n".join([
        render_alerts(conn),
        render_tasks(conn),
        render_milestone_candidate(conn),
        render_affect(conn),
        render_content(conn, dashboard_path=dashboard_path),
    ])
