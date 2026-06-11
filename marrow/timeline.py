"""Timeline block renderer — merged affect + session view.

Two outlets:
  render_timeline(conn) -> str   # SessionStart injection / dashboard block
  Both use the same render fn; caller decides where to put the output.

Format (FINAL spec, plan 4A-3):
  > 未解: <desc> [label]  — open affect episodes, 7d expiry, top of block
  Last 24h: flat HH:MM film-strip newest→oldest, cap 15
    - session's first line carries 【tone】
    - LIFE lines (casual) + TL line (task)
    - day crossings: --- MM-DD --- divider
  24-72h: per-day **MM-DD Day 【tone】** + AM/PM/ND period lines, cap ~12
    - ND 00-06 belongs to PREVIOUS day
    - all sessions' TLs in period joined time-order, truncated; empty hidden
  Day 4-7: Week 【tone ↗/↘/→】 trend + one line per day 【tone】 diary.tl_line
  No in-progress session line.
  Trim order: day lines → period lines (farthest day first) → 24h farthest.
  Budget ~1100 chars.

Tone labels reuse top_sections._tone / _vband / _aband (no duplication).
All DB timestamps are UTC; Melbourne on render via timeutil.
"""
from __future__ import annotations

import datetime as _dt
import re as _re
import sqlite3
from zoneinfo import ZoneInfo

from .top_sections import _tone, _vband, _aband, _wmean

_TZ = ZoneInfo("Australia/Melbourne")
# Matches leading HH:MM in a LIFE line (e.g. "21:40 买了b5精华")
_LIFE_TS_RE = _re.compile(r"^(\d{2}:\d{2})\s+(.*)", _re.DOTALL)
_CUTOFF_H = 6          # 6AM local day boundary
_BUDGET = 1100         # soft char budget
_24H_CAP = 15          # max film-strip lines
_2472H_CAP = 12        # max lines incl. headers for 24-72h zone
_OPEN_EXPIRY_DAYS = 7  # open episodes older than this are hidden
_TL_FALLBACK_CHARS = 60  # tl_line NULL → truncated body text


# ── helpers ─────────────────────────────────────────────────────────────────

def _now_melb() -> _dt.datetime:
    return _dt.datetime.now(_TZ)


def _day_start_utc(local_date: _dt.date) -> _dt.datetime:
    """Local date at 06:00 AM → UTC (the effective day boundary)."""
    local_midnight = _dt.datetime(
        local_date.year, local_date.month, local_date.day,
        _CUTOFF_H, 0, 0, tzinfo=_TZ)
    return local_midnight.astimezone(_dt.timezone.utc)


def _local_date_from_utc(utc_iso: str) -> _dt.date:
    """UTC ISO → Melbourne local date with 6AM cutoff."""
    s = (utc_iso or "").strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return _now_melb().date()
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    local = d.astimezone(_TZ)
    # Before 6AM → belongs to previous diary day
    if local.hour < _CUTOFF_H:
        local -= _dt.timedelta(days=1)
    return local.date()


def _hhmm_melb(utc_iso: str) -> str:
    """UTC ISO → Melbourne HH:MM display."""
    s = (utc_iso or "").strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return "??:??"
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_TZ).strftime("%H:%M")


def _period_of_hhmm(hhmm: str) -> str:
    """HH:MM → AM/PM/ND period label.
    AM 06-12, PM 12-18, ND 18-06 (next morning up to 06:00).
    00-06 display time belongs to ND of PREVIOUS diary day.
    """
    try:
        h = int(hhmm[:2])
    except (ValueError, IndexError):
        return "ND"
    if 6 <= h < 12:
        return "AM"
    if 12 <= h < 18:
        return "PM"
    return "ND"


def _period_diary_date(utc_iso: str) -> tuple[_dt.date, str]:
    """Return (diary_date, period) for a UTC timestamp.
    ND 00-05 belongs to PREVIOUS diary day (the ND period of the prior night).
    """
    s = (utc_iso or "").strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        today = _now_melb().date()
        return today, "ND"
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    local = d.astimezone(_TZ)
    h = local.hour
    period = _period_of_hhmm(local.strftime("%H:%M"))
    # 00-05 display → ND of PREVIOUS diary day
    if h < _CUTOFF_H:
        diary_date = (local - _dt.timedelta(days=1)).date()
    else:
        diary_date = local.date()
    return diary_date, period


