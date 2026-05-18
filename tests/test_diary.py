"""Tests for marrow/diary.py. LLM faked — prompt quality not under test;
day-boundary, per-session map-reduce, idempotency, dual triggers are.

Melbourne is UTC+10 (AEST) on these dates; diary_day(utc) = (utc+10h-4h)
= (utc+6h).date(). So UTC 18:00 rolls into the next diary day; a local
02:00 (UTC 16:00) still counts as the previous day.
"""
from __future__ import annotations

import datetime as dt

import pytest

from marrow import diary, storage


class FakeLLM:
    def __init__(self, lessons="coding\tnever guess a path"):
        self.lessons = lessons
        self.calls: list[str] = []

    def call(self, role, body, *, tier="cheap"):
        self.calls.append(role)
        if role == "day-digest":
            return "digest: did X"
        if role == "stitch":
            return "woven strand with X"
        if role == "diary":
            return "今天我们一起把 X 做完了。"
        if role == "lessons":
            return self.lessons
        return ""

    def n(self, role):
        return self.calls.count(role)


def _ev(conn, sid, ts, role, content):
    conn.execute("INSERT INTO events(session_id,timestamp,role,content) "
                 "VALUES(?,?,?,?)", (sid, ts, role, content))


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    # all map to diary day 2026-05-16 (utc+6h within that date)
    _ev(conn, "s1", "2026-05-16T02:00:00Z", "user", "build cli")
    _ev(conn, "s1", "2026-05-16T02:01:00Z", "assistant", "done")
    _ev(conn, "s2", "2026-05-16T09:00:00Z", "user", "fix bug")
    _ev(conn, "s2", "2026-05-16T09:01:00Z", "assistant", "fixed")
    conn.commit()
    return p, conn


# ── day boundary ──────────────────────────────────────────────────────────────

def test_diary_day_local_0400_cutoff():
    # UTC 18:00 -> local next-day 04:00 -> rolls to next diary day
    assert diary._diary_day("2026-05-16T17:00:00Z") == "2026-05-16"
    assert diary._diary_day("2026-05-16T18:00:00Z") == "2026-05-17"
    # local 02:00 (UTC 16:00) counts as previous day (late-night spillover)
    assert diary._diary_day("2026-05-16T16:00:00Z") == "2026-05-16"


def test_routine_target_is_just_closed_day(monkeypatch):
    fixed = dt.datetime(2026, 5, 18, 4, 30, tzinfo=diary._TZ)

    class _DT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(diary._dt, "datetime", _DT)
    assert diary._routine_target() == "2026-05-17"


# ── grouping / pending ────────────────────────────────────────────────────────

def test_day_events_only_that_diary_day(db):
    _, conn = db
    _ev(conn, "s9", "2026-05-16T18:00:00Z", "user", "next day")  # -> 05-17
    conn.commit()
    evs = diary.day_events(conn, "2026-05-16")
    assert {e["session_id"] for e in evs} == {"s1", "s2"}


def test_pending_days_excludes_written(db):
    p, conn = db
    assert diary.pending_days(conn) == ["2026-05-16"]
    conn.execute("INSERT INTO diary(date,content) VALUES('2026-05-16','x')")
    conn.commit()
    assert diary.pending_days(conn) == []


# ── per-session map-reduce ────────────────────────────────────────────────────

def test_run_day_one_digest_per_session(db):
    p, conn = db
    f = FakeLLM()
    assert diary.run_day(conn, "2026-05-16", f, db=p) is True
    assert f.n("day-digest") == 2          # s1 + s2, not one whole-day blob
    assert f.n("stitch") == 1              # 2 sessions woven once
    assert f.n("diary") == 1
    assert f.n("lessons") == 1
    row = conn.execute(
        "SELECT content,session_ids FROM diary WHERE date='2026-05-16'"
    ).fetchone()
    assert "X" in row["content"]
    assert row["session_ids"] == "s1,s2"
    assert conn.execute(
        "SELECT 1 FROM lessons").fetchone() is not None


def test_single_session_skips_stitch(tmp_path):
    p = str(tmp_path / "one.db")
    conn = storage.init_db(p)
    _ev(conn, "solo", "2026-05-16T02:00:00Z", "user", "just one session")
    _ev(conn, "solo", "2026-05-16T02:05:00Z", "assistant", "ok")
    conn.commit()
    f = FakeLLM()
    assert diary.run_day(conn, "2026-05-16", f, db=p) is True
    assert f.n("day-digest") == 1
    assert f.n("stitch") == 0              # nothing to weave, digest is strand
    assert f.n("diary") == 1


def test_oversized_session_is_chunked(db):
    p, conn = db
    big = "x" * (diary._SESSION_CHAR_CAP + diary._CHUNK_CHARS)
    _ev(conn, "s3", "2026-05-16T10:00:00Z", "user", big)
    conn.commit()
    f = FakeLLM()
    diary.run_day(conn, "2026-05-16", f, db=p)
    # s1 (1) + s2 (1) + s3 chunked (>=2) -> more than 3 digest calls
    assert f.n("day-digest") >= 4


def test_idempotent_skip(db):
    p, conn = db
    diary.run_day(conn, "2026-05-16", FakeLLM(), db=p)
    f2 = FakeLLM()
    assert diary.run_day(conn, "2026-05-16", f2, db=p) is False
    assert f2.calls == []


def test_lessons_none(db):
    p, conn = db
    diary.run_day(conn, "2026-05-16", FakeLLM(lessons="NONE"), db=p)
    assert conn.execute("SELECT COUNT(*) c FROM lessons").fetchone()["c"] == 0


# ── dual triggers ─────────────────────────────────────────────────────────────

def test_catchup_caps_and_alerts(db):
    p, conn = db
    base = dt.date(2026, 5, 16)
    for i in range(1, 6):  # 5 extra missing diary days
        d = base - dt.timedelta(days=i)
        _ev(conn, "s", f"{d.isoformat()}T08:00:00Z", "user", "x")
    conn.commit()
    written = diary.run(conn, FakeLLM(), db=p, catchup=True)
    assert len(written) == diary.CATCHUP_MAX
    al = conn.execute(
        "SELECT message FROM alerts WHERE type='routine'").fetchone()
    assert al and "still pending" in al["message"]


def test_run_explicit_day(db):
    p, conn = db
    assert diary.run(conn, FakeLLM(), db=p, day="2026-05-16") == ["2026-05-16"]
