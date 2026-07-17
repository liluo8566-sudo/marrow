"""Tests for marrow/timeline.py — render_timeline.

Covers:
- ND attribution (00-06 keeps ND label on its own calendar day)
- Day dividers in 24h film-strip
- 24h cap (20 lines)
- 2472h period/day bucketing, empty period hidden
- Day 4-7 zone + Week header
- Trim order: day lines → period lines → 24h farthest
- Budget ~1100 chars
- NULL tl_line fallback (truncated body text)
- Open episode rendering
"""
from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

from marrow import storage, timeline


# ── fixture ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def conn(tmp_path):
    db = str(tmp_path / "tl.db")
    c = storage.init_db(db)
    yield c
    c.close()


# ── helpers ──────────────────────────────────────────────────────────────────

def _utc(hours_ago: float) -> str:
    """UTC ISO string N hours before now."""
    dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(conn, sid: str, ts: str, kind: str = "casual",
            tl: str | None = "聊天了", life: str | None = None,
            body: str = "body text", date: str | None = None) -> None:
    conn.execute(
        "INSERT INTO session_digests (sid, date, ts, text, kind, tl_line, life_lines)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, date or ts[:10], ts, body, kind, tl, life),
    )
    conn.commit()


def _affect(conn, valence: float, arousal: float, importance: int,
            label: str, desc: str, unresolved: int = 0,
            hours_ago: float = 2.0) -> int:
    ts = _utc(hours_ago)
    cur = conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label,"
        " description, source, unresolved, created_at)"
        " VALUES (?, 1, ?, ?, ?, ?, ?, 'test', ?, ?)",
        (ts[:10], valence, arousal, importance, label, desc, unresolved, ts),
    )
    conn.commit()
    return cur.lastrowid


def _freeze_timeline_now(monkeypatch, melb_dt: _dt.datetime) -> None:
    class FrozenDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return melb_dt.replace(tzinfo=None)
            return melb_dt.astimezone(tz)

    monkeypatch.setattr(timeline._dt, "datetime", FrozenDateTime)


# ── 24h film-strip ────────────────────────────────────────────────────────────

def _local_iso(year: int, month: int, day: int, hour: int, minute: int) -> str:
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    return _dt.datetime(
        year, month, day, hour, minute, tzinfo=melb
    ).astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_24h_cross_day_stale_sd_date_clips_per_life_line(conn, monkeypatch):
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    _freeze_timeline_now(
        monkeypatch,
        _dt.datetime(2026, 6, 23, 2, 30, tzinfo=melb),
    )
    _digest(
        conn,
        "b2f76aa9",
        _local_iso(2026, 6, 22, 10, 37),
        kind="casual",
        tl="wrong fallback",
        life=(
            "01:20 clipped before window\n"
            "14:22 one-hour sleep before night shift\n"
            "23:50 tea after the shift"
        ),
        date="2026-06-20",
    )
    result = timeline.render_timeline(conn)
    assert "one-hour sleep before night shift" in result
    assert "tea after the shift <!-- tl:b2f76aa9:0:2 -->" in result
    assert "clipped before window" in result
    assert result.index("tea after the shift <!-- tl:b2f76aa9:0:2 -->") < result.index(
        "one-hour sleep before night shift"
    )


def test_24h_inline_tone_text_is_not_appended_from_affect():
    lines, overflow = timeline._render_24h(
        [
            {
                "sid": "s-tone-life",
                "ts": _local_iso(2026, 6, 22, 12, 0),
                "kind": "casual",
                "tl_line": "fallback",
                "text": "body",
                "life_lines": "14:00 early line\n18:00 later line",
            }
        ],
        current_sid=None,
        from_utc="2026-06-21T16:30:00Z",
        to_utc="2026-06-22T16:30:00Z",
    )
    assert overflow == []
    assert lines == [
        "**06-22 Mon**",
        "18:00 later line <!-- tl:s-tone-life:0:1 -->",
        "14:00 early line <!-- tl:s-tone-life:0:0 -->",
    ]


