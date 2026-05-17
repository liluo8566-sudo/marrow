"""Tests for marrow/diary.py deterministic pipeline. LLM is faked — prompt
quality is not under test here; idempotency, parsing, and DB writes are.
"""
from __future__ import annotations

import pytest

from marrow import diary, storage


class FakeLLM:
    def __init__(self, lessons="coding\tnever guess a path"):
        self.lessons = lessons
        self.calls = []

    def call(self, role, body, *, tier="cheap"):
        self.calls.append((role, tier))
        if role == "day-digest":
            return "digest: did X, fixed Y"
        if role == "diary":
            return "今天我们一起把 X 做完了。"
        if role == "lessons":
            return self.lessons
        return ""


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO events(session_id,timestamp,role,content) "
        "VALUES('s1','2026-05-16T09:00:00Z','user','build the cli')"
    )
    conn.execute(
        "INSERT INTO events(session_id,timestamp,role,content) "
        "VALUES('s1','2026-05-16T09:01:00Z','assistant','done, 68 tests green')"
    )
    conn.commit()
    return p, conn


def test_pending_days_lists_event_days_without_diary(db):
    _, conn = db
    assert diary.pending_days(conn) == ["2026-05-16"]


def test_run_day_writes_diary_lessons_audit_alert(db):
    p, conn = db
    assert diary.run_day(conn, "2026-05-16", FakeLLM(), db=p) is True
    d = conn.execute("SELECT content FROM diary WHERE date='2026-05-16'") \
        .fetchone()
    assert "X" in d["content"]
    le = conn.execute("SELECT scope,lesson_text FROM lessons").fetchone()
    assert le["scope"] == "coding"
    al = conn.execute(
        "SELECT 1 FROM alerts WHERE type='lesson'").fetchone()
    assert al is not None
    au = conn.execute(
        "SELECT 1 FROM audit_log WHERE target_table='diary'").fetchone()
    assert au is not None


def test_idempotent_skip_existing_diary(db):
    p, conn = db
    diary.run_day(conn, "2026-05-16", FakeLLM(), db=p)
    assert diary.run_day(conn, "2026-05-16", FakeLLM(), db=p) is False
    assert diary.pending_days(conn) == []


def test_lessons_none_writes_no_lesson(db):
    p, conn = db
    diary.run_day(conn, "2026-05-16", FakeLLM(lessons="NONE"), db=p)
    assert conn.execute("SELECT COUNT(*) c FROM lessons").fetchone()["c"] == 0


def test_empty_day_skipped(db):
    p, conn = db
    assert diary.run_day(conn, "2026-01-01", FakeLLM(), db=p) is False


def test_run_catchup_backfills_missing_days(db):
    p, conn = db
    assert diary.run(conn, FakeLLM(), db=p, catchup=True) == ["2026-05-16"]


def test_catchup_caps_and_alerts_on_overflow(db):
    p, conn = db
    import datetime as dt
    base = dt.date.today()
    for i in range(1, 6):  # 5 missing days inside the 7d window
        d = (base - dt.timedelta(days=i)).isoformat()
        conn.execute("INSERT INTO events(session_id,timestamp,role,content) "
                     "VALUES('s',?,?,?)", (f"{d}T08:00:00Z", "user", "x"))
    conn.commit()
    written = diary.run(conn, FakeLLM(), db=p, catchup=True)
    assert len(written) == diary.CATCHUP_MAX
    al = conn.execute(
        "SELECT message FROM alerts WHERE type='routine'").fetchone()
    assert al is not None and "still pending" in al["message"]


def test_run_explicit_day(db):
    p, conn = db
    assert diary.run(conn, FakeLLM(), db=p, day="2026-05-16") == ["2026-05-16"]


def test_run_default_targets_yesterday(db, monkeypatch):
    import datetime as dt
    p, conn = db

    class _D(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 5, 17)

    monkeypatch.setattr(diary._dt, "date", _D)
    assert diary.run(conn, FakeLLM(), db=p) == ["2026-05-16"]
