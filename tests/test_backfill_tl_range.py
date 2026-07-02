"""Tests for the backfill_tl_range.py script's line-rewrite logic.

Run: python -m pytest tests/test_backfill_tl_range.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from marrow import config, storage

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts import backfill_tl_range as bf  # noqa: E402


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return db


def _insert_user_events(db: str, sid: str, timestamps: list[str]) -> None:
    conn = storage.connect(db)
    with conn:
        for i, ts in enumerate(timestamps):
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content)"
                " VALUES (?, ?, 'user', ?)",
                (sid, ts, f"msg {i}"),
            )
    conn.close()


# ── _local_hhmm / _rewrite_line / _SINGLE_HHMM_RE ──────────────────────────

def test_local_hhmm_converts_utc_to_melbourne():
    # July = AEST, UTC+10.
    assert bf._local_hhmm("2026-07-01T05:00:00Z") == "15:00"


def test_local_hhmm_cross_midnight():
    assert bf._local_hhmm("2026-07-01T17:09:00Z") == "03:09"


def test_local_hhmm_parse_error_returns_none():
    assert bf._local_hhmm("not-a-timestamp") is None


def test_rewrite_line_produces_range():
    line = "12:00【专注】did the thing"
    out = bf._rewrite_line(line, "11:48", "13:09")
    assert out == "11:48-13:09【专注】did the thing"


def test_rewrite_line_cross_midnight_written_as_is():
    line = "00:00【专注】overnight session"
    out = bf._rewrite_line(line, "21:06", "03:09")
    assert out == "21:06-03:09【专注】overnight session"


def test_rewrite_line_same_minute_skips():
    line = "12:00【专注】did the thing"
    out = bf._rewrite_line(line, "12:00", "12:00")
    assert out is None


def test_single_hhmm_re_does_not_match_existing_range():
    assert bf._SINGLE_HHMM_RE.match("11:48-13:09【专注】x") is None
    assert bf._SINGLE_HHMM_RE.match("11:48【专注】x") is not None


# ── _segment_bounds (session_watermarks) ───────────────────────────────────

def test_segment_bounds_no_watermarks(db_env):
    conn = storage.connect(db_env)
    lower, upper = bf._segment_bounds(conn, "sid-none", 0)
    conn.close()
    assert lower is None and upper is None


def test_segment_bounds_multi_segment(db_env):
    sid = "sid-multi"
    conn = storage.connect(db_env)
    storage.insert_watermark(conn, sid, 1, 100, 5)
    storage.insert_watermark(conn, sid, 2, 200, 10)
    lower0, upper0 = bf._segment_bounds(conn, sid, 0)
    lower1, upper1 = bf._segment_bounds(conn, sid, 1)
    lower2, upper2 = bf._segment_bounds(conn, sid, 2)
    conn.close()
    # segment 0 has no matching watermark (never written for seq=0) -> unbounded
    assert lower0 is None and upper0 is None
    assert lower1 is None and upper1 == 100
    assert lower2 == 100 and upper2 == 200


# ── _build_plan / _apply ────────────────────────────────────────────────────

def test_build_plan_rewrites_task_kind_only(db_env):
    conn = storage.connect(db_env)
    sid_task, sid_casual = "sid-task", "sid-casual"
    _insert_user_events(db_env, sid_task, [
        "2026-07-01T01:00:00Z", "2026-07-01T05:00:00Z",
    ])
    with conn:
        conn.execute(
            "INSERT INTO session_digests (sid, segment_seq, date, text, ts, kind, life_lines)"
            " VALUES (?, 0, '2026-07-01', 'body', '2026-07-01T03:00:00Z', 'task', ?)",
            (sid_task, "13:00【专注】fixed a bug"),
        )
        # Casual row with a legitimate single-HH:MM per-scene LIFE line — must
        # NOT be touched even though it also matches the leading-HH:MM shape.
        conn.execute(
            "INSERT INTO session_digests (sid, segment_seq, date, text, ts, kind, life_lines)"
            " VALUES (?, 0, '2026-07-01', 'body', '2026-07-01T03:00:00Z', 'casual', ?)",
            (sid_casual, "15:12【愉悦】到家晒奶茶"),
        )
    plan = bf._build_plan(conn)
    conn.close()
    by_sid = {p["sid"]: p for p in plan}
    assert sid_task in by_sid
    assert sid_casual not in by_sid
    assert by_sid[sid_task]["action"] == "rewrite"
    assert by_sid[sid_task]["new"] == "11:00-15:00【专注】fixed a bug"


def test_build_plan_skips_no_events(db_env):
    conn = storage.connect(db_env)
    sid = "sid-gone"
    with conn:
        conn.execute(
            "INSERT INTO session_digests (sid, segment_seq, date, text, ts, kind, life_lines)"
            " VALUES (?, 0, '2026-07-01', 'body', '2026-07-01T03:00:00Z', 'task', ?)",
            (sid, "13:00【专注】fixed a bug"),
        )
    plan = bf._build_plan(conn)
    conn.close()
    assert len(plan) == 1
    assert plan[0]["action"] == "skip:no_events"


def test_build_plan_skips_same_minute_span(db_env):
    conn = storage.connect(db_env)
    sid = "sid-samemin"
    _insert_user_events(db_env, sid, [
        "2026-07-01T05:00:05Z", "2026-07-01T05:00:40Z",
    ])
    with conn:
        conn.execute(
            "INSERT INTO session_digests (sid, segment_seq, date, text, ts, kind, life_lines)"
            " VALUES (?, 0, '2026-07-01', 'body', '2026-07-01T03:00:00Z', 'task', ?)",
            (sid, "15:00【专注】quick fix"),
        )
    plan = bf._build_plan(conn)
    conn.close()
    assert len(plan) == 1
    assert plan[0]["action"] == "skip:same_minute"


def test_apply_writes_only_rewrite_rows(db_env):
    conn = storage.connect(db_env)
    sid = "sid-apply"
    _insert_user_events(db_env, sid, [
        "2026-07-01T01:00:00Z", "2026-07-01T05:00:00Z",
    ])
    with conn:
        conn.execute(
            "INSERT INTO session_digests (sid, segment_seq, date, text, ts, kind, life_lines)"
            " VALUES (?, 0, '2026-07-01', 'body', '2026-07-01T03:00:00Z', 'task', ?)",
            (sid, "13:00【专注】fixed a bug"),
        )
    plan = bf._build_plan(conn)
    bf._apply(conn, plan)
    row = conn.execute(
        "SELECT life_lines FROM session_digests WHERE sid=? AND segment_seq=0", (sid,)
    ).fetchone()
    conn.close()
    assert row["life_lines"] == "11:00-15:00【专注】fixed a bug"


def test_apply_is_idempotent_on_rerun(db_env):
    """Second pass over already-range-prefixed rows must be a no-op — the
    script must be safe to re-run after later pipeline changes."""
    conn = storage.connect(db_env)
    sid = "sid-idempotent"
    _insert_user_events(db_env, sid, [
        "2026-07-01T01:00:00Z", "2026-07-01T05:00:00Z",
    ])
    with conn:
        conn.execute(
            "INSERT INTO session_digests (sid, segment_seq, date, text, ts, kind, life_lines)"
            " VALUES (?, 0, '2026-07-01', 'body', '2026-07-01T03:00:00Z', 'task', ?)",
            (sid, "13:00【专注】fixed a bug"),
        )
    plan1 = bf._build_plan(conn)
    bf._apply(conn, plan1)
    assert [p["action"] for p in plan1] == ["rewrite"]

    plan2 = bf._build_plan(conn)
    conn.close()
    assert plan2 == []  # already-range row is skipped entirely on rerun