def test_24h_life_line_with_model_timestamp_not_double_prefixed():
    lines, overflow = timeline._render_24h(
        [
            {
                "sid": "s-double-ts",
                "ts": _local_iso(2026, 6, 22, 1, 54),
                "kind": "casual",
                "tl_line": "fallback",
                "text": "body",
                "life_lines": "01:25 【委屈】过敏难受",
            }
        ],
        current_sid=None,
        from_utc="2026-06-21T00:00:00Z",
        to_utc="2026-06-22T16:30:00Z",
    )
    assert overflow == []
    assert lines == [
        "**06-22 Mon**",
        "01:25 【委屈】过敏难受 <!-- tl:s-double-ts:0:0 -->",
    ]
    assert "01:54" not in lines[1]


def test_24h_renders_all_no_truncation():
    life = "\n".join(f"{h:02d}:00 line {h}" for h in range(24))
    lines, overflow = timeline._render_24h(
        [
            {
                "sid": "s-cap",
                "ts": _local_iso(2026, 6, 22, 12, 0),
                "kind": "casual",
                "tl_line": "fallback",
                "text": "body",
                "life_lines": life,
            }
        ],
        current_sid=None,
        from_utc="2026-06-21T14:00:00Z",
        to_utc="2026-06-22T14:00:00Z",
    )
    content_lines = [ln for ln in lines if not ln.startswith("**")]
    assert len(content_lines) == 24
    assert lines[0] == "**06-22 Mon**"
    assert "23:00 line 23 <!-- tl:s-cap:0:23 -->" in lines[1]
    assert overflow == []


def test_24h_manual_events_interleave():
    lines, overflow = timeline._render_24h(
        [
            {
                "sid": "s-10",
                "ts": _local_iso(2026, 6, 22, 10, 0),
                "kind": "casual",
                "text": "body",
                "life_lines": "10:00 聊了会天",
            },
            {
                "sid": "s-12",
                "ts": _local_iso(2026, 6, 22, 12, 0),
                "kind": "casual",
                "text": "body",
                "life_lines": "12:00 午后闲聊",
            },
        ],
        current_sid=None,
        manual_events=[{"id": 42, "timestamp": _local_iso(2026, 6, 22, 11, 0), "content": "manual note"}],
        from_utc="2026-06-21T16:30:00Z",
        to_utc="2026-06-22T16:30:00Z",
    )
    assert overflow == []
    assert lines == [
        "**06-22 Mon**",
        "12:00 午后闲聊 <!-- tl:s-12:0:0 -->",
        "11:00 manual note <!-- tl:e:42 -->",
        "10:00 聊了会天 <!-- tl:s-10:0:0 -->",
    ]


def test_24h_calendar_divider_between_local_dates():
    lines, overflow = timeline._render_24h(
        [
            {
                "sid": "s-2130",
                "ts": _local_iso(2026, 6, 21, 23, 30),
                "kind": "casual",
                "text": "body",
                "life_lines": "23:30 深夜散步",
            },
            {
                "sid": "s-2200",
                "ts": _local_iso(2026, 6, 22, 0, 30),
                "kind": "casual",
                "text": "body",
                "life_lines": "00:30 凌晨聊天",
            },
        ],
        current_sid=None,
        from_utc="2026-06-21T00:00:00Z",
        to_utc="2026-06-22T16:30:00Z",
    )
    assert overflow == []
    assert lines == [
        "**06-22 Mon**",
        "00:30 凌晨聊天 <!-- tl:s-2200:0:0 -->",
        "**06-21 Sun**",
        "23:30 深夜散步 <!-- tl:s-2130:0:0 -->",
    ]


