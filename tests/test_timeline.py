"""Tests for marrow/timeline.py — render_timeline.

Covers:
- ND attribution (00-05 belongs to previous diary day)
- Day dividers in 24h film-strip
- 24h cap (15 lines)
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
            body: str = "body text") -> None:
    conn.execute(
        "INSERT INTO session_digests (sid, date, ts, text, kind, tl_line, life_lines)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, ts[:10], ts, body, kind, tl, life),
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


# ── open episodes ─────────────────────────────────────────────────────────────

def test_open_episode_renders_at_top(conn):
    _affect(conn, 0.2, 0.7, 3, "委屈", "吵架了", unresolved=1, hours_ago=5)
    result = timeline.render_timeline(conn)
    assert "> 未解: 吵架了" in result
    lines = result.splitlines()
    top = [l for l in lines if l.startswith("> 未解:")]
    assert top, "open episode must appear"
    # Must be before any HH:MM content
    content_idx = next((i for i, l in enumerate(lines)
                        if len(l) >= 5 and l[2] == ":"), None)
    open_idx = next((i for i, l in enumerate(lines)
                     if l.startswith("> 未解:")), None)
    if content_idx is not None and open_idx is not None:
        assert open_idx < content_idx


def test_open_episode_expired_hidden(conn):
    """Episode older than 7 days must not appear in open line."""
    ts_old = _utc(8 * 24)  # 8 days ago — outside _OPEN_EXPIRY_DAYS window
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label,"
        " description, source, unresolved, created_at)"
        " VALUES (?, 1, 0.2, 0.7, 3, '委屈', '旧事', 'test', 1, ?)",
        (ts_old[:10], ts_old),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    assert "旧事" not in result


def test_resolved_episode_not_in_open(conn):
    row_id = _affect(conn, 0.2, 0.7, 3, "委屈", "已解决了",
                     unresolved=1, hours_ago=5)
    ts_now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE affect SET resolved_at=? WHERE id=?",
                 (ts_now, row_id))
    conn.commit()
    result = timeline.render_timeline(conn)
    assert "已解决了" not in result


# ── 24h film-strip ────────────────────────────────────────────────────────────

def test_24h_shows_tl_for_task(conn):
    _digest(conn, "s1", _utc(1), kind="task", tl="修了recall的bug", life=None)
    result = timeline.render_timeline(conn)
    assert "修了recall的bug" in result


def test_24h_shows_life_lines_for_casual(conn):
    _digest(conn, "s2", _utc(2), kind="casual", tl="聊天了",
            life="喝了拿铁\n看到小雏菊")
    result = timeline.render_timeline(conn)
    assert "喝了拿铁" in result


def test_24h_cap_15_lines(conn):
    for i in range(20):
        _digest(conn, f"s-cap-{i}", _utc(0.5 + i * 0.05),
                kind="task", tl=f"任务{i}", life=None)
    result = timeline.render_timeline(conn)
    # Count non-header content lines in 24h zone (lines with HH:MM or life items)
    lines = result.splitlines()
    # Only count up to the first blank line or Zone 2 header (**MM-DD...)
    content_lines: list[str] = []
    for ln in lines[1:]:  # skip ## Timeline
        if not ln or ln.startswith("**") or ln.startswith("Week"):
            break
        if ln.startswith("---") or ln.startswith("> 未解:") or ln.startswith("<!--"):
            continue
        content_lines.append(ln)
    assert len(content_lines) <= timeline._24H_CAP


def test_day_divider_on_date_crossing(conn):
    """--- MM-DD --- divider appears in 24h zone when sessions cross a diary day.

    Build two sessions exactly straddling the 6AM day boundary within 24h.
    """
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)

    # Session A: today after 6AM (e.g. 10:00 local)
    session_a_local = now_melb.replace(hour=10, minute=0, second=0, microsecond=0)
    if session_a_local > now_melb:
        session_a_local -= _dt.timedelta(days=1)
    ts_a = session_a_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Session B: same calendar day but before 6AM (e.g. 03:00 local)
    # → belongs to PREVIOUS diary day
    session_b_local = session_a_local.replace(hour=3, minute=0)
    ts_b = session_b_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Both must be within 24h of now
    cutoff = (now_melb - _dt.timedelta(hours=24)).astimezone(_dt.timezone.utc)
    if session_b_local.astimezone(_dt.timezone.utc) < cutoff:
        # Too old — skip; can't reliably test midnight crossing at all times of day
        pytest.skip("cannot construct cross-boundary pair within 24h window at this time of day")

    _digest(conn, "s-after6", ts_a, kind="task", tl="上午任务")
    _digest(conn, "s-before6", ts_b, kind="task", tl="深夜任务")
    result = timeline.render_timeline(conn)
    assert "---" in result


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

def test_nd_00_to_06_belongs_to_previous_day(conn):
    """A session at 02:00 local time belongs to the PREVIOUS diary day's ND."""
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
    # 02:00 local is ND and belongs to PREVIOUS diary day
    assert period == "ND"
    expected_date = (today_melb - _dt.timedelta(days=1)).date()
    assert diary_date == expected_date


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


