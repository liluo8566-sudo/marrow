"""Tests for marrow/daybrief.py — the day_log successor.

daybrief is pure glue: it must compose the SAME render functions the
SessionStart hook injects. The load-bearing assertion is line-content equality
of the Timeline zone against timeline.render_timeline (identical to the
dashboard) modulo the stripped leading header and tl reconcile anchors, which
have no function in a human-read file.
"""
from __future__ import annotations

import datetime as _dt
import re

import pytest

from marrow import daybrief, storage, timeline

_TL_ANCHOR_RE = re.compile(r"[ \t]*<!--\s*tl:[^>]*?-->")
_TL_TRAIL_RE = re.compile(r"\n?<!--\s*tl-rendered:[^>]*?-->")


def _strip_anchors(content: str) -> str:
    content = _TL_TRAIL_RE.sub("", content)
    return "\n".join(_TL_ANCHOR_RE.sub("", ln).rstrip() for ln in content.splitlines())


@pytest.fixture()
def conn(tmp_path):
    c = storage.init_db(str(tmp_path / "db.db"))
    yield c
    c.close()


def _digest(conn, sid: str, hours_ago: float, life_line: str) -> None:
    """A 24h film-strip row: life_lines (not tl_line) is what render_timeline's
    _render_24h turns into anchored lines."""
    ts = (_dt.datetime.now(_dt.timezone.utc)
          - _dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO session_digests (sid, date, ts, text, kind, life_lines)"
        " VALUES (?, ?, ?, ?, 'casual', ?)",
        (sid, ts[:10], ts, "body", life_line),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _stub_external(monkeypatch):
    """schedule.render_daily needs the cadence binary; usage reads the live kv.
    Stub both to deterministic values so the test exercises daybrief's own
    composition, not those sources."""
    monkeypatch.setattr(
        daybrief.schedule, "render_daily",
        lambda: "## Daily Schedule  2026-07-10 Fri | now 09:00\n"
                "note line\n- [Appointment] 08:30 GP 🚩\n---\n"
                "- [Routine] 05:30-06:15 Wake up")
    monkeypatch.setattr(
        daybrief.usage, "sessionstart_lines",
        lambda: ["Plan Used: 5h 5% | 7d 50%", "Net Token Used today: 1.2M"])


def test_all_zones_present(conn):
    _digest(conn, "sid-a", 2.0, "20:00 聊天了")
    out = daybrief.render(conn)
    for marker in (
        daybrief.STATUS_START, daybrief.STATUS_END,
        daybrief.REMCAL_START, daybrief.REMCAL_END,
        daybrief.TIMELINE_START, daybrief.TIMELINE_END,
        daybrief.FIRST_START, daybrief.FIRST_END,
        daybrief.TIMETRACK_START, daybrief.TIMETRACK_END,
    ):
        assert marker in out
    assert "### Status" in out
    assert "### Rem & Cal" in out
    assert "### Timeline" in out
    assert "### First" in out
    assert "#### Timetrack" in out


def test_status_body_from_usage(conn):
    out = daybrief.render(conn)
    assert "Plan Used: 5h 5% | 7d 50%" in out
    assert "Net Token Used today: 1.2M" in out


def test_remcal_strips_daily_schedule_header(conn):
    out = daybrief.render(conn)
    assert "## Daily Schedule" not in out          # header stripped
    assert "- [Appointment] 08:30 GP 🚩" in out    # body kept verbatim
    assert "- [Routine] 05:30-06:15 Wake up" in out


def test_timeline_zone_matches_render_timeline_modulo_header_and_anchors(conn):
    _digest(conn, "sid-a", 2.0, "10:00 早上写代码")
    _digest(conn, "sid-b", 20.0, "12:00 昨天复习")
    expected = timeline.render_timeline(conn)
    assert expected  # sanity: fixture produced real timeline content
    assert expected.splitlines()[0].startswith("## Timeline")  # dashboard header present
    assert "<!-- tl:" in expected  # sanity: fixture produced anchors to strip

    out = daybrief.render(conn)
    start = out.index(daybrief.TIMELINE_START) + len(daybrief.TIMELINE_START)
    end = out.index(daybrief.TIMELINE_END)
    zone = out[start:end].strip("\n")

    assert "<!-- tl:" not in zone           # line anchors stripped
    assert "tl-rendered:" not in zone       # trailing render-state stripped
    assert not zone.startswith("### Timeline\n## Timeline")  # header not doubled

    expected_stripped_lines = expected.splitlines()[1:]  # drop '## Timeline' header
    expected_body = _strip_anchors("\n".join(expected_stripped_lines)).strip("\n")
    assert zone == "### Timeline\n" + expected_body


def test_first_and_timetrack_carry_over_byte_for_byte(conn):
    existing = (
        "2026-07-10\n\n"
        f"{daybrief.FIRST_START}\n### First\nhand-written line one\nline two\n"
        f"{daybrief.FIRST_END}\n\n"
        f"{daybrief.TIMETRACK_START}\n#### Timetrack\n- Today: 3h\n"
        f"{daybrief.TIMETRACK_END}\n"
    )
    out = daybrief.render(conn, existing)
    assert "hand-written line one\nline two" in out
    assert "- Today: 3h" in out


def test_missing_zones_fall_back_to_placeholder(conn):
    out = daybrief.render(conn, existing=None)
    assert "placeholder" in out  # both deferred zones carry a placeholder comment