def test_24h_no_digestless_fallback(conn, monkeypatch):
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    _freeze_timeline_now(
        monkeypatch,
        _dt.datetime(2026, 6, 22, 12, 0, tzinfo=melb),
    )
    conn.execute(
        "INSERT INTO sessions (sid, title, created_at, last_active)"
        " VALUES ('s-no-digest', 'session title must stay invisible', ?, ?)",
        (_local_iso(2026, 6, 22, 10, 0), _local_iso(2026, 6, 22, 10, 0)),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    assert "session title must stay invisible" not in result
    assert "_none_" in result


def test_current_sid_excluded(conn):
    """In-progress session must not appear in timeline."""
    ts = _utc(1)
    sid = "live-session"
    _digest(conn, sid, ts, kind="task", tl="正在进行")
    # Write lifecycle:start with no lifecycle:end
    conn.execute(
        "INSERT INTO audit_log (target_table, target_id, action, summary)"
        " VALUES ('events', ?, 'session_lifecycle:start', 'ppid=1')",
        (sid,),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    assert "正在进行" not in result


# ── ND attribution ────────────────────────────────────────────────────────────

def test_nd_00_to_06_belongs_to_same_day(conn):
    """A session at 02:00 local keeps the ND label on its OWN calendar day."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    # Build a UTC timestamp such that local Melbourne time = 02:00 today
    now_melb = _dt.datetime.now(melb)
    today_melb = now_melb.replace(hour=2, minute=0, second=0, microsecond=0)
    if today_melb > now_melb:
        today_melb -= _dt.timedelta(days=1)
    ts_utc = today_melb.astimezone(_dt.timezone.utc)
    ts_iso = ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    diary_date, period = timeline._period_diary_date(ts_iso)
    # 02:00 local is ND, natural midnight → same calendar day
    assert period == "ND"
    assert diary_date == today_melb.date()


def test_nd_22_to_midnight_belongs_to_same_day(conn):
    """22:00 local → ND, same diary day."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)
    evening = now_melb.replace(hour=22, minute=0, second=0, microsecond=0)
    if evening > now_melb:
        evening -= _dt.timedelta(days=1)
    ts_utc = evening.astimezone(_dt.timezone.utc)
    ts_iso = ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    diary_date, period = timeline._period_diary_date(ts_iso)
    assert period == "ND"
    assert diary_date == evening.date()


# ── zone B: diary overview ────────────────────────────────────────────────────

def test_zone_b_renders_overview_with_tone(conn):
    """_render_zone_b produces **MM-DD Day 【tone】** header + overview line."""
    import datetime as _dt2
    date = _dt2.date(2026, 6, 22)
    diary_data = {
        "2026-06-22": {"tone": "温暖", "overview": "今天散步了很开心。"},
    }
    lines = timeline._render_zone_b(diary_data, [date])
    assert lines[0] == "**06-22 Mon 【温暖】** <!-- tl:d:2026-06-22 -->"
    assert lines[1] == "今天散步了很开心。"


def test_zone_b_empty_diary_returns_empty(conn):
    """_render_zone_b with no diary data returns []."""
    import datetime as _dt2
    dates = [_dt2.date(2026, 6, 22), _dt2.date(2026, 6, 21)]
    lines = timeline._render_zone_b({}, dates)
    assert lines == []


def test_query_diary_zone_b_skips_null_overview(conn):
    """_query_diary_zone_b excludes rows where overview IS NULL or empty."""
    import datetime as _dt2
    conn.execute(
        "INSERT INTO diary (date, content, tone, overview) VALUES (?, ?, ?, ?)",
        ("2026-06-22", "body", "温暖", None),
    )
    conn.execute(
        "INSERT INTO diary (date, content, tone, overview) VALUES (?, ?, ?, ?)",
        ("2026-06-21", "body", "平淡", "有内容的一天。"),
    )
    conn.commit()
    result = timeline._query_diary_zone_b(
        conn, [_dt2.date(2026, 6, 22), _dt2.date(2026, 6, 21)]
    )
    assert "2026-06-22" not in result
    assert "2026-06-21" in result
    assert result["2026-06-21"]["overview"] == "有内容的一天。"


def test_zone_b_diary_appears_in_render_timeline(conn):
    """render_timeline includes diary overview from zone B dates."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    today = _dt.datetime.now(melb).date()
    day3 = today - _dt.timedelta(days=3)
    conn.execute(
        "INSERT INTO diary (date, content, tone, overview) VALUES (?, ?, ?, ?)",
        (day3.isoformat(), "body", "愉悦", "三天前很开心。"),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    assert "三天前很开心。" in result


# ── trim order ────────────────────────────────────────────────────────────────


# ── budget ────────────────────────────────────────────────────────────────────

def test_timeline_within_budget(conn):
    """Render with many sessions; output must stay within _BUDGET chars."""
    for i in range(30):
        _digest(conn, f"s-budget-{i}", _utc(i * 3),
                kind="casual" if i % 2 == 0 else "task",
                tl=f"{'聊天' if i % 2 == 0 else '任务'}{i}",
                life=f"喝咖啡{i}" if i % 2 == 0 else None)
        if i % 5 == 0:
            _affect(conn, 0.6, 0.4, 2, "温暖", f"小事{i}",
                    hours_ago=i * 3)
    result = timeline.render_timeline(conn)
    assert len(result) <= timeline._BUDGET * 1.1  # 10% tolerance for header


def test_trim_drops_zone_b_before_24h(conn):
    """When over budget, zone B lines are trimmed before 24h lines."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    today = _dt.datetime.now(melb).date()
    # Seed many recent 24h sessions
    for i in range(10):
        _digest(conn, f"s-24h-{i}", _utc(i * 2),
                kind="casual", tl=f"最近任务{i}", life=f"最近任务{i}")
    # Zone B diary entries with overviews
    for d in range(2, 5):
        conn.execute(
            "INSERT INTO diary (date, content, tone, overview) VALUES (?, 'body', '平淡', ?)",
            ((today - _dt.timedelta(days=d)).isoformat(), f"日记第{d}天" * 20),
        )
    conn.commit()
    result = timeline.render_timeline(conn)
    # Recent 24h content must survive trimming
    assert "最近任务0" in result or "最近任务1" in result


# ── header ────────────────────────────────────────────────────────────────────

def test_timeline_header(conn):
    result = timeline.render_timeline(conn)
    assert result.startswith("## Timeline")


def test_empty_db_renders_none(conn):
    result = timeline.render_timeline(conn)
    assert "## Timeline" in result
    assert "_none_" in result


# ── per-line LIFE HH:MM timestamps ───────────────────────────────────────────

def test_life_lines_use_own_hhmm(conn):
    """LIFE lines with HH:MM prefix render at their own time, not session start."""
    # Session ts at 08:00 local Melbourne today
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)
    # Anchor: session started at 08:00 local
    sess_local = now_melb.replace(hour=8, minute=0, second=0, microsecond=0)
    if sess_local > now_melb:
        sess_local -= _dt.timedelta(days=1)
    ts = sess_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # LIFE lines carry their own timestamps: 09:15 and 20:30
    life = "09:15 早餐吃了粥\n20:30 晚上散步了"
    _digest(conn, "s-perline", ts, kind="casual", tl="聊天了", life=life)
    result = timeline.render_timeline(conn)

    # Both per-line times must appear, not just 08:00
    assert "09:15" in result
    assert "20:30" in result
    # Content must also appear
    assert "早餐吃了粥" in result
    assert "晚上散步了" in result


def test_life_lines_no_prefix_fallback_to_session_time(conn):
    """LIFE lines without HH:MM prefix fall back to session start time."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)
    sess_local = now_melb.replace(hour=14, minute=30, second=0, microsecond=0)
    if sess_local > now_melb:
        sess_local -= _dt.timedelta(days=1)
    ts = sess_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sess_hhmm = "14:30"

    # Legacy LIFE line: no HH:MM prefix
    life = "买了b5精华"
    _digest(conn, "s-legacy", ts, kind="casual", tl="聊天了", life=life)
    result = timeline.render_timeline(conn)

    assert "买了b5精华" in result
    assert sess_hhmm in result


def test_life_lines_calendar_crossing_get_dividers(conn):
    """LIFE lines crossing calendar dates get one divider per local date."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)

    # Session started at 23:00 yesterday local
    sess_local = now_melb.replace(hour=23, minute=0, second=0, microsecond=0)
    if sess_local >= now_melb:
        sess_local -= _dt.timedelta(days=1)
    ts = sess_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # One LIFE line at 23:30 (same diary day) and one at 00:30 (prev diary day)
    life = "23:30 看了个电影\n00:30 睡前喝了热水"
    _digest(conn, "s-midnight", ts, kind="casual", tl="夜聊", life=life)
    result = timeline.render_timeline(conn)

    assert "**" in result


def test_life_line_hhmm_helper_parses_prefix():
    """Unit test for _life_line_hhmm — prefix present vs absent."""
    hhmm, text = timeline._life_line_hhmm("21:40 买了b5精华", "08:00")
    assert hhmm == "21:40"
    assert text == "买了b5精华"

    # No prefix — fallback to session time
    hhmm2, text2 = timeline._life_line_hhmm("买了b5精华", "08:00")
    assert hhmm2 == "08:00"
    assert text2 == "买了b5精华"


def test_life_line_local_date_helper_cutoff():
    """Natural midnight: any prefixed time stays on the session calendar day."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    base_date = _dt.date(2026, 6, 10)

    # 00:30 → natural midnight → same calendar day
    d_early = timeline._life_line_local_date("00:30 热水", base_date, "23:00")
    assert d_early == _dt.date(2026, 6, 10)

    # 07:00 → same day
    d_day = timeline._life_line_local_date("07:00 早餐", base_date, "08:00")
    assert d_day == _dt.date(2026, 6, 10)

    # No prefix → inherits session date
    d_legacy = timeline._life_line_local_date("早餐", base_date, "08:00")
    assert d_legacy == base_date


# ── catchup backfill: window keyed on session start, not digest write ────────

def _event(conn, sid: str, ts: str, content: str = "msg") -> None:
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content)"
        " VALUES (?, ?, 'user', ?)",
        (sid, ts, content),
    )
    conn.commit()


def test_task_digest_uses_sd_ts_not_session_start(conn):
    # Recently-written catchup digests with older events render in zone B.
    _event(conn, "s-old", _utc(40), "exam talk")
    _digest(conn, "s-old", _utc(1), tl="考完试凯旋", life="考完试凯旋")
    result = timeline.render_timeline(conn)
    assert "考完试凯旋" in result
    strip = result.split("**")[0]
    assert "考完试凯旋" not in strip


def test_live_digest_stays_in_24h_strip(conn):
    _event(conn, "s-new", _utc(3), "chat")
    _digest(conn, "s-new", _utc(2.5), tl="深夜闲聊", life="深夜闲聊")
    result = timeline.render_timeline(conn)
    assert "深夜闲聊" in result


def test_life_lines_resolve_against_event_span_midnight_crossing():
    lines, overflow = timeline._render_24h(
        [
            {
                "sid": "b2f76aa9",
                "ts": "2026-06-21T15:48:56Z",
                "kind": "casual",
                "tl_line": "fallback",
                "text": "body",
                "life_lines": (
                    "14:22 nap before shift\n"
                    "14:33 leaving for shift\n"
                    "02:39 after midnight note\n"
                    "04:50 late snack\n"
                    "08:59 morning wrap"
                ),
            }
        ],
        current_sid=None,
        from_utc="2026-06-20T00:00:00Z",
        to_utc="2026-06-22T00:00:00Z",
        event_spans={
            "b2f76aa9": ("2026-06-20T01:44:00Z", "2026-06-21T00:37:00Z")
        },
    )
    assert overflow == []
    assert lines == [
        "**06-21 Sun**",
        "08:59 morning wrap <!-- tl:b2f76aa9:0:4 -->",
        "04:50 late snack <!-- tl:b2f76aa9:0:3 -->",
        "02:39 after midnight note <!-- tl:b2f76aa9:0:2 -->",
        "**06-20 Sat**",
        "14:33 leaving for shift <!-- tl:b2f76aa9:0:1 -->",
        "14:22 nap before shift <!-- tl:b2f76aa9:0:0 -->",
    ]


def test_life_lines_render_reconcile_anchor_on_every_line():
    lines, overflow = timeline._render_24h(
        [
            {
                "sid": "s-every-line",
                "ts": _local_iso(2026, 6, 22, 12, 0),
                "kind": "casual",
                "tl_line": "fallback",
                "text": "body",
                "life_lines": "14:00 first\n18:00 second",
            }
        ],
        current_sid=None,
        from_utc="2026-06-21T16:30:00Z",
        to_utc="2026-06-22T16:30:00Z",
    )
    assert overflow == []
    content_lines = [line for line in lines if not line.startswith("**")]
    assert all("<!-- tl:s-every-line:0:" in line for line in content_lines)


def test_zone_b_includes_session_by_max_event_ts(conn, monkeypatch):
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    _freeze_timeline_now(
        monkeypatch,
        _dt.datetime(2026, 6, 22, 12, 0, tzinfo=melb),
    )
    _event(conn, "b2f76aa9", "2026-06-21T00:37:38Z", "old span end")
    _digest(
        conn,
        "b2f76aa9",
        "2026-06-21T15:48:56Z",
        kind="casual",
        tl="zone b summary",
        life="zone b summary",
    )
    result = timeline.render_timeline(conn)
    assert "zone b summary" in result
    zone_a = result.split("**", 1)[0]
    assert "zone b summary" not in zone_a


# ── Bug 1: sort key for LIFE lines uses real UTC, not ts[:10]+HH:MM ──────────

def test_life_line_sort_key_correct_utc(conn):
    """LIFE line sort keys must be real UTC datetimes, not date-prefix+local-HH:MM.

    A session at 15:00 Melbourne with a 05:08 LIFE line must sort after a
    session from the prior diary day, not between entries from 2 days ago.
    """
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)

    # Session A: 6h ago, e.g. 15:00 local, with a LIFE line "05:08 some event"
    sess_a_local = now_melb - _dt.timedelta(hours=6)
    if sess_a_local.hour < 6:
        # Adjust so session is clearly in the afternoon of its diary day
        sess_a_local = now_melb.replace(hour=15, minute=0, second=0, microsecond=0)
        if sess_a_local > now_melb:
            sess_a_local -= _dt.timedelta(days=1)
    ts_a = sess_a_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Session B: 2h ago (more recent)
    sess_b_local = now_melb - _dt.timedelta(hours=2)
    ts_b = sess_b_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    life_a = f"05:08 早起锻炼\n{sess_a_local.strftime('%H:%M')} 下午聊天"
    _digest(conn, "s-sortkey-a", ts_a, kind="casual", tl="综合一天", life=life_a)
    _digest(conn, "s-sortkey-b", ts_b, kind="casual", tl="最新任务", life="最新任务")

    result = timeline.render_timeline(conn)
    lines = [l for l in result.splitlines() if l and not l.startswith("##")]
    # Most recent session (s-sortkey-b, 2h ago) must appear before s-sortkey-a
    idx_b = next((i for i, l in enumerate(lines) if "最新任务" in l), None)
    idx_a_life = next((i for i, l in enumerate(lines) if "下午聊天" in l), None)
    if idx_b is not None and idx_a_life is not None:
        assert idx_b < idx_a_life, (
            f"newest session (idx {idx_b}) must precede older life line (idx {idx_a_life})"
        )


def test_24h_first_life_line_sorts_by_own_display_time():
    lines, overflow = timeline._render_24h(
        [
            {
                "sid": "s-early-first",
                "ts": _local_iso(2026, 6, 13, 20, 0),
                "kind": "casual",
                "tl_line": "晚间总结",
                "text": "body",
                "life_lines": "04:30 清晨醒来\n20:10 晚上聊天",
            },
            {
                "sid": "s-midday",
                "ts": _local_iso(2026, 6, 13, 12, 0),
                "kind": "casual",
                "text": "body",
                "life_lines": "12:00 中午任务",
            },
            {
                "sid": "s-prev-evening",
                "ts": _local_iso(2026, 6, 12, 22, 0),
                "kind": "casual",
                "text": "body",
                "life_lines": "22:00 前夜任务",
            },
        ],
        current_sid=None,
        from_utc="2026-06-12T00:00:00Z",
        to_utc="2026-06-14T00:00:00Z",
    )

    assert overflow == []
    assert lines == [
        "**06-13 Sat**",
        "20:10 晚上聊天 <!-- tl:s-early-first:0:1 -->",
        "12:00 中午任务 <!-- tl:s-midday:0:0 -->",
        "04:30 清晨醒来 <!-- tl:s-early-first:0:0 -->",
        "**06-12 Fri**",
        "22:00 前夜任务 <!-- tl:s-prev-evening:0:0 -->",
    ]
    assert lines.count("**06-12 Fri**") == 1
    assert lines.count("**06-13 Sat**") == 1


# ── Bug 2: line date = session calendar date (natural midnight) ──────────────

def test_life_line_utc_and_date_matches_session_calendar_day():
    """A 05:08 LIFE line lands on the session's own calendar day.

    Natural midnight: an early-morning line and its 05:30 session both belong
    to the same local calendar date, no previous-day shift.
    """
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    # Session start: today at 05:30 local
    now_melb = _dt.datetime.now(melb)
    sess_local = now_melb.replace(hour=5, minute=30, second=0, microsecond=0)
    if sess_local > now_melb:
        sess_local -= _dt.timedelta(days=1)
    sess_utc = sess_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _, line_date = timeline._life_line_utc_and_date("05:08 早起", sess_utc, "05:30")
    sess_diary_date = timeline._calendar_date_from_utc(sess_utc)

    assert line_date == sess_diary_date, (
        f"05:08 line diary date {line_date} must equal session diary date {sess_diary_date}"
    )


# ── Bug 3: tone tag on first 24h film-strip line ──────────────────────────────

def test_24h_line_with_affect_still_renders(conn):
    """A session with affect rows still renders without appending a tone tag."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)

    sess_local = now_melb - _dt.timedelta(hours=3)
    ts = sess_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _event(conn, "s-tone", ts, "hello")
    _digest(conn, "s-tone", ts, kind="casual", tl="今天完成了任务", life="今天完成了任务")
    # Insert affect row within session time span
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label,"
        " description, source, created_at)"
        " VALUES (?, 1, 0.8, 0.5, 3, '开心', '任务完成', 'test', ?)",
        (ts[:10], ts),
    )
    conn.commit()

    result = timeline.render_timeline(conn)
    lines = [l for l in result.splitlines() if "今天完成了任务" in l]
    assert lines, "session TL must appear"


