"""Tests for marrow/daybrief.py — the day_log successor.

daybrief is pure glue: it must compose the SAME render functions the
SessionStart hook injects. Post-P2 the Timeline zone keeps render_timeline
output verbatim (H2 header + tl anchors + trail all retained) so
reconcile_timeline can absorb hand-edits back into the DB.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from marrow import config, daybrief, storage, timeline


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
    assert "## Timeline" in out          # verbatim render_timeline H2 (post-P2)
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


def test_timeline_zone_keeps_render_verbatim(conn):
    """Post-P2: zone body = render_timeline verbatim (H2 header + tl anchors +
    trail retained), plus the daybrief.timeline id marker for md_index."""
    _digest(conn, "sid-a", 2.0, "10:00 早上写代码")
    _digest(conn, "sid-b", 20.0, "12:00 昨天复习")
    expected = timeline.render_timeline(conn)
    assert expected and expected.splitlines()[0].startswith("## Timeline")
    assert "<!-- tl:" in expected           # sanity: anchors present
    assert "tl-rendered:" in expected       # sanity: trail present

    out = daybrief.render(conn)
    start = out.index(daybrief.TIMELINE_START) + len(daybrief.TIMELINE_START)
    end = out.index(daybrief.TIMELINE_END)
    zone = out[start:end].strip("\n")

    assert "## Timeline" in zone            # H2 header kept
    assert "<!-- tl:" in zone               # line anchors kept
    assert "tl-rendered:" in zone           # trail kept
    assert "<!-- id:daybrief.timeline -->" in zone  # md_index marker stamped
    # Every original timeline line survives verbatim.
    for line in expected.splitlines():
        assert line in zone


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


def test_first_timetrack_survive_timeline_change(conn):
    """Hand-written First/Timetrack are byte-preserved across a render where the
    timeline changed — exercises the P0 <!-- marrow:timeline:end --> terminator
    so the trailing zones are never absorbed into the timeline block nor lost."""
    _digest(conn, "sid-a", 2.0, "10:00 first line")
    existing = daybrief.render(conn)
    existing = existing.replace(
        daybrief._FIRST_PLACEHOLDER, "### First\nhand kept A\nhand kept B")
    existing = existing.replace(
        daybrief._TIMETRACK_PLACEHOLDER, "#### Timetrack\n- Deep work: 4h")

    _digest(conn, "sid-b", 1.0, "11:00 NEW timeline line")  # timeline now differs
    out = daybrief.render(conn, existing)

    assert "hand kept A\nhand kept B" in out
    assert "- Deep work: 4h" in out
    assert "11:00 NEW timeline line" in out
    for m in (daybrief.TIMELINE_START, daybrief.TIMELINE_END,
              daybrief.FIRST_START, daybrief.FIRST_END,
              daybrief.TIMETRACK_START, daybrief.TIMETRACK_END):
        assert m in out
    # First/Timetrack content stays OUT of the timeline zone.
    zstart = out.index(daybrief.TIMELINE_START)
    zend = out.index(daybrief.TIMELINE_END)
    assert "hand kept A" not in out[zstart:zend]
    assert "Deep work" not in out[zstart:zend]


# ── bidirectional: hand-edit absorbs into DB via update() ────────────────────

@pytest.fixture()
def daybrief_file(conn, tmp_path, monkeypatch):
    """Point update()'s out path + alert db at the tmp fixtures."""
    path = str(tmp_path / "daybrief.md")
    monkeypatch.setattr(daybrief, "_out_path", lambda: path)
    monkeypatch.setattr(config, "db_path", lambda: str(tmp_path / "db.db"))
    return path


def test_update_absorbs_handedit_into_db(conn, daybrief_file):
    _digest(conn, "sid-h", 2.0, "10:00 原始行")
    daybrief.update(conn)  # first build
    text = open(daybrief_file, encoding="utf-8").read()
    assert "10:00 原始行" in text

    edited = text.replace("10:00 原始行", "10:00 手改后的行")
    assert edited != text
    open(daybrief_file, "w", encoding="utf-8").write(edited)

    daybrief.update(conn)  # second call reconciles then re-renders

    life = conn.execute(
        "SELECT life_lines FROM session_digests WHERE sid='sid-h' AND segment_seq=0"
    ).fetchone()["life_lines"]
    assert "手改后的行" in life                       # DB absorbed the edit
    assert "手改后的行" in open(daybrief_file, encoding="utf-8").read()  # re-rendered


def test_update_delete_line_sets_tl_hidden(conn, daybrief_file):
    _digest(conn, "sid-d", 2.0, "10:00 to be deleted")
    daybrief.update(conn)
    text = open(daybrief_file, encoding="utf-8").read()
    # Drop the anchored timeline line, keep everything else.
    kept = "\n".join(
        ln for ln in text.splitlines() if "to be deleted" not in ln)
    open(daybrief_file, "w", encoding="utf-8").write(kept)

    daybrief.update(conn)

    hidden = conn.execute(
        "SELECT tl_hidden FROM session_digests WHERE sid='sid-d' AND segment_seq=0"
    ).fetchone()["tl_hidden"]
    assert hidden == 1


def test_update_gate_keeps_newer_db_row(conn, daybrief_file):
    """A DB row updated AFTER the render trail t= is NOT overwritten by a stale
    md line — the P0 t= gate works in the daybrief path."""
    _digest(conn, "sid-g", 2.0, "10:00 gate原始")
    daybrief.update(conn)
    text = open(daybrief_file, encoding="utf-8").read()

    # DB row moves forward in time (simulating an MCP write after render).
    future = (_dt.datetime.now(_dt.timezone.utc)
              + _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE session_digests SET life_lines='10:00 DB赢了', updated_at=?"
        " WHERE sid='sid-g' AND segment_seq=0", (future,))
    conn.commit()

    # Stale md still carries the old edit.
    stale = text.replace("10:00 gate原始", "10:00 md旧改")
    open(daybrief_file, "w", encoding="utf-8").write(stale)

    daybrief.update(conn)

    life = conn.execute(
        "SELECT life_lines FROM session_digests WHERE sid='sid-g' AND segment_seq=0"
    ).fetchone()["life_lines"]
    assert life == "10:00 DB赢了"           # DB row kept, stale md ignored
