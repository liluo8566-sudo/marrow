"""Shared sync renderers for the 4 dashboard/handover top sections.

All functions are pure (no I/O); accept a sqlite3.Connection; return markdown.
Imported by dashboard.py and handover_render.py — no duplication.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ── day boundary ─────────────────────────────────────────────────────────────
# Current cutoff 5AM local (diary.py _CUTOFF_H=4 + TZ logic → local 5AM net).
# CUTOFF_MIGRATION: change _DAY_CUTOFF_H to 6 when 2.5c diary.py migrates.
_TZ = ZoneInfo("Australia/Melbourne")
_DAY_CUTOFF_H = 5

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
    if not l_due and not l_nodue:
        out.append("- (none)")
    else:
        if l_due:
            out += [_row(r, r[2][:10]) for r in l_due]
        if l_due and l_nodue:
            out.append("---")
        if l_nodue:
            out += [_row(r, r[3][:10] if r[3] else None) for r in l_nodue]
    return "\n".join(out)


def render_milestone_candidate(conn: sqlite3.Connection, n: int = 5) -> str:
    rows = conn.execute(
        "SELECT date, title, created_at FROM milestones WHERE pinned=0 "
        "ORDER BY created_at DESC LIMIT ?", (n,)).fetchall()
    out = [f"## Milestone candidate [{len(rows)}]"]
    out += [f"- [{r[0]}] {r[1]} ({_rel_time(r[2])})" for r in rows] if rows else ["- (none)"]
    return "\n".join(out)


def render_affect(conn: sqlite3.Connection) -> str:
    cutoff_utc = _day_cutoff_utc()
    cutoff_iso = cutoff_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    today_rows = [dict(r) for r in conn.execute(
        "SELECT valence, arousal, importance, label, ep FROM affect "
        "WHERE superseded_by IS NULL AND date>=? AND created_at>=? ORDER BY ep",
        (cutoff_utc.date().isoformat(), cutoff_iso)).fetchall()]

    week_rows = [dict(r) for r in conn.execute(
        "SELECT valence, arousal, importance, label, ep, date FROM affect "
        "WHERE superseded_by IS NULL AND date>=?", (_week_iso(),)).fetchall()]

    out = ["## Affect", "### Today"]
    if today_rows:
        mv, ma = _wmean(today_rows, "valence"), _wmean(today_rows, "arousal")
        ep_h = max(today_rows, key=lambda r: (r["valence"], r["importance"]))
        ep_l = min(today_rows, key=lambda r: (r["valence"], -r["importance"]))
        lh = ep_h.get("label") or f"ep{ep_h['ep']}"
        ll = ep_l.get("label") or f"ep{ep_l['ep']}"
        out.append(f"- [{_tone(mv, ma)}] · ep{ep_h['importance']}h {lh} | {lh} · ep{ep_l['importance']}l {ll} | {ll}")
    else:
        out.append("- (none)")

    out.append("### This Week")
    if week_rows:
        mv, ma = _wmean(week_rows, "valence"), _wmean(week_rows, "arousal")
        simple_mean = sum(r["valence"] for r in week_rows) / len(week_rows)
        std_v = math.sqrt(sum((r["valence"] - simple_mean) ** 2 for r in week_rows) / len(week_rows))
        if std_v > 0.3:
            srt = sorted(week_rows, key=lambda r: (r["date"], r.get("ep", 0)))
            mid = len(srt) // 2
            def ht(rs: list[dict]) -> str:
                ws = sum(r["importance"] for r in rs)
                mv2 = sum(r["valence"] * r["importance"] for r in rs) / ws if ws else 0.5
                ma2 = sum(r["arousal"] * r["importance"] for r in rs) / ws if ws else 0.5
                return _tone(mv2, ma2)
            tone_label = f"{ht(srt[:mid] or srt)} → {ht(srt[mid:] or srt)}"
        else:
            tone_label = _tone(mv, ma)
        outliers = sorted(week_rows, key=lambda r: (-abs(r["valence"] - simple_mean), -r["importance"]))[:4]
        keys = " · ".join(r.get("label") or f"ep{r.get('ep','?')}" for r in outliers)
        out.append(f"- [{tone_label}] · {keys}")
    else:
        out.append("- (none)")

    out.append("### Pending")
    # Pending body renders empty until affect.unresolved column lands in 2.5c (P4).
    out.append("- (none)")
    return "\n".join(out)


def render_top(conn: sqlite3.Connection) -> str:
    """Concatenate all 4 sections (Alerts/Tasks/Milestone/Affect)."""
    return "\n\n".join([
        render_alerts(conn),
        render_tasks(conn),
        render_milestone_candidate(conn),
        render_affect(conn),
    ])