def _tone_from_rows(rows: list[dict]) -> str:
    if not rows:
        return "平淡"
    v = _wmean(rows, "valence")
    a = _wmean(rows, "arousal")
    return _tone(v, a)


def _week_trend(this_week: list[dict], last_week: list[dict]) -> str:
    """↗/↘/→ based on V/A mean delta between this week and last week."""
    if not this_week:
        return "平淡"
    tw_v = _wmean(this_week, "valence")
    tw_a = _wmean(this_week, "arousal")
    tone_label = _tone(tw_v, tw_a)
    if not last_week:
        return tone_label
    lw_v = _wmean(last_week, "valence")
    delta_v = tw_v - lw_v
    if delta_v > 0.05:
        arrow = "↗"
    elif delta_v < -0.05:
        arrow = "↘"
    else:
        arrow = "→"
    return f"{tone_label} {arrow}"


def _tl_or_fallback(sd: dict) -> str:
    """tl_line if set; else truncate body to _TL_FALLBACK_CHARS."""
    tl = sd.get("tl_line")
    if tl:
        return tl
    body = (sd.get("text") or "").strip()
    if len(body) > _TL_FALLBACK_CHARS:
        return body[:_TL_FALLBACK_CHARS] + "…"
    return body


def _tl_anchor_sid(sid: str) -> str:
    return f"<!-- tl:{sid} -->"


def _tl_anchor_date(date: str) -> str:
    return f"<!-- tl:d:{date} -->"


def _life_line_hhmm(item: str, session_hhmm: str) -> tuple[str, str]:
    """Return (hhmm, text) for a LIFE line item.

    If the item starts with HH:MM (new format), use it as the display time.
    Otherwise fall back to session_hhmm (legacy rows without prefix).
    Returns (hhmm, display_text).
    """
    m = _LIFE_TS_RE.match(item)
    if m:
        return m.group(1), m.group(2).strip()
    return session_hhmm, item


def _life_line_local_date(item: str, session_date: _dt.date,
                          session_hhmm: str) -> _dt.date:
    """Resolve local diary date for a LIFE line.

    Lines with their own HH:MM: build a full datetime using that time on
    the session's local date, then apply 6AM cutoff (in case line is 00-05).
    Prefix-less lines inherit the session date directly.
    """
    m = _LIFE_TS_RE.match(item)
    if not m:
        return session_date
    hhmm = m.group(1)
    try:
        h, mi = int(hhmm[:2]), int(hhmm[3:5])
    except ValueError:
        return session_date
    # Build candidate datetime on the session's calendar date
    candidate = _dt.datetime(
        session_date.year, session_date.month, session_date.day,
        h, mi, 0, tzinfo=_TZ)
    # Apply 6AM cutoff: 00-05 belongs to previous diary day
    if h < _CUTOFF_H:
        return (candidate - _dt.timedelta(days=1)).date()
    return candidate.date()


# ── DB queries ───────────────────────────────────────────────────────────────

