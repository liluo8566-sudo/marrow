"""Regression: diary since/until window must not leak the day after `until`.

`until` is converted to an EXCLUSIVE UTC boundary (start of the Melbourne day
after the requested until-day). Converting that instant straight back to a
local date yielded until+1, so a since==until window leaked the following day.
"""
from __future__ import annotations

import pytest

from marrow import recall as rm, storage
from marrow.timecue import melb_day_range


@pytest.fixture()
def db(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    yield conn
    conn.close()


def _diary(db, date, content):
    with db:
        db.execute("INSERT INTO diary(date, content) VALUES(?, ?)", (date, content))


def _window_dates(db, day: str) -> set[str]:
    since_utc, until_utc = melb_day_range(day)
    rows = rm.recall_fusion(
        db, "diary", limit=20, min_score=0.0,
        since=since_utc, until=until_utc,
    )
    return {r["date"] for r in rows if r.get("kind") == "diary"}


def test_single_day_window_returns_only_that_day(db):
    _diary(db, "2026-07-05", "day five prose about a feeling")
    _diary(db, "2026-07-06", "day six prose about a feeling")
    _diary(db, "2026-07-07", "day seven prose about a feeling")

    # since == until == 07-06: only 07-06, never 07-05 (since) or 07-07 (leak).
    assert _window_dates(db, "2026-07-06") == {"2026-07-06"}


def test_each_day_isolated_across_three_day_span(db):
    _diary(db, "2026-07-05", "day five prose about a feeling")
    _diary(db, "2026-07-06", "day six prose about a feeling")
    _diary(db, "2026-07-07", "day seven prose about a feeling")

    assert _window_dates(db, "2026-07-05") == {"2026-07-05"}
    assert _window_dates(db, "2026-07-07") == {"2026-07-07"}