# ── 24-72h zone ──────────────────────────────────────────────────────────────

def test_2472h_empty_period_hidden(conn):
    """If no sessions in AM, AM line must not appear."""
    # Session 30h ago at a time that renders as PM
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)
    # Use 14:00 local yesterday-ish
    target = now_melb - _dt.timedelta(hours=30)
    target_pm = target.replace(hour=14, minute=0, second=0, microsecond=0)
    ts_utc = target_pm.astimezone(_dt.timezone.utc)
    ts_iso = ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    _digest(conn, "s-pm", ts_iso, kind="task", tl="下午任务")
    result = timeline.render_timeline(conn)
    # Should have PM but not AM for that day (assuming no AM session seeded)
    if "**" in result and "AM" in result:
        # Only assert AM is not present when there's no AM session
        lines = result.splitlines()
        for ln in lines:
            if ln.strip() == "AM":
                pytest.fail("AM line present without AM session data")


def test_2472h_day_header_present(conn):
    """Day header **MM-DD Day 【tone】** appears for sessions 24-72h ago."""
    _digest(conn, "s-2472", _utc(36), kind="task", tl="前天任务")
    result = timeline.render_timeline(conn)
    assert "**" in result and "Day" in result


# ── day 4-7 zone ─────────────────────────────────────────────────────────────

def test_day47_week_header_present(conn):
    """Week 【tone】 header appears if there's affect data for day 4-7."""
    _affect(conn, 0.7, 0.4, 2, "温暖", "散步了", hours_ago=5 * 24)
    result = timeline.render_timeline(conn)
    assert "Week" in result


