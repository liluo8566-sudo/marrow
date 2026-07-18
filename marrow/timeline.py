"""Timeline block renderer — life-line session view.

  render_timeline(conn) -> str   # SessionStart injection / daybrief block

Format:
  Zone A (last 24h from natural midnight): flat HH:MM film-strip newest→oldest,
    cap 20 · LIFE lines (life_lines column) · day crossings `--- MM-DD ---`.
  Zone B (3 diary days before zone A start): per-day `**MM-DD Day 【tone】**` +
    overview from diary.tone/overview; NULL overview days skipped.
  No in-progress session line.
  Trim order: zone-B day lines (farthest first) → zone-A farthest. Budget
  ~4000 chars (safety net).

Day boundary: natural midnight (configured tz). All DB timestamps are UTC;
local timezone applied on render via timeutil.
"""
from __future__ import annotations

import datetime as _dt
import hashlib as _hashlib
import re as _re
import sqlite3
from . import config as _config

_TZ = _config.get_tz()

# Matches leading HH:MM in a LIFE line (e.g. "21:40 买了b5精华")
_LIFE_TS_RE = _re.compile(r"^(\d{2}:\d{2})(?:-\d{2}:\d{2})?(?:\s+|(?=【))(.*)", _re.DOTALL)
_BUDGET = 4000         # soft char budget (safety net; zone caps control sizing)
_INJECT_CAP = 20       # max film-strip lines injected into context


# ── helpers ─────────────────────────────────────────────────────────────────

def _now_local() -> _dt.datetime:
    return _dt.datetime.now(_TZ)


def _calendar_date_from_utc(utc_iso: str) -> _dt.date:
    """UTC ISO → local calendar date (configured tz), natural midnight."""
    s = (utc_iso or "").strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return _now_local().date()
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


def _hhmm_local(utc_iso: str) -> str:
    """UTC ISO → local HH:MM display (configured tz)."""
    s = (utc_iso or "").strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return "??:??"
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_TZ).strftime("%H:%M")


def _period_of_hhmm(hhmm: str) -> str:
    """HH:MM → AM/PM/ND period label (display only, not day assignment).
    AM 06-12, PM 12-18, ND 18-06. 00-06 keeps the ND label of its own day.
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
    Natural midnight: diary_date = local calendar date. Period is a display
    label (AM/PM/ND); 00-06 keeps the ND label of that same calendar day.
    """
    s = (utc_iso or "").strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        today = _now_local().date()
        return today, "ND"
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    local = d.astimezone(_TZ)
    period = _period_of_hhmm(local.strftime("%H:%M"))
    return local.date(), period


def _tl_anchor_sid(sid: str, segment_seq: int = 0,
                   line_index: int | None = None) -> str:
    if line_index is not None:
        return f"<!-- tl:{sid}:{segment_seq}:{line_index} -->"
    if segment_seq > 0:
        return f"<!-- tl:{sid}:{segment_seq} -->"
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
        diary_date = _calendar_date_from_utc(session_utc_iso)
        return session_utc_iso, diary_date

    hhmm = m.group(1)
    try:
        h, mi = int(hhmm[:2]), int(hhmm[3:5])
    except ValueError:
        diary_date = _calendar_date_from_utc(session_utc_iso)
        return session_utc_iso, diary_date

    sess_dt = _parse_utc(session_utc_iso)
    if sess_dt is None:
        diary_date = _calendar_date_from_utc(session_utc_iso)
        return session_utc_iso, diary_date
    sess_local = sess_dt.astimezone(_TZ)

    cal_date = sess_local.date()
    candidate = _dt.datetime(cal_date.year, cal_date.month, cal_date.day,
                             h, mi, 0, tzinfo=_TZ)

    return _utc_iso(candidate), candidate.date()


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
    # Build candidate on the given calendar date; natural midnight boundary.
    candidate = _dt.datetime(
        session_date.year, session_date.month, session_date.day,
        h, mi, 0, tzinfo=_TZ)
    return candidate.date()


# ── DB queries ───────────────────────────────────────────────────────────────

