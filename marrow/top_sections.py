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
        "SELECT id, severity, message FROM alerts WHERE resolved = 0 "
        "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 "
        "ELSE 2 END, created_at ASC"
    ).fetchall()
    lines = ["## Alerts"]
    # Each row carries `<!-- id:alert.N -->` so reconcile_alerts can map a
    # deleted bullet back to the row to resolve. Lumi's md-side delete IS
    # the resolve gesture.
    lines += (
        [f"- {r[1]}: {r[2]} <!-- id:alert.{r[0]} -->" for r in rows]
        if rows else ["_none_"]
    )
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


def _affect_anchor_inline(ids: list[int]) -> str:
    """Inline end-of-line anchor for an affect bullet, ids left-to-right.
    Parity with task `<!-- id:N -->`: one space before the comment so it
    glues to the visible body and reconcile_affect can read it from the
    same line.
    """
    return f"<!-- aff:{','.join(str(i) for i in ids)} -->"


def _affect_pending_anchor(row: dict) -> str:
    """Per-row anchor for Pending bullets (single-record lines)."""
    return f"<!-- id:affect.{row['id']} -->"


def _ep_side(row: dict, baseline: float = 0.5) -> str:
    """Resolve eph/epl side from valence relative to a baseline (default 0.5)."""
    return "h" if row["valence"] >= baseline else "l"


