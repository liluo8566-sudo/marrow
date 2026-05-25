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
    # 6AM-aligned: pre-6AM still belongs to prior day, like Affect Today.
    today = _day_cutoff_utc().astimezone(_TZ).date()
    return (today - timedelta(days=7)).isoformat()


def _rel_time(created_at: str) -> str:
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 3600:
            return f"{max(secs // 60, 1)}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
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
        "ELSE 2 END, created_at ASC"
    ).fetchall()
    lines = ["## Alerts (active)"]
    lines += [f"- {r[0]}: {r[1]}" for r in rows] if rows else ["_none_"]
    return "\n".join(lines)


def render_tasks(conn: sqlite3.Connection) -> str:
    # 6AM local day boundary — aligned with daily_catchup / sessionend_async.
    cutoff_utc = _day_cutoff_utc()
    cutoff_iso = cutoff_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    done = conn.execute(
        "SELECT id, category, title FROM tasks WHERE status='done' AND updated_at>=? "
        "ORDER BY updated_at ASC", (cutoff_iso,)).fetchall()

    today_local = cutoff_utc.astimezone(_TZ).date()
    next7 = today_local + timedelta(days=7)
    active = conn.execute(
        "SELECT id, category, title, due, created_at, next_step FROM tasks "
        "WHERE status='active' "
        "ORDER BY (due IS NULL), due, created_at").fetchall()

    buckets: dict[str, list] = {"t": [], "n": [], "l": []}
    for r in active:
        due = r[3]
        if due:
            try:
                d = datetime.fromisoformat(due[:10]).date()
                buckets["t" if d <= today_local else "n" if d <= next7 else "l"].append(r)
                continue
            except ValueError:
                pass
        buckets["l"].append(r)
    for k in buckets:
        # r[1] is category (r[0] is id)
        buckets[k].sort(key=lambda r: _tag_key(r[1:]))

    all_ids: list[int] = []

    def _row(r, date_str: str | None) -> str:
        # r = (id, category, title, due, created_at, next_step)
        all_ids.append(r[0])
        tag = r[1] or "Others"
        detail = f": {r[5]}" if r[5] else ""
        date_part = f" [{date_str}]" if date_str else ""
        return f"- [ ] [{tag}] {r[2]}{detail}{date_part} <!-- id:{r[0]} -->"

    out = [f"## Tasks", f"### Completed [{len(done)}]"]
    if done:
        for r in done:
            all_ids.append(r[0])
            out.append(f"- [x] [{r[1] or 'Others'}] {r[2]} <!-- id:{r[0]} -->")
    else:
        out.append("_none_")
    out.append(f"### To-Do List [{len(active)}]")
    l_due = [r for r in buckets["l"] if r[3]]
    l_nodue = [r for r in buckets["l"] if not r[3]]
    if not active:
        out.append("_none_")
    else:
        # Empty sub-buckets hide their heading entirely — no `(none)` placeholder.
        if buckets["t"]:
            out.append("Today")
            out += [_row(r, None) for r in buckets["t"]]
        if buckets["n"]:
            out.append("Next 7 Days")
            out += [_row(r, r[3][:10] if r[3] else None) for r in buckets["n"]]
        if l_due:
            out.append("Later")
            out += [_row(r, r[3][:10]) for r in l_due]
        if l_nodue:
            out.append("No date")
            out += [_row(r, r[4][:10] if r[4] else None) for r in l_nodue]
    # Trail marker — reconcile_tasks uses this to detect rows deleted from md.
    ids_str = ",".join(str(i) for i in all_ids)
    out.append(f"<!-- cand:task:ids=[{ids_str}] -->")
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
        "ORDER BY created_at ASC LIMIT ?", (n,)).fetchall()
    out = [f"## Milestone candidate [{len(rows)}]"]
    if rows:
        for r in rows:
            out.append(
                f"- [{r[1]}] {r[2]} ({_rel_time(r[3])})  "
                f"{_BUTTONS}  <!-- id:{r[0]} -->"
            )
    else:
        out.append("_none_")
    # Trail marker — reconcile compares this against ids surviving in md
    # so deleting a row in Obsidian == drop+tombstone (no vote needed).
    ids_csv = ",".join(str(r[0]) for r in rows)
    out.append(f"<!-- cand:milestone:ids=[{ids_csv}] -->")
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
    # Anchor everything to the latest sessionend batch's date — so after 6AM
    # rollover the prior day stays visible until the next sessionend writes.
    latest = conn.execute(
        "SELECT date, created_at FROM affect "
        "WHERE superseded_by IS NULL "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()

    out = ["## Affect", "### Today"]
    if not latest:
        out.append("_none_")
        out.append("### This Week")
        out.append("_none_")
        return "\n".join(out)
    last_date, last_ts = latest[0], latest[1]
    week_floor = (datetime.fromisoformat(last_date).date()
                  - timedelta(days=6)).isoformat()

    last_batch = [dict(r) for r in conn.execute(
        "SELECT id, valence, arousal, importance, label, description, ep "
        "FROM affect WHERE superseded_by IS NULL AND created_at=? "
        "ORDER BY ep", (last_ts,)).fetchall()]
    today_rows = [dict(r) for r in conn.execute(
        "SELECT id, valence, arousal, importance, label, description, ep "
        "FROM affect WHERE superseded_by IS NULL AND date=? "
        "ORDER BY ep", (last_date,)).fetchall()]
    week_rows = [dict(r) for r in conn.execute(
        "SELECT id, valence, arousal, importance, label, description, ep, "
        "date FROM affect WHERE superseded_by IS NULL AND date>=?",
        (week_floor,)).fetchall()]

    # Line 1 — last sessionend batch (fine label tone, batch max/min, ago tag).
    if last_batch:
        tone_row = max(last_batch, key=lambda r: (r["importance"], r["valence"]))
        last_tone = tone_row.get("label") or _tone(
            tone_row["valence"], tone_row["arousal"]
        )
        ep_h = max(last_batch, key=lambda r: r["valence"])
        ep_l = min(last_batch, key=lambda r: r["valence"])
        ago = _rel_time(last_ts)
        if ep_h["id"] == ep_l["id"]:
            out.append(f"- 【{last_tone}】 · {_ep_phrase(ep_h, 'h')} [{ago}]")
        else:
            out.append(
                f"- 【{last_tone}】 · {_ep_phrase(ep_h, 'h')} · "
                f"{_ep_phrase(ep_l, 'l')} [{ago}]"
            )

    # Line 2 — 24h (today, anchored to last_date).
    if today_rows:
        mv, ma = _wmean(today_rows, "valence"), _wmean(today_rows, "arousal")
        ep_h = max(today_rows, key=lambda r: (r["valence"], r["importance"]))
        ep_l = min(today_rows, key=lambda r: (r["valence"], -r["importance"]))
        tone = _tone(mv, ma)
        if ep_h["id"] == ep_l["id"]:
            out.append(f"- 【{tone}】 · {_ep_phrase(ep_h, 'h')} [24h]")
        else:
            out.append(
                f"- 【{tone}】 · {_ep_phrase(ep_h, 'h')} · "
                f"{_ep_phrase(ep_l, 'l')} [24h]"
            )

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
        out.append("_none_")

    pending_rows = conn.execute(
        "SELECT description, label, resolved_at FROM affect "
        "WHERE superseded_by IS NULL AND unresolved=1 AND date>=? "
        "ORDER BY created_at, id",
        (week_floor,),
    ).fetchall()
    # Pending sub-section hides entirely when empty (no heading, no body).
    if pending_rows:
        out.append("### Pending")
        for r in pending_rows:
            text = r["description"] or r["label"] or "(ep)"
            box = "x" if r["resolved_at"] else " "
            out.append(f"- [{box}] {text}")
    return "\n".join(out)


def render_content(conn: sqlite3.Connection,
                   *, dashboard_path: str | None = None) -> str:
    """Render the `## Content` section listing subpages with md links.

    Both top and bottom groups render as dot bullets — the `---` divider
    is the only thing separating content (top) from utility (bottom).
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
    for label, path in top:
        out.append(f"- [{label}]({_rel(path)})")
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
