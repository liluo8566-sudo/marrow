"""Timeline block renderer — merged affect + session view.

Two outlets:
  render_timeline(conn) -> str   # SessionStart injection / dashboard block
  Both use the same render fn; caller decides where to put the output.

Format (FINAL spec, plan 4A-3):
  未解: <desc> [label] <!-- tl:ep:<id> -->  — open affect episodes, 7d expiry, top of block
  Last 24h: flat HH:MM film-strip newest→oldest, cap 20
    - LIFE lines (casual) + TL line (task)
    - day crossings: --- MM-DD --- divider
  Today-1 overflow + today-2: per-day **MM-DD Day 【tone】** + AM/PM/ND periods
    - ND 00-06 belongs to PREVIOUS day
    - all sessions' TLs in period joined time-order, truncated; empty hidden
    - 24h sessions exceeding cap spill here as day summaries
  Day 3-6: Week 【tone ↗/↘/→】 trend + one line per day 【tone】 diary.tl_line
  No in-progress session line.
  Trim order: day lines → period lines (farthest day first) → 24h farthest.
  Budget ~4000 chars (safety net).

Tone labels reuse top_sections._tone / _vband / _aband (no duplication).
All DB timestamps are UTC; Melbourne on render via timeutil.
"""
from __future__ import annotations

import datetime as _dt
import re as _re
import sqlite3
from zoneinfo import ZoneInfo
from .top_sections import _tone, _vband, _aband, _wmean
from . import config as _config

_TZ = _config.get_tz()
_MELB_TZ = ZoneInfo("Australia/Melbourne")
# Matches leading HH:MM in a LIFE line (e.g. "21:40 买了b5精华")
_LIFE_TS_RE = _re.compile(r"^(\d{2}:\d{2})(?:\s+|(?=【))(.*)", _re.DOTALL)
_CUTOFF_H = 6          # 6AM local day boundary
_BUDGET = 4000         # soft char budget (safety net; zone caps control sizing)
_24H_CAP = 20          # max film-strip lines
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


def _calendar_date_from_utc(utc_iso: str) -> _dt.date:
    """UTC ISO → Melbourne calendar date without the 6AM cutoff."""
    s = (utc_iso or "").strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return _now_melb().date()
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_TZ).date()


def _parse_utc(utc_iso: str) -> _dt.datetime | None:
    s = (utc_iso or "").strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def _utc_iso(d: _dt.datetime) -> str:
    return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


# Bug 5: rendered day-line pattern written by old backfill (e.g. "06-09 Day 【平淡】")
_RENDERED_DAY_RE = _re.compile(r"^\d{2}-\d{2}\s+Day\s+【.+】")


def _tl_or_fallback(sd: dict) -> str:
    """tl_line if set and not a rendered day-line artifact; else sanitised
    truncation of legacy prose body."""
    tl = sd.get("tl_line")
    # Bug 5 guard: treat rendered "MM-DD Day 【tone】" strings and blank/
    # whitespace-only values as NULL so we fall through to the body fallback.
    if tl and tl.strip() and not _RENDERED_DAY_RE.match(tl.strip()):
        return tl
    body = (sd.get("text") or "").strip()
    # Legacy prose digests carry markdown + newlines — flatten before cut.
    body = _re.sub(r"[#*`>]+", "", body)
    body = _re.sub(r"\s+", " ", body).strip()
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


def _life_line_utc_and_date(item: str, session_utc_iso: str,
                            session_hhmm: str) -> tuple[str, _dt.date]:
    """Return (utc_iso_sort_key, diary_date) for a LIFE line.

    Prefix timestamps are combined with the digest timestamp's local calendar
    date. No batch or cross-midnight heuristic is applied.
    """
    m = _LIFE_TS_RE.match(item)
    if not m:
        diary_date = _local_date_from_utc(session_utc_iso)
        return session_utc_iso, diary_date

    hhmm = m.group(1)
    try:
        h, mi = int(hhmm[:2]), int(hhmm[3:5])
    except ValueError:
        diary_date = _local_date_from_utc(session_utc_iso)
        return session_utc_iso, diary_date

    sess_dt = _parse_utc(session_utc_iso)
    if sess_dt is None:
        diary_date = _local_date_from_utc(session_utc_iso)
        return session_utc_iso, diary_date
    sess_local = sess_dt.astimezone(_TZ)

    cal_date = sess_local.date()
    candidate = _dt.datetime(cal_date.year, cal_date.month, cal_date.day,
                             h, mi, 0, tzinfo=_TZ)

    if candidate.hour < _CUTOFF_H:
        diary_date = (candidate - _dt.timedelta(days=1)).date()
    else:
        diary_date = candidate.date()

    return _utc_iso(candidate), diary_date


