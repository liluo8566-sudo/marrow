"""Tests for marrow/daily_catchup.py: 6AM boundary, pending scan, fcntl lock."""
from __future__ import annotations

import os

import pytest

from marrow import daily_catchup, storage


def _ev(conn, sid, ts, role, content):
    conn.execute(
        "INSERT INTO events(session_id,timestamp,role,content)"
        " VALUES(?,?,?,?)", (sid, ts, role, content))


@pytest.fixture()
def conn(tmp_path):
    c = storage.init_db(str(tmp_path / "t.db"))
    yield c
    c.close()


def test_diary_day_6am_cutoff():
    assert daily_catchup.diary_day("2026-05-16T19:00:00Z") == "2026-05-16"
    assert daily_catchup.diary_day("2026-05-16T20:00:00Z") == "2026-05-17"
    assert daily_catchup.diary_day("2026-05-16T18:00:00Z") == "2026-05-16"


def test_pending_days_excludes_diary_done(conn, monkeypatch):
    import datetime as dt
    pinned = dt.date(2026, 5, 17)

    class _D(dt.date):
        @classmethod
        def today(cls):
            return pinned

    class _DT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 5, 17, 19, 0, tzinfo=daily_catchup._TZ)

    monkeypatch.setattr(daily_catchup._dt, "date", _D)
    monkeypatch.setattr(daily_catchup._dt, "datetime", _DT)
    _ev(conn, "s1", "2026-05-16T08:00:00Z", "user", "x")
    conn.commit()
    assert daily_catchup.pending_days(conn) == ["2026-05-16"]
    conn.execute("INSERT INTO diary(date, content) VALUES('2026-05-16','x')")
    conn.commit()
    assert daily_catchup.pending_days(conn) == []


def test_day_events_filters_by_6am(conn):
    _ev(conn, "s1", "2026-05-16T07:00:00Z", "user", "in")   # local 17:00, in
    _ev(conn, "s2", "2026-05-16T20:00:00Z", "user", "out")  # local 06:00 next, out
    conn.commit()
    evs = daily_catchup.day_events(conn, "2026-05-16")
    sids = {e["session_id"] for e in evs}
    assert sids == {"s1"}


def test_has_diary(conn):
    assert daily_catchup.has_diary(conn, "2026-05-16") is False
    conn.execute("INSERT INTO diary(date, content) VALUES('2026-05-16','x')")
    conn.commit()
    assert daily_catchup.has_diary(conn, "2026-05-16") is True


def test_app_lock_serializes_holders(tmp_path):
    lf = str(tmp_path / "daily.lock")
    with daily_catchup.app_lock(lf):
        with pytest.raises(BlockingIOError):
            with daily_catchup.app_lock(lf, blocking=False):
                pass
    with daily_catchup.app_lock(lf, blocking=False):
        pass
    assert os.path.exists(lf)


def test_app_lock_releases_on_exception(tmp_path):
    lf = str(tmp_path / "daily.lock")
    with pytest.raises(RuntimeError):
        with daily_catchup.app_lock(lf):
            raise RuntimeError("boom")
    with daily_catchup.app_lock(lf, blocking=False):
        pass
