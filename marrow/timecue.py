"""Time-cue parser: detect natural-language date references in prompt text.

Converts Melbourne-local cues (昨天, 上周X, N天前, etc.) to UTC ISO windows.
All output timestamps are UTC ISO strings; all day boundaries computed in Melbourne time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_MELB = ZoneInfo("Australia/Melbourne")

_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CN_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


@dataclass
class TimeCue:
    since_utc: str   # UTC ISO, inclusive start
    until_utc: str   # UTC ISO, exclusive end
    stripped: str    # prompt text with the matched cue phrase removed


def _melb_now(now: datetime | None) -> datetime:
    ref = now if now is not None else datetime.now(timezone.utc)
    return ref.astimezone(_MELB)


def _day_bounds(local_date, tz: ZoneInfo = _MELB) -> tuple[datetime, datetime]:
    """Return (start, end) aware UTC datetimes for a Melbourne calendar day."""
    start = datetime(local_date.year, local_date.month, local_date.day,
                     0, 0, 0, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def melb_day_range(date_str: str) -> tuple[str, str]:
    """Convert YYYY-MM-DD Melbourne day to (since_utc, until_utc) ISO strings."""
    from datetime import date as _date
    d = _date.fromisoformat(date_str)
    s, e = _day_bounds(d)
    return _fmt(s), _fmt(e)


def _strip_and_collapse(text: str, match: re.Match) -> str:
    before = text[:match.start()].rstrip()
    after = text[match.end():].lstrip()
    if before and after:
        return before + " " + after
    return (before + after).strip()


def _cn_to_int(s: str) -> int | None:
    """Parse a short CN numeral string (e.g. (七), (十), (二十)) or arabic digit string."""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if len(s) == 1 and s in _CN_DIGIT:
        return _CN_DIGIT[s]
    if s == "十":
        return 10
    if len(s) == 2 and s[0] == "十" and s[1] in _CN_DIGIT:
        return 10 + _CN_DIGIT[s[1]]
    if len(s) == 2 and s[0] in _CN_DIGIT and s[1] == "十":
        return _CN_DIGIT[s[0]] * 10
    if len(s) == 3 and s[0] in _CN_DIGIT and s[1] == "十" and s[2] in _CN_DIGIT:
        return _CN_DIGIT[s[0]] * 10 + _CN_DIGIT[s[2]]
    return None


# ── pattern list — ordered; first match wins ─────────────────────────────────
# Each entry: (compiled_regex, handler(match, now_melb) -> (since, until) or None)
# Handler returns None to signal "future cue → skip".

def _h_yesterday(m: re.Match, now_melb: datetime):
    d = (now_melb - timedelta(days=1)).date()
    s, e = _day_bounds(d)
    return _fmt(s), _fmt(e)


def _h_today(m: re.Match, now_melb: datetime):
    s, e = _day_bounds(now_melb.date())
    return _fmt(s), _fmt(e)


def _h_qiantian(m: re.Match, now_melb: datetime):
    d = (now_melb - timedelta(days=2)).date()
    s, e = _day_bounds(d)
    return _fmt(s), _fmt(e)


def _h_daqiantian(m: re.Match, now_melb: datetime):
    d = (now_melb - timedelta(days=3)).date()
    s, e = _day_bounds(d)
    return _fmt(s), _fmt(e)


def _h_n_days_ago_cn(m: re.Match, now_melb: datetime):
    n = _cn_to_int(m.group(1))
    if n is None or n < 1 or n > 30:
        return None
    d = (now_melb - timedelta(days=n)).date()
    s, e = _day_bounds(d)
    return _fmt(s), _fmt(e)


def _h_n_days_ago_en(m: re.Match, now_melb: datetime):
    n = int(m.group(1))
    if n < 1 or n > 30:
        return None
    d = (now_melb - timedelta(days=n)).date()
    s, e = _day_bounds(d)
    return _fmt(s), _fmt(e)


def _h_last_week(m: re.Match, now_melb: datetime):
    # Previous Mon-Sun full week
    today = now_melb.date()
    this_mon = today - timedelta(days=today.weekday())
    prev_mon = this_mon - timedelta(days=7)
    prev_sun = prev_mon + timedelta(days=6)
    s, _ = _day_bounds(prev_mon)
    _, e = _day_bounds(prev_sun)
    return _fmt(s), _fmt(e)


def _h_last_week_day(m: re.Match, now_melb: datetime):
    # (上周X / 上星期X): that specific weekday in the previous week
    wd = _CN_WEEKDAY.get(m.group(1))
    if wd is None:
        return None
    today = now_melb.date()
    this_mon = today - timedelta(days=today.weekday())
    prev_mon = this_mon - timedelta(days=7)
    target = prev_mon + timedelta(days=wd)
    s, e = _day_bounds(target)
    return _fmt(s), _fmt(e)


def _h_this_week(m: re.Match, now_melb: datetime):
    # This week Mon..today
    today = now_melb.date()
    this_mon = today - timedelta(days=today.weekday())
    s, _ = _day_bounds(this_mon)
    _, e = _day_bounds(today)
    return _fmt(s), _fmt(e)


def _h_weekday_bare(m: re.Match, now_melb: datetime):
    # (周X / 星期X) no prefix: most recent past occurrence within last 7 days
    wd = _CN_WEEKDAY.get(m.group(1))
    if wd is None:
        return None
    today = now_melb.date()
    for delta in range(7):
        candidate = today - timedelta(days=delta)
        if candidate.weekday() == wd:
            s, e = _day_bounds(candidate)
            return _fmt(s), _fmt(e)
    return None


def _h_last_month(m: re.Match, now_melb: datetime):
    today = now_melb.date()
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    s, _ = _day_bounds(first_prev)
    _, e = _day_bounds(last_prev)
    return _fmt(s), _fmt(e)


def _h_month_day(m: re.Match, now_melb: datetime):
    month = int(m.group(1))
    day = int(m.group(2))
    today = now_melb.date()
    try:
        from datetime import date as _date
        candidate = _date(today.year, month, day)
        if candidate > today:
            candidate = _date(today.year - 1, month, day)
        s, e = _day_bounds(candidate)
        return _fmt(s), _fmt(e)
    except ValueError:
        return None


def _h_day_of_month(m: re.Match, now_melb: datetime):
    day = int(m.group(1))
    if day < 1 or day > 31:
        return None
    today = now_melb.date()
    try:
        from datetime import date as _date
        candidate = _date(today.year, today.month, day)
        if candidate > today:
            # Go to previous month
            first_this = today.replace(day=1)
            prev_last = first_this - timedelta(days=1)
            candidate = _date(prev_last.year, prev_last.month, day)
        s, e = _day_bounds(candidate)
        return _fmt(s), _fmt(e)
    except ValueError:
        return None


def _h_tomorrow(m: re.Match, now_melb: datetime):
    return None  # future → skip


def _h_next_week(m: re.Match, now_melb: datetime):
    return None  # future → skip


# Ordered list of (pattern, handler)
_PATTERNS: list[tuple[re.Pattern, object]] = [
    # Future cues — must come before bare (周X) to avoid partial match
    (re.compile(r"明天|明早|明晚|tomorrow", re.IGNORECASE), _h_tomorrow),
    (re.compile(r"下周|下星期|next\s+week", re.IGNORECASE), _h_next_week),

    # Specific past cues
    (re.compile(r"大前天"), _h_daqiantian),
    (re.compile(r"前天"), _h_qiantian),
    (re.compile(r"昨天|昨晚|昨夜|yesterday", re.IGNORECASE), _h_yesterday),
    (re.compile(r"今天|今早|今天早上|今晚|today", re.IGNORECASE), _h_today),

    # N days ago — CN numerals first (more specific)
    (re.compile(r"([一二两三四五六七八九十]{1,3})天前"), _h_n_days_ago_cn),
    (re.compile(r"(\d{1,2})\s*天前"), _h_n_days_ago_cn),
    (re.compile(r"(\d{1,2})\s*days?\s+ago", re.IGNORECASE), _h_n_days_ago_en),

    # Last week with specific day — must come before bare (上周)
    (re.compile(r"(?:上周|上星期)([一二三四五六日天])"), _h_last_week_day),

    # Whole last week
    (re.compile(r"上周|上星期|last\s+week", re.IGNORECASE), _h_last_week),

    # This week
    (re.compile(r"这周|本周|this\s+week", re.IGNORECASE), _h_this_week),

    # Bare weekday (no prefix) — within last 7 days
    (re.compile(r"(?:周|星期)([一二三四五六日天])"), _h_weekday_bare),

    # Last month
    (re.compile(r"上个月|last\s+month", re.IGNORECASE), _h_last_month),

    # X月X号 / X月X日
    (re.compile(r"(\d{1,2})月(\d{1,2})[号日]"), _h_month_day),

    # N号 alone (day of current month)
    (re.compile(r"(?<!\d)(\d{1,2})号(?!\d)"), _h_day_of_month),
]


def parse_time_cue(text: str, now: datetime | None = None) -> TimeCue | None:
    """Detect the first natural-language time cue in text (by position).

    Scans all patterns, picks the match at the earliest text position.
    Returns TimeCue(since_utc, until_utc, stripped) or None if no cue found
    or cue refers to the future.
    """
    now_melb = _melb_now(now)
    best_pos: int = len(text) + 1
    best_match: re.Match | None = None
    best_handler = None

    for pat, handler in _PATTERNS:
        m = pat.search(text)
        if m is None:
            continue
        if m.start() < best_pos:
            best_pos = m.start()
            best_match = m
            best_handler = handler

    if best_match is None:
        return None

    result = best_handler(best_match, now_melb)
    if result is None:
        return None  # future cue
    since, until = result
    stripped = _strip_and_collapse(text, best_match)
    return TimeCue(since_utc=since, until_utc=until, stripped=stripped)
    return None