def render_affect(conn: sqlite3.Connection) -> str:
    # Line 1 = latest sessionend batch (event-anchored). Lines 2/3 = rolling
    # time windows from now (24h / 7d). Headers stay stable; bodies emit
    # `_none_` when empty so structure is constant.
    latest = conn.execute(
        "SELECT created_at FROM affect "
        "WHERE superseded_by IS NULL "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()

    now = datetime.now(timezone.utc)
    day_cut = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    week_cut = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    out: list[str] = ["## Affect", "### Today"]

    last_batch: list[dict] = []
    last_ts: str | None = None
    if latest:
        last_ts = latest[0]
        last_batch = [dict(r) for r in conn.execute(
            "SELECT id, valence, arousal, importance, label, description, ep "
            "FROM affect WHERE superseded_by IS NULL AND created_at=? "
            "ORDER BY ep", (last_ts,)).fetchall()]
    today_rows = [dict(r) for r in conn.execute(
        "SELECT id, valence, arousal, importance, label, description, ep "
        "FROM affect WHERE superseded_by IS NULL AND created_at>=? "
        "ORDER BY created_at, ep", (day_cut,)).fetchall()]
    week_rows = [dict(r) for r in conn.execute(
        "SELECT id, valence, arousal, importance, label, description, ep, "
        "created_at FROM affect WHERE superseded_by IS NULL "
        "AND created_at>=?", (week_cut,)).fetchall()]

    line1_ids: set[int] = set()
    line2_ids: set[int] = set()

    # Line 1 — last sessionend batch (event-anchored). Single-ep batch uses
    # eph/epl based on valence sign, not a forced 'h'.
    if last_batch:
        tone_row = max(last_batch, key=lambda r: (r["importance"], r["valence"]))
        last_tone = tone_row.get("label") or _tone(
            tone_row["valence"], tone_row["arousal"]
        )
        ago = _rel_time(last_ts) if last_ts else "?"
        if len(last_batch) == 1:
            only = last_batch[0]
            segs = [_ep_phrase(only, _ep_side(only))]
            ids = [only["id"]]
        else:
            ep_h = max(last_batch, key=lambda r: r["valence"])
            ep_l = min(last_batch, key=lambda r: r["valence"])
            segs = [_ep_phrase(ep_h, 'h')]
            ids = [ep_h["id"]]
            if ep_l["id"] != ep_h["id"]:
                segs.append(_ep_phrase(ep_l, 'l'))
                ids.append(ep_l["id"])
        body = " · ".join(segs)
        out.append(
            f"- 【{last_tone}】 · {body} [{ago}] {_affect_anchor_inline(ids)}"
        )
        line1_ids.update(ids)

    # Line 2 — rolling 24h aggregate, deduped against line 1.
    today_pool = [r for r in today_rows if r["id"] not in line1_ids]
    if today_pool:
        mv, ma = _wmean(today_pool, "valence"), _wmean(today_pool, "arousal")
        tone = _tone(mv, ma)
        if len(today_pool) == 1:
            only = today_pool[0]
            segs = [_ep_phrase(only, _ep_side(only))]
            ids = [only["id"]]
        else:
            ep_h = max(today_pool, key=lambda r: (r["valence"], r["importance"]))
            ep_l = min(today_pool, key=lambda r: (r["valence"], -r["importance"]))
            segs = [_ep_phrase(ep_h, 'h')]
            ids = [ep_h["id"]]
            if ep_l["id"] != ep_h["id"]:
                segs.append(_ep_phrase(ep_l, 'l'))
                ids.append(ep_l["id"])
        body = " · ".join(segs)
        out.append(
            f"- 【{tone}】 · {body} [24h] {_affect_anchor_inline(ids)}"
        )
        line2_ids.update(ids)
    elif not last_batch:
        out.append("_none_")

    out.append("### This Week")
    week_pool = [r for r in week_rows
                 if r["id"] not in line1_ids and r["id"] not in line2_ids]
    if week_pool:
        mv, ma = _wmean(week_pool, "valence"), _wmean(week_pool, "arousal")
        simple_mean = sum(r["valence"] for r in week_pool) / len(week_pool)
        std_v = math.sqrt(
            sum((r["valence"] - simple_mean) ** 2 for r in week_pool)
            / len(week_pool)
        )
        if std_v > 0.3:
            srt = sorted(week_pool,
                         key=lambda r: (r["created_at"], r.get("ep", 0)))
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
        srt = sorted(week_pool, key=lambda r: r["valence"], reverse=True)
        n = len(srt)
        if n == 1:
            picked = [(srt[0], _ep_side(srt[0], simple_mean))]
        elif n == 2:
            picked = [(srt[0], 'h'), (srt[1], 'l')]
        elif n == 3:
            picked = [(srt[0], 'h'),
                      (srt[1], _ep_side(srt[1], simple_mean)),
                      (srt[2], 'l')]
        else:
            picked = [(srt[0], 'h'), (srt[1], 'h'),
                      (srt[-2], 'l'), (srt[-1], 'l')]
        segs = [_ep_phrase(r, side) for r, side in picked]
        ids = [r["id"] for r, _ in picked]
        out.append(
            f"- 【{tone_label}】 · " + " · ".join(segs) + " [7d] "
            f"{_affect_anchor_inline(ids)}"
        )
    else:
        out.append("_none_")

    pending_rows = conn.execute(
        "SELECT id, description, label, resolved_at FROM affect "
        "WHERE superseded_by IS NULL AND unresolved=1 "
        "AND resolved_at IS NULL AND created_at>=? "
        "ORDER BY created_at, id", (week_cut,),
    ).fetchall()
    # Pending sub-section hides entirely when empty (no heading, no body).
    if pending_rows:
        out.append("")
        out.append("### Pending")
        for r in pending_rows:
            text = r["description"] or r["label"] or "(ep)"
            box = "x" if r["resolved_at"] else " "
            out.append(f"- [{box}] {text} {_affect_pending_anchor(dict(r))}")
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


# ── inserter API (Phase 3 md=SoT) ─────────────────────────────────────────────
# Canonical block ids used by the dashboard inserter. Stable across renders so
# md_index can track per-block content_hash. Trailing comment on each `## H2`
# heading line — picked up by md_index.parse_blocks as the block boundary.

DASHBOARD_BLOCK_IDS = (
    "dashboard.alerts",
    "dashboard.tasks",
    "dashboard.milestone_cand",
    "dashboard.affect",
    "dashboard.content",
)

# Blocks whose user edits are absorbed into the DB by a reconcile pass before
# render — safe to overwrite the block body with fresh DB-driven content.
# alerts: reconcile_alerts treats a deleted bullet as `resolved=1`.
RECONCILED_BLOCK_IDS = frozenset({
    "dashboard.alerts",
    "dashboard.tasks",
    "dashboard.milestone_cand",
    "dashboard.affect",
})


def _stamp_block_id(body: str, block_id: str) -> str:
    """Prepend the id marker on its own line just above the first `## `
    heading. Marker on own line keeps Obsidian's live preview clean — the
    HTML comment hides while the H2 reads normally. Idempotent.
    """
    marker = f"<!-- id:{block_id} -->"
    if marker in body:
        return body
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            lines.insert(i, marker)
            return "\n".join(lines)
    # No H2 heading — prepend marker on its own line (defensive).
    return f"{marker}\n{body}"


def iter_top_blocks(conn: sqlite3.Connection,
                    *, dashboard_path: str | None = None
                    ) -> list[tuple[str, str]]:
    """Yield (block_id, body) for each canonical dashboard top section.

    body carries the `<!-- id:<block_id> -->` marker on its H2 line so the
    inserter and md_index see the same block boundary.
    """
    pairs = [
        ("dashboard.alerts", render_alerts(conn)),
        ("dashboard.tasks", render_tasks(conn)),
        ("dashboard.milestone_cand", render_milestone_candidate(conn)),
        ("dashboard.affect", render_affect(conn)),
        ("dashboard.content",
         render_content(conn, dashboard_path=dashboard_path)),
    ]
    return [(bid, _stamp_block_id(body, bid)) for bid, body in pairs]