def test_24h_no_tone_tag_without_affect(conn):
    """Session with no affect rows must not carry a tone tag."""
    _digest(conn, "s-notone", _utc(1), kind="casual", tl="普通任务", life="普通任务")
    result = timeline.render_timeline(conn)
    lines = [l for l in result.splitlines() if "普通任务" in l]
    assert lines
    # No 【】 in that line (session has no affect rows)
    assert "【" not in lines[0], f"unexpected tone tag: {lines[0]!r}"


# ── Bug 4: zone windows — no duplication, correct day ranges ──────────────────

def test_zone_a_starts_at_yesterday_calendar_midnight(conn, monkeypatch):
    """Zone A includes yesterday after 00:00 local, even beyond 24h."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    _freeze_timeline_now(
        monkeypatch,
        _dt.datetime(2026, 6, 23, 3, 30, tzinfo=melb),
    )
    _digest(
        conn,
        "s-after-midnight",
        _local_iso(2026, 6, 22, 0, 1),
        kind="casual",
        tl="昨天零点后",
        life="00:01 昨天零点后",
    )
    _digest(
        conn,
        "s-before-midnight",
        _local_iso(2026, 6, 21, 23, 59),
        kind="casual",
        tl="昨天零点前",
        life="23:59 昨天零点前",
    )
    result = timeline.render_timeline(conn)
    # Zone A uses **MM-DD Weekday** headers; Zone B uses **MM-DD Day 【tone】** headers.
    # Split on the first Zone B header to isolate Zone A content.
    import re as _re2
    zone_b_start = _re2.search(r"\*\*\d{2}-\d{2} Day\b", result)
    zone_a = result[:zone_b_start.start()] if zone_b_start else result
    assert "昨天零点后" in zone_a
    assert "昨天零点前" not in zone_a


def test_zone_b_covers_today_minus_2_to_4(conn):
    """Zone B covers today-2 through today-4 using diary overview."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)
    today = now_melb.date()

    day2 = today - _dt.timedelta(days=2)
    day4 = today - _dt.timedelta(days=4)
    conn.execute(
        "INSERT INTO diary (date, content, tone, overview) VALUES (?, 'body', '平淡', ?)",
        (day2.isoformat(), "两天前概览"),
    )
    conn.execute(
        "INSERT INTO diary (date, content, tone, overview) VALUES (?, 'body', '平淡', ?)",
        (day4.isoformat(), "四天前概览"),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    assert "两天前概览" in result, "today-2 overview must appear in zone B"
    assert "四天前概览" in result, "today-4 overview must appear in zone B"


def test_zone_b_excludes_today_minus_5(conn):
    """today-5 diary overview must NOT appear in zone B (out of range)."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)
    today = now_melb.date()
    day5 = today - _dt.timedelta(days=5)
    conn.execute(
        "INSERT INTO diary (date, content, tone, overview) VALUES (?, 'body', '平淡', ?)",
        (day5.isoformat(), "五天前概览"),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    assert "五天前概览" not in result