def _query_digests_range(conn: sqlite3.Connection,
                         from_utc: str, to_utc: str) -> list[dict]:
    """Visible digests with sd.ts in a generous range for Python filtering.

    sd.date is intentionally ignored; it can be stale for cross-day sessions.
    """
    from_dt = _dt.datetime.fromisoformat(from_utc.replace("Z", "+00:00"))
    ts_floor = (from_dt - _dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT sd.sid, sd.segment_seq, sd.date, sd.text, sd.kind, sd.life_lines,"
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


def _query_diary_zone_b(conn: sqlite3.Connection,
                        dates: list[_dt.date]) -> dict[str, dict]:
    """diary.tone + diary.overview keyed by date string, for zone B."""
    if not dates:
        return {}
    placeholders = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT date, tone, overview FROM diary"
        f" WHERE date IN ({placeholders}) AND tl_hidden = 0",
        [d.isoformat() for d in dates],
    ).fetchall()
    result: dict[str, dict] = {}
    for r in rows:
        overview = (r["overview"] or "").strip()
        if not overview:
            continue  # skip days with NULL overview
        result[r["date"]] = {
            "tone": (r["tone"] or "").strip() or "平淡",
            "overview": overview,
        }
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



def _query_self_rows_24h(conn: sqlite3.Connection,
                         from_utc: str, to_utc: str) -> list[dict]:
    """Self timeline rows (role='tl', tl_add) in the 24h window.

    content already carries the 【label】body phrase verbatim; the range prefix
    is composed from ts_start/ts_end. Returns one dict per row with a
    pre-composed display line (HH:mm[-HH:mm] 【label】body).
    """
    rows = conn.execute(
        "SELECT e.id AS id, e.ts_start AS ts_start, e.ts_end AS ts_end,"
        " e.timestamp AS ts, e.content AS body"
        " FROM events e"
        " WHERE e.role='tl' AND e.timestamp >= ? AND e.timestamp < ?"
        " ORDER BY e.timestamp ASC",
        (from_utc, to_utc),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        ts_start = r["ts_start"] or r["ts"]
        hhmm = _hhmm_local(ts_start)
        end = r["ts_end"]
        rng = f"{hhmm}-{_hhmm_local(end)}" if end else hhmm
        body = (r["body"] or "").strip()
        out.append({
            "id": r["id"],
            "ts": ts_start,
            "hhmm": hhmm,
            "composed": f"{rng} {body}".rstrip(),
        })
    return out


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

def _render_24h(digests: list[dict],
                current_sid: str | None,
                manual_events: list[dict] | None = None,
                from_utc: str | None = None,
                to_utc: str | None = None,
                event_spans: dict[str, tuple[str | None, str | None]] | None = None,
                exclude_full_session_sids: set[str] | None = None,
                self_rows: list[dict] | None = None,
                ) -> tuple[list[str], list[dict]]:
    """Flat 24h film-strip, newest first, filtered per rendered line.

    self_rows (role='tl', tl_add) render PRIMARY with a tl:e anchor and
    the 【<u> word♡<a> word】body [i] format (markers from persona config);
    life_lines are the history fallback.
    """
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
            "segment_seq": sd.get("segment_seq", 0),
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
            context_date = context_dt.astimezone(_TZ).date()
            span_start = span_end = None
            dates = (context_date, context_date - _dt.timedelta(days=1))
        else:
            span_start = span_start_utc.astimezone(_TZ)
            span_end = span_end_utc.astimezone(_TZ)
            days = (span_end.date() - span_start.date()).days
            dates = tuple(span_start.date() + _dt.timedelta(days=i)
                          for i in range(days + 1))

        candidates = [
            _dt.datetime(d.year, d.month, d.day, h, mi, 0, tzinfo=_TZ)
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
        sess_hhmm = _hhmm_local(ts)
        life_raw = sd.get("life_lines") or ""
        life_items = [x.strip() for x in life_raw.splitlines() if x.strip()]

        if not life_items:
            continue
        else:
            for idx, item in enumerate(life_items):
                line_hhmm, text = _life_line_hhmm(item, sess_hhmm)
                if _re.match(r"^\d{2}:\d{2}(?:-\d{2}:\d{2})?[\s【]", item):
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
            "hhmm": _hhmm_local(ts),
            "text": content,
        })

    for sr in (self_rows or []):
        ts_dt = _in_window(sr.get("ts") or "")
        composed = (sr.get("composed") or "").strip()
        if ts_dt is None or not composed:
            continue
        entries.append({
            "ts": ts_dt,
            "local_date": ts_dt.astimezone(_TZ).date(),
            "sid": None,
            "event_id": sr["id"],
            "line_index": None,
            "hhmm": sr.get("hhmm") or _hhmm_local(sr.get("ts") or ""),
            "text": composed,
            "self_row": True,
        })

    entries.sort(key=lambda e: e["ts"], reverse=True)

    lines: list[str] = []
    for cal_date in sorted({entry["local_date"] for entry in entries}, reverse=True):
        lines.append(f"**{cal_date.strftime('%m-%d')} {cal_date.strftime('%a')}**")
        for entry in (e for e in entries if e["local_date"] == cal_date):
            hhmm = entry["hhmm"]
            text = entry["text"]
            sid = entry.get("sid")
            if sid is None:
                if entry.get("self_row"):
                    # composed already carries HH:mm[-HH:mm] 【label】body
                    lines.append(f"{text} <!-- tl:e:{entry['event_id']} -->")
                else:
                    lines.append(f"{hhmm} {text} <!-- tl:e:{entry['event_id']} -->")
                continue

            segment_seq = entry.get("segment_seq", 0)
            anchor = f" {_tl_anchor_sid(sid, segment_seq, line_index=entry['line_index'])}"
            if _re.match(r"^\d{2}:\d{2}(?:-\d{2}:\d{2})?[\s【]", text):
                lines.append(f"{text}{anchor}")
            else:
                lines.append(f"{hhmm} {text}{anchor}")

    return lines, []