def test_day47_diary_tl_included(conn):
    """diary.tl_line for a day-4+ date appears in day 4-7 zone."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    today = (_dt.datetime.now(melb).date()
             if _dt.datetime.now(melb).hour >= 6
             else (_dt.datetime.now(melb) - _dt.timedelta(days=1)).date())
    day4 = today - _dt.timedelta(days=4)
    conn.execute(
        "INSERT INTO diary (date, content, tl_line) VALUES (?, 'diary body', ?)",
        (day4.isoformat(), "四天前的一天"),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    assert "四天前的一天" in result


# ── NULL tl_line fallback ─────────────────────────────────────────────────────

def test_null_tl_line_falls_back_to_body(conn):
    """When tl_line is NULL, render truncated body text."""
    _digest(conn, "s-null-tl", _utc(1), kind="task", tl=None,
            body="body text that stands in for tl line detail")
    result = timeline.render_timeline(conn)
    assert "body text" in result


def test_null_tl_line_truncated_at_60(conn):
    long_body = "x" * 100
    _digest(conn, "s-long-body", _utc(2), kind="task", tl=None, body=long_body)
    result = timeline.render_timeline(conn)
    assert "x" * 60 in result
    assert "x" * 61 not in result


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


# ── trim order ────────────────────────────────────────────────────────────────

def test_trim_drops_day47_before_24h(conn):
    """When over budget, day lines trimmed before 24h lines."""
    # Seed many sessions across all zones
    for i in range(10):
        _digest(conn, f"s-24h-{i}", _utc(i * 2),
                kind="task", tl=f"最近任务{i}")
    # Day 4-7 diary entries
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    today = (_dt.datetime.now(melb).date()
             if _dt.datetime.now(melb).hour >= 6
             else (_dt.datetime.now(melb) - _dt.timedelta(days=1)).date())
    for d in range(3, 7):
        conn.execute(
            "INSERT INTO diary (date, content, tl_line) VALUES (?, 'body', ?)",
            ((today - _dt.timedelta(days=d)).isoformat(), f"日记第{d}天"),
        )
    conn.commit()
    result = timeline.render_timeline(conn)
    # Should still have some recent 24h content even if day47 was trimmed
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


def test_life_lines_midnight_crossing_divider(conn):
    """LIFE lines at 00:30 (before 6AM) cross into previous diary day — divider expected."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)

    # Session started at 23:00 yesterday local
    sess_local = now_melb.replace(hour=23, minute=0, second=0, microsecond=0)
    if sess_local >= now_melb:
        sess_local -= _dt.timedelta(days=1)
    ts = sess_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Check both timestamps are within 24h
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    ts_dt = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if (now_utc - ts_dt).total_seconds() > 24 * 3600:
        import pytest as _pt
        _pt.skip("session outside 24h window at this time of day")

    # One LIFE line at 23:30 (same diary day) and one at 00:30 (prev diary day)
    life = "23:30 看了个电影\n00:30 睡前喝了热水"
    _digest(conn, "s-midnight", ts, kind="casual", tl="夜聊", life=life)
    result = timeline.render_timeline(conn)

    # At least one date divider must appear due to the 00:30 crossing
    assert "---" in result


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
    """00:30 local time → previous diary day; 07:00 → same day."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    base_date = _dt.date(2026, 6, 10)

    # 00:30 → before 6AM cutoff → previous day
    d_early = timeline._life_line_local_date("00:30 热水", base_date, "23:00")
    assert d_early == _dt.date(2026, 6, 9)

    # 07:00 → after cutoff → same day
    d_day = timeline._life_line_local_date("07:00 早餐", base_date, "08:00")
    assert d_day == _dt.date(2026, 6, 10)

    # No prefix → inherits session date
    d_legacy = timeline._life_line_local_date("早餐", base_date, "08:00")
    assert d_legacy == base_date


# ── prompt content checks ─────────────────────────────────────────────────────

def test_prompt_facts_60w_cap():
    """TASK_AFFECT_DIGEST_PROMPT must reference 60-word cap for TL+FACTS."""
    from marrow.sessionend_prompts import TASK_AFFECT_DIGEST_PROMPT
    assert "60 words" in TASK_AFFECT_DIGEST_PROMPT


def test_prompt_life_hhmm_rule():
    """Prompt must instruct model to prefix LIFE lines with HH:MM timestamp."""
    from marrow.sessionend_prompts import TASK_AFFECT_DIGEST_PROMPT
    assert "HH:MM" in TASK_AFFECT_DIGEST_PROMPT
    # Example in ===DIGEST=== block should show a timestamped LIFE line
    assert "21:40 买了b5精华" in TASK_AFFECT_DIGEST_PROMPT


# ── catchup backfill: window keyed on session start, not digest write ────────

def _event(conn, sid: str, ts: str, content: str = "msg") -> None:
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content)"
        " VALUES (?, ?, 'user', ?)",
        (sid, ts, content),
    )
    conn.commit()


def test_backfilled_digest_uses_session_start_time(conn):
    # Session really happened 40h ago; catchup wrote its digest 1h ago.
    _event(conn, "s-old", _utc(40), "exam talk")
    _digest(conn, "s-old", _utc(1), tl="考完试凯旋")
    result = timeline.render_timeline(conn)
    # Must land in the 24-72h zone (day header present), NOT the 24h strip.
    day_headers = [l for l in result.splitlines() if l.startswith("**")]
    assert day_headers, "backfilled session must render under a day header"
    assert "考完试凯旋" in result
    strip = result.split("**")[0]  # text before first day header
    assert "考完试凯旋" not in strip


def test_live_digest_stays_in_24h_strip(conn):
    _event(conn, "s-new", _utc(3), "chat")
    _digest(conn, "s-new", _utc(2.5), tl="深夜闲聊")
    result = timeline.render_timeline(conn)
    assert "深夜闲聊" in result
    assert not [l for l in result.splitlines() if l.startswith("**")]


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
    _digest(conn, "s-sortkey-b", ts_b, kind="task", tl="最新任务")

    result = timeline.render_timeline(conn)
    lines = [l for l in result.splitlines() if l and not l.startswith("##")]
    # Most recent session (s-sortkey-b, 2h ago) must appear before s-sortkey-a
    idx_b = next((i for i, l in enumerate(lines) if "最新任务" in l), None)
    idx_a_life = next((i for i, l in enumerate(lines) if "下午聊天" in l), None)
    if idx_b is not None and idx_a_life is not None:
        assert idx_b < idx_a_life, (
            f"newest session (idx {idx_b}) must precede older life line (idx {idx_a_life})"
        )


# ── Bug 2: no double 6AM cutoff ───────────────────────────────────────────────

def test_life_line_utc_and_date_no_double_cutoff():
    """_life_line_utc_and_date must not shift 6AM twice.

    A 05:08 LIFE line from a session that starts at 05:30 (both before 6AM)
    must land on the SAME diary date as the session's own 6AM-shifted date,
    not one day earlier.
    """
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    # Session start: today at 05:30 local
    now_melb = _dt.datetime.now(melb)
    sess_local = now_melb.replace(hour=5, minute=30, second=0, microsecond=0)
    if sess_local > now_melb:
        sess_local -= _dt.timedelta(days=1)
    sess_utc = sess_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 05:08 LIFE line — same diary day as session (both before 6AM → both on
    # previous calendar day's diary date)
    _, line_date = timeline._life_line_utc_and_date("05:08 早起", sess_utc, "05:30")
    sess_diary_date = timeline._local_date_from_utc(sess_utc)

    assert line_date == sess_diary_date, (
        f"05:08 line diary date {line_date} must equal session diary date {sess_diary_date}"
    )


# ── Bug 3: tone tag on first 24h film-strip line ──────────────────────────────

def test_24h_tone_tag_on_first_line_with_affect(conn):
    """First rendered line of a session with affect rows carries 【tone】 tag."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)

    sess_local = now_melb - _dt.timedelta(hours=3)
    ts = sess_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _event(conn, "s-tone", ts, "hello")
    _digest(conn, "s-tone", ts, kind="task", tl="今天完成了任务")
    # Insert affect row within session time span
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label,"
        " description, source, created_at)"
        " VALUES (?, 1, 0.8, 0.5, 3, '开心', '任务完成', 'test', ?)",
        (ts[:10], ts),
    )
    conn.commit()

    result = timeline.render_timeline(conn)
    # The session's line must contain 【...】 tone tag
    lines = [l for l in result.splitlines() if "今天完成了任务" in l]
    assert lines, "session TL must appear"
    assert "【" in lines[0], f"tone tag missing from first line: {lines[0]!r}"


