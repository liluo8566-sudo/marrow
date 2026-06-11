"""Tests for timecue.parse_time_cue — every cue pattern, UTC boundary correctness,
CN numerals, future-cue→None, stripped text, multi-cue first-match.

All tests inject `now` so results are deterministic (never naive datetime).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from marrow.timecue import TimeCue, parse_time_cue, melb_day_range

_MELB = ZoneInfo("Australia/Melbourne")


def _melb_now(date_str: str, time_str: str = "12:00:00") -> datetime:
    """Return an aware UTC datetime for a Melbourne local date+time string."""
    local = datetime.fromisoformat(f"{date_str}T{time_str}").replace(tzinfo=_MELB)
    return local.astimezone(timezone.utc)


def _since_until(date_str: str) -> tuple[str, str]:
    return melb_day_range(date_str)


# ── helpers ───────────────────────────────────────────────────────────────────

def _bounds(cue: TimeCue | None) -> tuple[str, str]:
    assert cue is not None
    return cue.since_utc, cue.until_utc


# Reference: Melbourne is UTC+10 (AEST) or UTC+11 (AEDT).
# 2026-06-10 is in AEST (UTC+10). 2026-01-10 is in AEDT (UTC+11).

NOW_AEST = _melb_now("2026-06-10")   # Wednesday, AEST
NOW_AEDT = _melb_now("2026-01-10")   # Saturday, AEDT


# ── basic single-day cues ─────────────────────────────────────────────────────

def test_yesterday_cn():
    cue = parse_time_cue("昨天说了什么", now=NOW_AEST)
    assert cue is not None
    s, e = _bounds(cue)
    assert s == melb_day_range("2026-06-09")[0]
    assert e == melb_day_range("2026-06-09")[1]
    assert "昨天" not in cue.stripped


def test_yesterday_en():
    cue = parse_time_cue("what did I say yesterday", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-09")[0]


def test_zuowan():
    cue = parse_time_cue("昨晚我们聊了什么", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-09")[0]
    assert "昨晚" not in cue.stripped


def test_today_cn():
    cue = parse_time_cue("今天心情如何", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-10")[0]


def test_jintian_zaoshang():
    cue = parse_time_cue("今天早上吃了什么", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-10")[0]


def test_today_en():
    cue = parse_time_cue("remind me today's tasks", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-10")[0]


def test_qiantian():
    cue = parse_time_cue("前天发生了什么", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-08")[0]


def test_daqiantian():
    cue = parse_time_cue("大前天的事", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-07")[0]


# ── N天前 / N days ago ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,n", [
    ("3天前", 3),
    ("7天前", 7),
    ("一天前", 1),
    ("两天前", 2),
    ("三天前", 3),
    ("七天前", 7),
    ("十天前", 10),
])
def test_n_days_ago_cn(text, n):
    cue = parse_time_cue(text, now=NOW_AEST)
    assert cue is not None
    expected_date = (datetime(2026, 6, 10, tzinfo=_MELB) - timedelta(days=n)).strftime("%Y-%m-%d")
    assert cue.since_utc == melb_day_range(expected_date)[0]


def test_n_days_ago_en():
    cue = parse_time_cue("5 days ago", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-05")[0]


def test_n_days_ago_singular():
    cue = parse_time_cue("1 day ago", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-09")[0]


def test_n_days_ago_over_30_returns_none():
    cue = parse_time_cue("31天前", now=NOW_AEST)
    assert cue is None


# ── last week ─────────────────────────────────────────────────────────────────

def test_last_week_cn():
    # NOW_AEST = 2026-06-10 Wed; prev week Mon=2026-06-01, Sun=2026-06-07
    cue = parse_time_cue("上周说了什么", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-01")[0]
    assert cue.until_utc == melb_day_range("2026-06-07")[1]


def test_last_week_en():
    cue = parse_time_cue("last week", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-01")[0]
    assert cue.until_utc == melb_day_range("2026-06-07")[1]


@pytest.mark.parametrize("char,expected_date", [
    ("一", "2026-06-01"),  # Monday
    ("三", "2026-06-03"),  # Wednesday
    ("五", "2026-06-05"),  # Friday
    ("六", "2026-06-06"),  # Saturday
    ("日", "2026-06-07"),  # Sunday
    ("天", "2026-06-07"),  # Sunday (alias)
])
def test_last_week_specific_day(char, expected_date):
    cue = parse_time_cue(f"上周{char}的事", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range(expected_date)[0]
    assert cue.until_utc == melb_day_range(expected_date)[1]


# ── this week ─────────────────────────────────────────────────────────────────

def test_this_week():
    # NOW_AEST = 2026-06-10 Wed; this week Mon=2026-06-08..today=2026-06-10
    cue = parse_time_cue("这周发生了什么", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-08")[0]
    assert cue.until_utc == melb_day_range("2026-06-10")[1]


def test_this_week_en():
    cue = parse_time_cue("this week", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-08")[0]


# ── bare weekday ──────────────────────────────────────────────────────────────

def test_bare_weekday_past():
    # NOW_AEST = 2026-06-10 Wednesday (weekday=2)
    # 周一 = Monday = 2026-06-08 (2 days ago, within 7)
    cue = parse_time_cue("周一发生了什么", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-08")[0]


def test_bare_weekday_today():
    # 周三 = Wednesday = today 2026-06-10
    cue = parse_time_cue("周三吃了什么", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-10")[0]


def test_xingqi_format():
    # 星期五 = Friday; NOW_AEST is Wednesday, so most recent Friday = 2026-06-05
    cue = parse_time_cue("星期五去哪了", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-05")[0]


# ── last month ────────────────────────────────────────────────────────────────

def test_last_month():
    # NOW_AEST = 2026-06-10; prev month = May 2026 (2026-05-01..2026-05-31)
    cue = parse_time_cue("上个月的事", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-05-01")[0]
    assert cue.until_utc == melb_day_range("2026-05-31")[1]


def test_last_month_en():
    cue = parse_time_cue("last month", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-05-01")[0]


# ── X月X号 / X月X日 ────────────────────────────────────────────────────────────

def test_month_day_past():
    # 3月15号 in 2026 is past (now=Jun)
    cue = parse_time_cue("3月15号发生了什么", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-03-15")[0]


def test_month_day_future_falls_prev_year():
    # 12月25号 in 2026 is in the future (now=Jun 2026) → use 2025
    cue = parse_time_cue("12月25号的事", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2025-12-25")[0]


def test_month_day_ri_format():
    cue = parse_time_cue("4月1日发生了什么", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-04-01")[0]


# ── N号 (day of month) ────────────────────────────────────────────────────────

def test_day_of_month_past():
    # 5号 in Jun 2026 is past (now=10th)
    cue = parse_time_cue("5号发生的事", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-05")[0]


def test_day_of_month_future_prev_month():
    # 15号 in Jun 2026 is future (now=10th) → May 15
    cue = parse_time_cue("15号那天", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-05-15")[0]


# ── future cues → None ────────────────────────────────────────────────────────

def test_tomorrow_returns_none():
    cue = parse_time_cue("明天的计划", now=NOW_AEST)
    assert cue is None


def test_minzao_returns_none():
    cue = parse_time_cue("明早出门", now=NOW_AEST)
    assert cue is None


def test_next_week_returns_none():
    cue = parse_time_cue("下周计划", now=NOW_AEST)
    assert cue is None


def test_next_week_en_returns_none():
    cue = parse_time_cue("next week plans", now=NOW_AEST)
    assert cue is None


# ── stripped text ─────────────────────────────────────────────────────────────

def test_stripped_removes_cue():
    cue = parse_time_cue("你还记得昨天我们聊的灭绝师太", now=NOW_AEST)
    assert cue is not None
    assert "昨天" not in cue.stripped
    assert "灭绝师太" in cue.stripped


def test_stripped_whitespace_collapsed():
    cue = parse_time_cue("  昨天  吃了拉面  ", now=NOW_AEST)
    assert cue is not None
    assert cue.stripped == "吃了拉面"


def test_stripped_cue_at_end():
    cue = parse_time_cue("灭绝师太是昨天", now=NOW_AEST)
    assert cue is not None
    assert "昨天" not in cue.stripped
    assert "灭绝师太" in cue.stripped


def test_stripped_only_cue_is_empty():
    cue = parse_time_cue("昨天", now=NOW_AEST)
    assert cue is not None
    assert cue.stripped == ""


# ── multi-cue: first match wins ───────────────────────────────────────────────

def test_multi_cue_first_wins():
    # (昨天) comes before (前天) in text;昨天 is matched first
    cue = parse_time_cue("昨天还是前天来着", now=NOW_AEST)
    assert cue is not None
    assert cue.since_utc == melb_day_range("2026-06-09")[0]


# ── AEST↔UTC boundary correctness ────────────────────────────────────────────

def test_aest_boundary_melbourne_midnight():
    # A Melbourne 00:30 AEST event = UTC 2026-06-09 14:30 (previous UTC day)
    # (昨天) window should include UTC 14:30 on 2026-06-09
    cue = parse_time_cue("昨天发生的事", now=NOW_AEST)
    assert cue is not None
    # 2026-06-09 00:00 AEST = 2026-06-08 14:00 UTC
    assert "2026-06-08T14:00:00Z" == cue.since_utc
    # 2026-06-10 00:00 AEST = 2026-06-09 14:00 UTC
    assert "2026-06-09T14:00:00Z" == cue.until_utc


def test_aedt_boundary():
    # AEDT is UTC+11; 2026-01-10 00:00 AEDT = 2025-01-09 13:00 UTC
    cue = parse_time_cue("昨天发生的事", now=NOW_AEDT)
    assert cue is not None
    # 2026-01-09 00:00 AEDT = 2026-01-08 13:00 UTC
    assert cue.since_utc == "2026-01-08T13:00:00Z"
    assert cue.until_utc == "2026-01-09T13:00:00Z"


# ── melb_day_range ────────────────────────────────────────────────────────────

def test_melb_day_range_aest():
    s, e = melb_day_range("2026-06-10")
    assert s == "2026-06-09T14:00:00Z"
    assert e == "2026-06-10T14:00:00Z"


def test_melb_day_range_aedt():
    s, e = melb_day_range("2026-01-10")
    assert s == "2026-01-09T13:00:00Z"
    assert e == "2026-01-10T13:00:00Z"


# ── no cue → None ────────────────────────────────────────────────────────────

def test_no_cue_returns_none():
    assert parse_time_cue("你好吗", now=NOW_AEST) is None
    assert parse_time_cue("hello there", now=NOW_AEST) is None
    assert parse_time_cue("", now=NOW_AEST) is None