def _life_line_local_date(item: str, session_date: _dt.date,
                          session_hhmm: str) -> _dt.date:
    """Kept for unit-test compatibility. Derives diary date from session_date
    treated as the session's LOCAL CALENDAR date (not diary date).

    For new code use _life_line_utc_and_date instead.
    """
    m = _LIFE_TS_RE.match(item)
    if not m:
        return session_date
    hhmm = m.group(1)
    try:
        h, mi = int(hhmm[:2]), int(hhmm[3:5])
    except ValueError:
        return session_date
    # Build candidate on the given calendar date; single 6AM cutoff.
    candidate = _dt.datetime(
        session_date.year, session_date.month, session_date.day,
        h, mi, 0, tzinfo=_TZ)
    if candidate.hour < _CUTOFF_H:
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
    """Visible digests with sd.ts in a generous range for Python filtering.

    sd.date is intentionally ignored; it can be stale for cross-day sessions.
    """
    from_dt = _dt.datetime.fromisoformat(from_utc.replace("Z", "+00:00"))
    ts_floor = (from_dt - _dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT sd.sid, sd.date, sd.text, sd.kind, sd.tl_line, sd.life_lines,"
        " sd.ts AS ts"
        " FROM session_digests sd"
        " WHERE sd.ts >= ? AND sd.ts < ? AND sd.tl_hidden = 0"
        " ORDER BY sd.ts ASC",
        (ts_floor, to_utc),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_session_event_span(conn: sqlite3.Connection,
                              sid: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT MIN(timestamp) AS t_start, MAX(timestamp) AS t_end"
        " FROM events WHERE session_id = ?",
        (sid,),
    ).fetchone()
    if row is None:
        return None, None
    return row["t_start"], row["t_end"]


def _query_session_max_event_ts(conn: sqlite3.Connection,
                                sids: list[str]) -> dict[str, str]:
    if not sids:
        return {}
    placeholders = ",".join("?" for _ in sids)
    rows = conn.execute(
        "SELECT session_id, MAX(timestamp) AS t_end"
        f" FROM events WHERE session_id IN ({placeholders})"
        " GROUP BY session_id",
        sids,
    ).fetchall()
    return {r["session_id"]: r["t_end"] for r in rows if r["t_end"]}


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


def _query_affect_by_session(conn: sqlite3.Connection,
                              from_utc: str, to_utc: str) -> dict[str, list[dict]]:
    """Affect rows grouped by session_id for 24h tone tags (Bug 3).

    Affect rows don't carry a session_id column, so we match by time span:
    rows whose created_at falls within [session_start, session_end) where
    session_end = next session's start (or to_utc).  We approximate this by
    joining on events: affect.created_at between first and last event of
    the session.  For sessions with no events we use the digest ts window.
    Returns {sid: [affect_row, ...]}
    """
    # Fetch affect rows in range with their timestamps
    rows = conn.execute(
        "SELECT valence, arousal, importance, created_at"
        " FROM affect"
        " WHERE superseded_by IS NULL"
        " AND created_at >= ? AND created_at < ?",
        (from_utc, to_utc),
    ).fetchall()
    affect_rows = [dict(r) for r in rows]

    # Fetch session time spans (first_event_ts, last_event_ts) from events
    spans = conn.execute(
        "SELECT session_id, MIN(timestamp) AS t_start, MAX(timestamp) AS t_end"
        " FROM events"
        " WHERE timestamp >= ? AND timestamp < ?"
        " GROUP BY session_id",
        (from_utc, to_utc),
    ).fetchall()

    by_sid: dict[str, list[dict]] = {}
    for span in spans:
        sid = span["session_id"]
        t_start = span["t_start"]
        t_end = span["t_end"]
        by_sid[sid] = [
            ar for ar in affect_rows
            if t_start <= ar["created_at"] <= t_end
        ]
    return by_sid


def _query_diary_range(conn: sqlite3.Connection,
                       date_from: str, date_to: str) -> dict[str, str]:
    """diary.tl_line (or truncated text fallback) keyed by date string, for day 4-8 zone.

    Rows whose tl_line matches the rendered-day-line pattern or are empty fall
    back to truncated diary.text (same logic as _tl_or_fallback).
    """
    rows = conn.execute(
        "SELECT date, tl_line, content FROM diary"
        " WHERE date >= ? AND date <= ? AND tl_hidden = 0",
        (date_from, date_to),
    ).fetchall()
    result: dict[str, str] = {}
    for r in rows:
        tl = (r["tl_line"] or "").strip()
        tl_bare = tl.strip("*").strip()
        if tl_bare and not _RENDERED_DAY_RE.match(tl_bare):
            result[r["date"]] = tl
    return result


def _query_manual_events_24h(conn: sqlite3.Connection,
                             from_utc: str, to_utc: str) -> list[dict]:
    """Manual events (channel='manual') in the 24h window."""
    rows = conn.execute(
        "SELECT id, timestamp, content FROM events"
        " WHERE channel='manual' AND timestamp >= ? AND timestamp < ?"
        " ORDER BY timestamp ASC",
        (from_utc, to_utc),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_manual_events_range(conn: sqlite3.Connection,
                               from_utc: str, to_utc: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, timestamp, content FROM events"
        " WHERE channel='manual' AND timestamp >= ? AND timestamp < ?"
        " ORDER BY timestamp ASC",
        (from_utc, to_utc),
    ).fetchall()
    return [dict(r) for r in rows]


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
        ep_id = ep.get("id", 0)
        lines.append(f"未解: {desc}{tag} <!-- tl:ep:{ep_id} -->")
    return lines


def _render_24h(digests: list[dict],
                current_sid: str | None,
                manual_events: list[dict] | None = None,
                from_utc: str | None = None,
                to_utc: str | None = None,
                event_spans: dict[str, tuple[str | None, str | None]] | None = None,
                exclude_full_session_sids: set[str] | None = None,
                ) -> tuple[list[str], list[dict]]:
    """Flat 24h film-strip, newest first, filtered per rendered line."""
    if event_spans is None:
        event_spans = {}
    if exclude_full_session_sids is None:
        exclude_full_session_sids = set()

    to_dt = _parse_utc(to_utc or _utc_iso(_dt.datetime.now(_dt.timezone.utc)))
    if to_dt is None:
        to_dt = _dt.datetime.now(_dt.timezone.utc)
    from_dt = _parse_utc(from_utc or _utc_iso(to_dt - _dt.timedelta(hours=24)))
    if from_dt is None:
        from_dt = to_dt - _dt.timedelta(hours=24)

    entries: list[dict] = []

    def _in_window(ts_iso: str) -> _dt.datetime | None:
        ts_dt = _parse_utc(ts_iso)
        if ts_dt is None or ts_dt < from_dt or ts_dt >= to_dt:
            return None
        return ts_dt

    def _add_session_line(sd: dict, idx: int, ts_iso: str,
                          hhmm: str, text: str) -> None:
        ts_dt = _in_window(ts_iso)
        if ts_dt is None or not text:
            return
        entries.append({
            "ts": ts_dt,
            "local_date": ts_dt.astimezone(_TZ).date(),
            "sid": sd["sid"],
            "line_index": idx,
            "hhmm": hhmm,
            "text": text,
        })

    def _life_line_window_times(item: str, context_ts: str,
                                span: tuple[str | None, str | None] | None
                                ) -> list[str]:
        m = _LIFE_TS_RE.match(item)
        if not m:
            return [context_ts]
        try:
            h, mi = int(m.group(1)[:2]), int(m.group(1)[3:5])
        except ValueError:
            return [context_ts]

        context_dt = _parse_utc(context_ts)
        if context_dt is None:
            return []
        span_start_utc = span_end_utc = None
        if span is not None:
            span_start_utc = _parse_utc(span[0] or "")
            span_end_utc = _parse_utc(span[1] or "")
        if span_start_utc is None or span_end_utc is None:
            context_date = context_dt.astimezone(_MELB_TZ).date()
            span_start = span_end = None
            dates = (context_date, context_date - _dt.timedelta(days=1))
        else:
            span_start = span_start_utc.astimezone(_MELB_TZ)
            span_end = span_end_utc.astimezone(_MELB_TZ)
            days = (span_end.date() - span_start.date()).days
            dates = tuple(span_start.date() + _dt.timedelta(days=i)
                          for i in range(days + 1))

        candidates = [
            _dt.datetime(d.year, d.month, d.day, h, mi, 0, tzinfo=_MELB_TZ)
            for d in dates
        ]
        if span_start is not None and span_end is not None:
            in_span = [c for c in candidates if span_start <= c <= span_end]
            if in_span:
                candidates = in_span
            else:
                candidates = [min(
                    candidates,
                    key=lambda c: min(abs((c - span_start).total_seconds()),
                                      abs((c - span_end).total_seconds())),
                )]
        for candidate in candidates:
            if from_dt <= candidate.astimezone(_dt.timezone.utc) < to_dt:
                return [_utc_iso(candidate)]
        return []

    for sd in digests:
        if sd["sid"] == current_sid:
            continue
        ts = sd.get("ts") or ""
        sess_hhmm = _hhmm_melb(ts)
        kind = (sd.get("kind") or "casual").lower()
        tl = _tl_or_fallback(sd)
        life_raw = sd.get("life_lines") or ""
        life_items = [x.strip() for x in life_raw.splitlines() if x.strip()]

        if kind == "task" or not life_items:
            if sd["sid"] in exclude_full_session_sids:
                continue
            _add_session_line(sd, 0, ts, sess_hhmm, tl)
        else:
            for idx, item in enumerate(life_items):
                line_hhmm, text = _life_line_hhmm(item, sess_hhmm)
                if _re.match(r"^\d{2}:\d{2}[\s【]", item):
                    text = item
                for ts_iso in _life_line_window_times(
                    item, ts, event_spans.get(sd["sid"])
                ):
                    _add_session_line(sd, idx, ts_iso, line_hhmm, text)

    for ev in (manual_events or []):
        ts = ev.get("timestamp") or ""
        ts_dt = _in_window(ts)
        content = (ev.get("content") or "").strip()
        if ts_dt is None or not content:
            continue
        entries.append({
            "ts": ts_dt,
            "local_date": ts_dt.astimezone(_TZ).date(),
            "sid": None,
            "event_id": ev["id"],
            "line_index": None,
            "hhmm": _hhmm_melb(ts),
            "text": content,
        })

    entries.sort(key=lambda e: e["ts"], reverse=True)
    shown = entries[:_24H_CAP]
    dropped = entries[_24H_CAP:]

    lines: list[str] = []
    anchored_sids: set[str] = set()
    for cal_date in sorted({entry["local_date"] for entry in shown}, reverse=True):
        lines.append(f"--- {cal_date.strftime('%m-%d')} ---")
        for entry in (e for e in shown if e["local_date"] == cal_date):
            hhmm = entry["hhmm"]
            text = entry["text"]
            sid = entry.get("sid")
            if sid is None:
                lines.append(f"{hhmm} {text} <!-- tl:e:{entry['event_id']} -->")
                continue

            if sid not in anchored_sids:
                anchored_sids.add(sid)
            anchor = f" {_tl_anchor_sid(sid)}"
            if _re.match(r"^\d{2}:\d{2}[\s【]", text):
                lines.append(f"{text}{anchor}")
            else:
                lines.append(f"{hhmm} {text}{anchor}")

    overflow_by_sid: dict[str, dict] = {}
    for entry in dropped:
        sid = entry.get("sid")
        if sid is None:
            continue
        item = overflow_by_sid.setdefault(
            sid, {"sid": sid, "dropped_count": 0, "line_indexes": []}
        )
        item["dropped_count"] += 1
        item["line_indexes"].append(entry["line_index"])

    return lines, list(overflow_by_sid.values())


def _render_2472h(digests: list[dict],
                  affect_rows: list[dict],
                  current_sid: str | None,
                  manual_events: list[dict] | None = None,
                  overflow_24h: list[dict] | None = None) -> list[str]:
    """Per-day headers + AM/PM/ND period lines, newest day first."""
    from collections import defaultdict
    buckets: dict[tuple[_dt.date, str], list[tuple[str, str, str]]] = defaultdict(list)
    for sd in digests:
        if sd["sid"] == current_sid:
            continue
        ts = sd.get("ts") or ""
        diary_date, period = _period_diary_date(ts)
        buckets[(diary_date, period)].append((ts, _tl_or_fallback(sd), ""))

    for ev in (manual_events or []):
        ts = ev.get("timestamp") or ""
        diary_date, period = _period_diary_date(ts)
        anchor = f"<!-- tl:e:{ev['id']} -->"
        buckets[(diary_date, period)].append((ts, ev.get("content") or "", anchor))

    # Bucket affect by diary_date for tone
    affect_by_date: dict[_dt.date, list[dict]] = defaultdict(list)
    for ar in affect_rows:
        diary_date, _ = _period_diary_date(ar.get("created_at") or "")
        affect_by_date[diary_date].append(ar)

    # Unique dates newest→oldest
    dates = sorted({k[0] for k in buckets}, reverse=True)
    lines: list[str] = []
    for date in dates:
        tone_label = _tone_from_rows(affect_by_date.get(date, []))
        lines.append(
            f"**{date.strftime('%m-%d')} Day 【{tone_label}】** {_tl_anchor_date(date.isoformat())}"
        )
        for period in ("AM", "PM", "ND"):
            items = sorted(buckets.get((date, period), []), key=lambda x: x[0])
            if not items:
                continue
            text = _render_2472_period_text(items)
            lines.append(f"{period} {text}")

    return lines


def _render_2472_period_text(items: list[tuple[str, str, str]]) -> str:
    parts: list[str] = []
    visible_len = 0
    deferred_anchors: list[str] = []
    for _ts, text, anchor in items:
        visible = (text or "").strip()
        sep = " · " if parts else ""
        room = 250 - visible_len - len(sep)
        if room <= 0:
            if anchor:
                deferred_anchors.append(anchor)
            continue
        if len(visible) > room:
            visible = visible[: max(0, room - 1)] + "…"
        piece = sep + visible
        if anchor:
            piece += f" {anchor}"
        parts.append(piece)
        visible_len += len(sep) + len(visible)

    text = "".join(parts).rstrip()
    if deferred_anchors:
        text = f"{text} {' '.join(deferred_anchors)}".strip()
    return text


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
    lines.append(f"**Week 【{trend}】**")
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
    now_melb = now_utc.astimezone(_TZ)
    yesterday_start_utc = _dt.datetime.combine(
        (now_melb - _dt.timedelta(days=1)).date(),
        _dt.time.min,
        tzinfo=_TZ,
    ).astimezone(_dt.timezone.utc)
    yesterday_start_utc_iso = _utc_iso(yesterday_start_utc)

    # Time boundaries (UTC ISO strings)
    t_24h = (now_utc - _dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_72h = (now_utc - _dt.timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_7d  = (now_utc - _dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_14d = (now_utc - _dt.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")

    current_sid = _query_current_sid(conn)

    # Melbourne diary-date for "today" (6AM boundary)
    today_melb = (now_melb if now_melb.hour >= _CUTOFF_H
                  else now_melb - _dt.timedelta(days=1)).date()

    # ── open episodes ────────────────────────────────────────────────────────
    open_eps = _query_open_episodes(conn, t_7d)
    open_lines = _render_open_episodes(open_eps)

    # ── zone (b): today-1 overflow + today-2 day summaries ────────────────
    day2 = today_melb - _dt.timedelta(days=2)
    zone_b_from_utc = _day_start_utc(day2).strftime("%Y-%m-%dT%H:%M:%SZ")
    zone_b_from_dt = _parse_utc(zone_b_from_utc)
    t_24h_dt = _parse_utc(t_24h)
    zone_b_candidates = _query_digests_range(conn, zone_b_from_utc, now_utc_iso)
    max_event_ts = _query_session_max_event_ts(
        conn, [d["sid"] for d in zone_b_candidates]
    )
    digests_2472 = [
        d for d in zone_b_candidates
        if (
            zone_b_from_dt is not None and t_24h_dt is not None
            and (parsed := _parse_utc(d.get("ts") or "")) is not None
            and zone_b_from_dt <= parsed < t_24h_dt
        ) or (
            zone_b_from_dt is not None and t_24h_dt is not None
            and (event_parsed := _parse_utc(max_event_ts.get(d["sid"], ""))) is not None
            and zone_b_from_dt <= event_parsed < t_24h_dt
        )
    ]
    zone_b_event_sids = {
        d["sid"] for d in digests_2472
        if (
            t_24h_dt is not None
            and (parsed := _parse_utc(d.get("ts") or "")) is not None
            and parsed >= t_24h_dt
        )
    }

    # ── last 24h ─────────────────────────────────────────────────────────────
    digests_24h = _query_digests_range(conn, yesterday_start_utc_iso, now_utc_iso)
    event_spans_24h = {
        d["sid"]: _query_session_event_span(conn, d["sid"])
        for d in digests_24h
    }
    manual_24h = _query_manual_events_24h(conn, yesterday_start_utc_iso, now_utc_iso)
    lines_24h, overflow_24h = _render_24h(
        digests_24h, current_sid, manual_24h,
        from_utc=yesterday_start_utc_iso, to_utc=now_utc_iso,
        event_spans=event_spans_24h,
        exclude_full_session_sids=zone_b_event_sids,
    )

    affect_2472 = _query_affect_range(conn, zone_b_from_utc, now_utc_iso)
    manual_2472 = _query_manual_events_range(conn, zone_b_from_utc, t_24h)
    lines_2472 = _render_2472h(
        digests_2472, affect_2472, current_sid, manual_2472,
        overflow_24h=overflow_24h,
    )

    # ── zone (c): diary dates today-3 .. today-6 (four days) ────────────────
    dates_3_6 = [today_melb - _dt.timedelta(days=d) for d in range(3, 7)]
    date_c_str_from = dates_3_6[-1].isoformat()   # oldest (today-6)
    date_c_str_to   = dates_3_6[0].isoformat()    # newest (today-3)

    diary_tl = _query_diary_range(conn, date_c_str_from, date_c_str_to)

    # Affect for zone (c): from diary-day-start of today-6 up to zone_b_from_utc
    day6 = today_melb - _dt.timedelta(days=6)
    zone_c_from_utc = _day_start_utc(day6).strftime("%Y-%m-%dT%H:%M:%SZ")
    affect_3_6 = _query_affect_range(conn, zone_c_from_utc, zone_b_from_utc)
    affect_last_wk = _query_affect_range(conn, t_14d, t_7d)

    affect_by_date: dict[_dt.date, list[dict]] = {}
    for ar in affect_3_6:
        d, _ = _period_diary_date(ar.get("created_at") or "")
        affect_by_date.setdefault(d, []).append(ar)

    lines_47 = _render_day47(dates_3_6, affect_by_date, diary_tl,
                             affect_3_6, affect_last_wk)

    # ── assemble + trim to budget ────────────────────────────────────────────
    all_sections = _assemble(open_lines, lines_24h, lines_2472, lines_47)
    text = "## Timeline\n" + "\n".join(all_sections) if all_sections else "## Timeline\n_none_"

    # Trim if over budget (visible text only — edit anchors don't count)
    if _visible_len(text) > _BUDGET:
        text = _trim_to_budget(text, open_lines, lines_24h, lines_2472, lines_47)

    # Append tl-rendered trail marker so reconcile knows which anchors were rendered
    trail_sids  = sorted(set(_TL_TRAIL_SID_RE.findall(text)))
    trail_dates = sorted(set(_TL_TRAIL_DATE_RE.findall(text)))
    trail_evts  = sorted(set(_TL_TRAIL_EVT_RE.findall(text)))
    trail_eps   = sorted(set(_TL_TRAIL_EP_RE.findall(text)))

    parts: list[str] = []
    if trail_sids:  parts.append("s=" + ",".join(trail_sids))
    if trail_dates: parts.append("d=" + ",".join(trail_dates))
    if trail_evts:  parts.append("e=" + ",".join(trail_evts))
    if trail_eps:   parts.append("ep=" + ",".join(trail_eps))
    if parts:
        text += f"\n<!-- tl-rendered:{';'.join(parts)} -->"

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


_ANCHOR_RE = _re.compile(r"<!--.*?-->")

# Trail marker regexes — used to extract rendered anchor IDs for reconcile
_TL_TRAIL_SID_RE  = _re.compile(r"<!--\s*tl:(?!d:|e:|ep:)(\S+?)\s*-->")
_TL_TRAIL_DATE_RE = _re.compile(r"<!--\s*tl:d:(\d{4}-\d{2}-\d{2})\s*-->")
_TL_TRAIL_EVT_RE  = _re.compile(r"<!--\s*tl:e:(\d+)\s*-->")
_TL_TRAIL_EP_RE   = _re.compile(r"<!--\s*tl:ep:(\d+)\s*-->")


def _visible_len(s: str) -> int:
    """Budget length: rendered text minus invisible HTML anchors."""
    return len(_ANCHOR_RE.sub("", s))

visible_len = _visible_len


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
    while len(l47) > 1 and _visible_len(_rebuild()) > _BUDGET:
        l47.pop()  # remove oldest day line

    # Remove Week header too if now just the header
    if len(l47) == 1 and _visible_len(_rebuild()) > _BUDGET:
        l47 = []

    # Trim 2472h period lines (farthest day first = lines near end)
    while l2472 and _visible_len(_rebuild()) > _BUDGET:
        l2472.pop()
    # Clean up orphaned day headers
    if l2472 and l2472[-1].startswith("**"):
        l2472.pop()

    # Trim 24h farthest lines
    while l24h and _visible_len(_rebuild()) > _BUDGET:
        l24h.pop()

    return _rebuild()