def test_24h_no_tone_tag_without_affect(conn):
    """Session with no affect rows must not carry a tone tag."""
    _digest(conn, "s-notone", _utc(1), kind="task", tl="普通任务")
    result = timeline.render_timeline(conn)
    lines = [l for l in result.splitlines() if "普通任务" in l]
    assert lines
    # No 【】 in that line (session has no affect rows)
    assert "【" not in lines[0], f"unexpected tone tag: {lines[0]!r}"


# ── Bug 4: zone windows — no duplication, correct day ranges ──────────────────

def test_zone_b_does_not_overlap_zone_a(conn):
    """A session 25h ago must appear in zone (b), not zone (a)."""
    _digest(conn, "s-25h", _utc(25), kind="task", tl="昨天的事")
    result = timeline.render_timeline(conn)
    # Zone (a) is the text before the first ** day header
    if "**" in result:
        zone_a = result.split("**")[0]
        assert "昨天的事" not in zone_a, "25h-old session must not appear in 24h strip"
    assert "昨天的事" in result


def test_zone_c_covers_five_days(conn):
    """Zone (c) covers today-4 through today-8 (five diary days)."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)
    today = (now_melb.date() if now_melb.hour >= 6
             else (now_melb - _dt.timedelta(days=1)).date())

    # Insert diary entries for today-4 and today-8
    day4 = today - _dt.timedelta(days=4)
    day8 = today - _dt.timedelta(days=8)
    conn.execute(
        "INSERT INTO diary (date, content, tl_line) VALUES (?, 'body', ?)",
        (day4.isoformat(), "四天前日记"),
    )
    conn.execute(
        "INSERT INTO diary (date, content, tl_line) VALUES (?, 'body', ?)",
        (day8.isoformat(), "八天前日记"),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    assert "四天前日记" in result, "today-4 diary must appear in zone (c)"
    assert "八天前日记" in result, "today-8 diary must appear in zone (c)"


def test_zone_b_and_c_no_day3_duplication(conn):
    """today-3 must appear only in zone (b), not in zone (c)."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)
    today = (now_melb.date() if now_melb.hour >= 6
             else (now_melb - _dt.timedelta(days=1)).date())
    day3 = today - _dt.timedelta(days=3)

    # Session 73h ago puts it in zone (b) for today-3
    _digest(conn, "s-day3", _utc(73), kind="task", tl="三天前任务")
    # Also add diary entry for today-3
    conn.execute(
        "INSERT INTO diary (date, content, tl_line) VALUES (?, 'body', ?)",
        (day3.isoformat(), "三天前日记"),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    # today-3 diary must NOT appear (it's in zone b, not c)
    assert "三天前日记" not in result, "today-3 diary must not appear in zone (c)"


# ── Bug 5: tl_line pollution guard ───────────────────────────────────────────

def test_rendered_day_line_in_diary_treated_as_null(conn):
    """diary.tl_line that looks like a rendered day line is excluded."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)
    today = (now_melb.date() if now_melb.hour >= 6
             else (now_melb - _dt.timedelta(days=1)).date())
    day5 = today - _dt.timedelta(days=5)
    # Polluted tl_line (looks like rendered output)
    conn.execute(
        "INSERT INTO diary (date, content, tl_line) VALUES (?, 'body', ?)",
        (day5.isoformat(), "06-09 Day 【平淡】"),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    # Must NOT produce double prefix like "06-09 Day 【专注】 06-09 Day 【平淡】"
    assert "06-09 Day 【平淡】 06-09 Day" not in result
    assert "06-09 Day 【平淡】" not in result


def test_empty_diary_tl_line_treated_as_null(conn):
    """diary.tl_line that is empty string is excluded (not rendered as blank)."""
    from zoneinfo import ZoneInfo
    melb = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(melb)
    today = (now_melb.date() if now_melb.hour >= 6
             else (now_melb - _dt.timedelta(days=1)).date())
    day6 = today - _dt.timedelta(days=6)
    conn.execute(
        "INSERT INTO diary (date, content, tl_line) VALUES (?, 'body', ?)",
        (day6.isoformat(), ""),
    )
    conn.commit()
    # Should not crash; empty tl_line renders without content (fallback path)
    result = timeline.render_timeline(conn)
    assert "## Timeline" in result


def test_rendered_day_line_in_session_digest_treated_as_null(conn):
    """session_digests.tl_line that matches rendered day pattern falls back to body."""
    _digest(conn, "s-polluted", _utc(2), kind="task",
            tl="06-09 Day 【平淡】",
            body="real content from session body text here")
    result = timeline.render_timeline(conn)
    # Must not render the rendered-day-line pattern; body fallback shown instead
    assert "06-09 Day 【平淡】" not in result
    assert "real content from session body text" in result