def _query_open_episodes(conn: sqlite3.Connection,
                         cutoff_utc: str) -> list[dict]:
    """Unresolved affect episodes from last 7d, not superseded."""
    rows = conn.execute(
        "SELECT id, description, label, created_at"
        " FROM affect"
        " WHERE superseded_by IS NULL"
        " AND unresolved = 1"
        " AND resolved_at IS NULL"
        " AND created_at >= ?"
        " ORDER BY created_at ASC",
        (cutoff_utc,),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_digests_range(conn: sqlite3.Connection,
                         from_utc: str, to_utc: str) -> list[dict]:
    """session_digests in UTC range [from, to), newest first."""
    rows = conn.execute(
        "SELECT sid, date, ts, text, kind, tl_line, life_lines"
        " FROM session_digests"
        " WHERE ts >= ? AND ts < ?"
        " ORDER BY ts ASC",
        (from_utc, to_utc),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_affect_range(conn: sqlite3.Connection,
                        from_utc: str, to_utc: str) -> list[dict]:
    """Affect rows in UTC range for tone computation."""
    rows = conn.execute(
        "SELECT valence, arousal, importance, created_at"
        " FROM affect"
        " WHERE superseded_by IS NULL"
        " AND created_at >= ? AND created_at < ?",
        (from_utc, to_utc),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_diary_range(conn: sqlite3.Connection,
                       date_from: str, date_to: str) -> dict[str, str]:
    """diary.tl_line keyed by date string, for day 4-7 zone."""
    rows = conn.execute(
        "SELECT date, tl_line FROM diary"
        " WHERE date >= ? AND date <= ? AND tl_line IS NOT NULL",
        (date_from, date_to),
    ).fetchall()
    return {r["date"]: r["tl_line"] for r in rows}


def _query_current_sid(conn: sqlite3.Connection) -> str | None:
    """Latest in-progress session id (lifecycle:start with no end).
    Used to exclude the current session from timeline."""
    row = conn.execute(
        "SELECT target_id FROM audit_log"
        " WHERE action = 'session_lifecycle:start'"
        " ORDER BY id DESC LIMIT 1",
    ).fetchone()
    if not row:
        return None
    sid = row["target_id"]
    # Confirm no end row
    end = conn.execute(
        "SELECT 1 FROM audit_log"
        " WHERE action = 'session_lifecycle:end' AND target_id = ?",
        (sid,),
    ).fetchone()
    return sid if not end else None


# ── zone renderers ───────────────────────────────────────────────────────────

def _render_open_episodes(episodes: list[dict]) -> list[str]:
    lines: list[str] = []
    for ep in episodes:
        desc = ep.get("description") or ep.get("label") or "(ep)"
        label = ep.get("label") or ""
        tag = f" [{label}]" if label and label != desc else ""
        lines.append(f"> 未解: {desc}{tag}")
    return lines


def _render_24h(digests: list[dict],
                current_sid: str | None) -> list[str]:
    """Flat film-strip newest→oldest, cap 15.

    Casual sessions: each LIFE line is stamped with its own HH:MM (parsed
    from the leading HH:MM prefix written by the model); prefix-less lines
    (legacy rows) fall back to session start time.  TL line uses session
    start time.  Task sessions: single TL line at session start time.

    Sort key and day-divider attribution use the per-line time where
    available so lines spanning 08:00-20:00 land at their own hours.
    Day crossings get --- MM-DD --- divider.
    """
    # Each flat_entry: (sort_key_str, disp_date, rendered_line, is_first_of_session)
    # sort_key is a string we can compare lexicographically (UTC ISO for session,
    # or a synthetic local-HH:MM key for per-line times within the session)
    flat_entries: list[tuple[str, _dt.date, str, bool]] = []

    for sd in digests:
        if sd["sid"] == current_sid:
            continue
        ts = sd.get("ts") or ""
        sess_date = _local_date_from_utc(ts)
        sess_hhmm = _hhmm_melb(ts)
        kind = (sd.get("kind") or "casual").lower()
        tl = _tl_or_fallback(sd)
        life_raw = sd.get("life_lines") or ""
        life_items = [x.strip() for x in life_raw.splitlines() if x.strip()]
        anchor = _tl_anchor_sid(sd["sid"])

        if kind == "task" or not life_items:
            # Single line: TL at session start time
            rendered = f"{sess_hhmm} {tl} {anchor}"
            flat_entries.append((ts, sess_date, rendered, True))
        else:
            # Casual with LIFE items — one rendered line per item
            for idx, item in enumerate(life_items):
                line_hhmm, text = _life_line_hhmm(item, sess_hhmm)
                line_date = _life_line_local_date(item, sess_date, sess_hhmm)
                # Sort key: use ts for first item so session ordering is stable;
                # subsequent items inherit the session ts with HH:MM appended
                # to maintain relative ordering within the same session.
                sort_key = ts if idx == 0 else f"{ts[:10]}T{line_hhmm}:00Z"
                if idx == 0:
                    rendered = f"{line_hhmm} {text} {anchor}"
                else:
                    rendered = f"{line_hhmm} {text}"
                flat_entries.append((sort_key, line_date, rendered, idx == 0))

    # Newest first
    flat_entries.sort(key=lambda e: e[0], reverse=True)

    # Interleave day dividers and flatten; cap _24H_CAP
    lines: list[str] = []
    prev_date: _dt.date | None = None
    for _sort_key, disp_date, rendered_line, _is_first in flat_entries:
        if len(lines) >= _24H_CAP:
            break
        if prev_date is not None and disp_date != prev_date:
            lines.append(f"--- {disp_date.strftime('%m-%d')} ---")
        prev_date = disp_date
        lines.append(rendered_line)
        if len(lines) >= _24H_CAP:
            break

    return lines


def _render_2472h(digests: list[dict],
                  affect_rows: list[dict],
                  current_sid: str | None) -> list[str]:
    """Per-day headers + AM/PM/ND period lines, newest day first, cap ~12."""
    # Bucket digests by (diary_date, period)
    from collections import defaultdict
    buckets: dict[tuple[_dt.date, str], list[dict]] = defaultdict(list)
    for sd in digests:
        if sd["sid"] == current_sid:
            continue
        ts = sd.get("ts") or ""
        diary_date, period = _period_diary_date(ts)
        buckets[(diary_date, period)].append(sd)

    # Bucket affect by diary_date for tone
    affect_by_date: dict[_dt.date, list[dict]] = defaultdict(list)
    for ar in affect_rows:
        diary_date, _ = _period_diary_date(ar.get("created_at") or "")
        affect_by_date[diary_date].append(ar)

    # Unique dates newest→oldest
    dates = sorted({k[0] for k in buckets}, reverse=True)
    lines: list[str] = []
    for date in dates:
        if len(lines) >= _2472H_CAP:
            break
        tone_label = _tone_from_rows(affect_by_date.get(date, []))
        lines.append(
            f"**{date.strftime('%m-%d')} Day 【{tone_label}】**"
        )
        for period in ("AM", "PM", "ND"):
            sds = sorted(buckets.get((date, period), []),
                         key=lambda sd: sd.get("ts") or "")
            if not sds:
                continue
            parts = [_tl_or_fallback(sd) for sd in sds]
            text = " · ".join(parts)
            # Truncate to keep budget
            if len(text) > 80:
                text = text[:77] + "…"
            lines.append(f"{period} {text}")
            if len(lines) >= _2472H_CAP:
                break

    return lines


def _render_day47(dates_4_7: list[_dt.date],
                  affect_rows_by_date: dict[_dt.date, list[dict]],
                  diary_tl: dict[str, str],
                  this_week_affect: list[dict],
                  last_week_affect: list[dict]) -> list[str]:
    """Week trend line + per-day 【tone】 diary.tl_line.
    Only renders if there is actual affect or diary data for day 4-7.
    """
    # Skip entirely if no actual data (avoids empty date stubs)
    has_data = bool(affect_rows_by_date) or bool(diary_tl)
    if not has_data:
        return []

    lines: list[str] = []
    trend = _week_trend(this_week_affect, last_week_affect)
    lines.append(f"Week 【{trend}】")
    for date in sorted(dates_4_7, reverse=True):
        day_affect = affect_rows_by_date.get(date, [])
        tone_label = _tone_from_rows(day_affect)
        dtl = diary_tl.get(date.isoformat(), "")
        anchor = _tl_anchor_date(date.isoformat())
        if dtl:
            lines.append(
                f"{date.strftime('%m-%d')} Day 【{tone_label}】 {dtl} {anchor}")
        else:
            lines.append(
                f"{date.strftime('%m-%d')} Day 【{tone_label}】 {anchor}")
    return lines


# ── main render ──────────────────────────────────────────────────────────────

def render_timeline(conn: sqlite3.Connection) -> str:
    """Render the ## Timeline block.

    Uses UTC boundaries for DB queries; Melbourne local for display.
    Never naive datetime. Returns empty string if DB is empty/cold.
    """
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    now_utc_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Time boundaries (UTC ISO strings)
    t_24h = (now_utc - _dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_72h = (now_utc - _dt.timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_7d  = (now_utc - _dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_14d = (now_utc - _dt.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")

    current_sid = _query_current_sid(conn)

    # ── open episodes ────────────────────────────────────────────────────────
    open_eps = _query_open_episodes(conn, t_7d)
    open_lines = _render_open_episodes(open_eps)

    # ── last 24h ─────────────────────────────────────────────────────────────
    digests_24h = _query_digests_range(conn, t_24h, now_utc_iso)
    lines_24h = _render_24h(digests_24h, current_sid)

    # ── 24-72h ───────────────────────────────────────────────────────────────
    digests_2472 = _query_digests_range(conn, t_72h, t_24h)
    affect_2472 = _query_affect_range(conn, t_72h, t_24h)
    lines_2472 = _render_2472h(digests_2472, affect_2472, current_sid)

    # ── day 4-7 ──────────────────────────────────────────────────────────────
    # Diary date boundaries (Melbourne local)
    now_melb = now_utc.astimezone(_TZ)
    today_melb = (now_melb if now_melb.hour >= _CUTOFF_H
                  else now_melb - _dt.timedelta(days=1)).date()
    # Day 4-7: 3 days ago through 6 days ago (0-indexed from today)
    dates_4_7 = [today_melb - _dt.timedelta(days=d) for d in range(3, 7)]
    date_4_7_str_from = dates_4_7[-1].isoformat()
    date_4_7_str_to   = dates_4_7[0].isoformat()

    diary_tl = _query_diary_range(conn, date_4_7_str_from, date_4_7_str_to)
    affect_4_7 = _query_affect_range(conn, t_7d, t_72h)
    affect_last_wk = _query_affect_range(conn, t_14d, t_7d)

    affect_by_date: dict[_dt.date, list[dict]] = {}
    for ar in affect_4_7:
        d, _ = _period_diary_date(ar.get("created_at") or "")
        affect_by_date.setdefault(d, []).append(ar)

    lines_47 = _render_day47(dates_4_7, affect_by_date, diary_tl,
                             affect_4_7, affect_last_wk)

    # ── assemble + trim to budget ────────────────────────────────────────────
    all_sections = _assemble(open_lines, lines_24h, lines_2472, lines_47)
    text = "## Timeline\n" + "\n".join(all_sections) if all_sections else "## Timeline\n_none_"

    # Trim if over budget
    if len(text) > _BUDGET:
        text = _trim_to_budget(text, open_lines, lines_24h, lines_2472, lines_47)

    return text


def _assemble(open_lines: list[str],
              lines_24h: list[str],
              lines_2472: list[str],
              lines_47: list[str]) -> list[str]:
    parts: list[str] = []
    parts.extend(open_lines)
    parts.extend(lines_24h)
    if lines_2472:
        if parts:
            parts.append("")
        parts.extend(lines_2472)
    if lines_47:
        if parts:
            parts.append("")
        parts.extend(lines_47)
    return parts


def _trim_to_budget(text: str,
                    open_lines: list[str],
                    lines_24h: list[str],
                    lines_2472: list[str],
                    lines_47: list[str]) -> str:
    """Trim order: day lines → period lines (farthest day first) → 24h farthest."""
    # Step 1: trim day4-7 lines one at a time (farthest first = last in list)
    l47 = list(lines_47)
    l2472 = list(lines_2472)
    l24h = list(lines_24h)

    def _rebuild() -> str:
        parts = _assemble(open_lines, l24h, l2472, l47)
        body = "\n".join(parts) if parts else "_none_"
        return "## Timeline\n" + body

    # Trim day lines (skip the Week header at index 0)
    while len(l47) > 1 and len(_rebuild()) > _BUDGET:
        l47.pop()  # remove oldest day line

    # Remove Week header too if now just the header
    if len(l47) == 1 and len(_rebuild()) > _BUDGET:
        l47 = []

    # Trim 2472h period lines (farthest day first = lines near end)
    while l2472 and len(_rebuild()) > _BUDGET:
        l2472.pop()
    # Clean up orphaned day headers
    if l2472 and l2472[-1].startswith("**"):
        l2472.pop()

    # Trim 24h farthest lines
    while l24h and len(_rebuild()) > _BUDGET:
        l24h.pop()

    return _rebuild()