def _render_zone_b(diary_data: dict[str, dict],
                   dates: list[_dt.date]) -> list[str]:
    """Zone B: per-day diary overview.
    Returns [] if there is no diary data (no days rendered).
    """
    day_lines: list[str] = []
    for date in sorted(dates, reverse=True):
        data = diary_data.get(date.isoformat())
        if not data:
            continue
        tone = data["tone"]
        overview = data["overview"]
        anchor = _tl_anchor_date(date.isoformat())
        day_lines.append(f"**{date.strftime('%m-%d')} {date.strftime('%a')} 【{tone}】** {anchor}")
        day_lines.append(overview)

    return day_lines


# ── main render ──────────────────────────────────────────────────────────────

def render_timeline(conn: sqlite3.Connection,
                    inject_cap: int | None = None) -> str:
    """Render the ## Timeline block.

    Uses UTC boundaries for DB queries; configured local timezone for display.
    Never naive datetime. Returns empty string if DB is empty/cold.
    When inject_cap is set, Zone A is truncated to that many entries.
    """
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    now_utc_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_local = now_utc.astimezone(_TZ)
    yesterday_start_utc = _dt.datetime.combine(
        (now_local - _dt.timedelta(days=1)).date(),
        _dt.time.min,
        tzinfo=_TZ,
    ).astimezone(_dt.timezone.utc)
    yesterday_start_utc_iso = _utc_iso(yesterday_start_utc)

    current_sid = _query_current_sid(conn)

    # ── last 24h ─────────────────────────────────────────────────────────────
    digests_24h = _query_digests_range(conn, yesterday_start_utc_iso, now_utc_iso)
    event_spans_24h = {
        d["sid"]: _query_session_event_span(conn, d["sid"])
        for d in digests_24h
    }
    manual_24h = _query_manual_events_24h(conn, yesterday_start_utc_iso, now_utc_iso)
    self_24h = _query_self_rows_24h(conn, yesterday_start_utc_iso, now_utc_iso)
    lines_24h, _overflow_24h = _render_24h(
        digests_24h, current_sid, manual_24h,
        from_utc=yesterday_start_utc_iso, to_utc=now_utc_iso,
        event_spans=event_spans_24h,
        self_rows=self_24h,
    )
    if inject_cap is not None:
        lines_24h = lines_24h[:inject_cap]

    # ── zone B: 3 diary days before zone A start ──────────────────────────────
    zone_a_start_date = (now_local - _dt.timedelta(days=1)).date()
    zone_b_dates = [zone_a_start_date - _dt.timedelta(days=d) for d in range(1, 4)]
    diary_data = _query_diary_zone_b(conn, zone_b_dates)

    lines_zone_b = _render_zone_b(diary_data, zone_b_dates)

    # ── assemble + trim to budget ────────────────────────────────────────────
    all_sections = _assemble(lines_24h, lines_zone_b)
    text = "## Timeline\n" + "\n".join(all_sections) if all_sections else "## Timeline\n_none_"

    # Trim if over budget (visible text only — edit anchors don't count)
    if _visible_len(text) > _BUDGET:
        text = _trim_to_budget(text, lines_24h, lines_zone_b)

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
    # t= = moment timeline block content last changed; render_timeline stays
    # pure and stamps now — carry_trail_t (writer-side) rewrites it to the
    # carried value when the content is unchanged. Only stamped when the block
    # has rendered anchors (empty `_none_` render emits no trail at all).
    if parts:
        # z= = fingerprint of the zone body (this text, trail-free). Lets
        # reconcile tell render residue (z= matches recompute → DB wins
        # silently) from a human edit (z= differs → warn) without relying on
        # file mtime, which a volatile co-writer (Status zone) bumps.
        parts.append("z=" + _zone_fingerprint(text))
        parts.append("t=" + _now_utc_iso())
        text += f"\n<!-- tl-rendered:{';'.join(parts)} -->"

    return text


def _now_utc_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_TL_TRAIL_LINE_RE = _re.compile(r"\n?<!--\s*tl-rendered:[^>]+-->\s*$")
_TL_TRAIL_T_RE = _re.compile(r"t=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
_TL_TRAIL_Z_RE = _re.compile(r"z=([0-9a-f]{8})")


def _zone_fingerprint(text: str) -> str:
    """Deterministic 8-hex fingerprint of the timeline zone's content.

    Shared by render (stamps z= into the trail) and reconcile (recomputes on
    the current file zone). Excludes the tl-rendered trail line itself and
    normalizes newlines + trailing whitespace so both sides are byte-identical
    by construction, independent of the volatile t= / z= trail fields.
    """
    body = _TL_TRAIL_LINE_RE.sub("", text)
    body = body.replace("\r\n", "\n").replace("\r", "\n").rstrip()
    return _hashlib.sha1(body.encode("utf-8")).hexdigest()[:8]


def carry_trail_t(new_block: str, old_block: str | None,
                  absorbed: bool = False) -> str:
    """Writer-side trail-t reconciliation.

    Compare new vs old timeline block with the trail line stripped. Identical
    content → rewrite the new trail carrying the old t= (content unchanged).
    Different content, or no old t= → keep the fresh t=now already stamped by
    render_timeline. Returns the (possibly rewritten) new block.

    absorbed=True means the reconcile pass just wrote at least one edit into
    the DB, so the DB rows now carry a fresh mts (> the old t=). Keeping the
    old t= would deadlock the per-row db-win gate (row_ts > t= forever → every
    later edit of that row rejected). So on absorb, keep the fresh t=now even
    when the rendered content equals the old file content (the reconcile made
    the file match the DB, but the DB moved).
    """
    if old_block is None:
        return new_block
    if absorbed:
        return new_block
    old_t = _TL_TRAIL_T_RE.search(_TL_TRAIL_LINE_RE.search(old_block).group(0)) \
        if _TL_TRAIL_LINE_RE.search(old_block) else None
    if old_t is None:
        return new_block
    new_body = _TL_TRAIL_LINE_RE.sub("", new_block)
    old_body = _TL_TRAIL_LINE_RE.sub("", old_block)
    if new_body != old_body:
        return new_block
    return _TL_TRAIL_T_RE.sub(f"t={old_t.group(1)}", new_block, count=1)


def _assemble(lines_24h: list[str],
              lines_zone_b: list[str]) -> list[str]:
    parts: list[str] = []
    parts.extend(lines_24h)
    if lines_zone_b:
        if parts:
            parts.append("")
        parts.extend(lines_zone_b)
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
                    lines_24h: list[str],
                    lines_zone_b: list[str]) -> str:
    lzb = list(lines_zone_b)
    l24h = list(lines_24h)

    def _rebuild() -> str:
        parts = _assemble(l24h, lzb)
        body = "\n".join(parts) if parts else "_none_"
        return "## Timeline\n" + body

    # Trim zone B lines (farthest day first = near end)
    while len(lzb) >= 2 and _visible_len(_rebuild()) > _BUDGET:
        # Remove last day entry (2 lines: header + overview)
        lzb.pop()  # overview
        lzb.pop()  # header

    # Remove all zone B if still over
    if lzb and _visible_len(_rebuild()) > _BUDGET:
        lzb = []

    # Trim 24h farthest
    while l24h and _visible_len(_rebuild()) > _BUDGET:
        l24h.pop()

    return _rebuild()
