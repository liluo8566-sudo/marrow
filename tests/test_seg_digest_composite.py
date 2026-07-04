from marrow import storage
from marrow.sessionend_writers import seg_digest


def _raw(body: str) -> str:
    return f"===DIGEST===\n{body}\n===END==="


def test_seg_digest_segment_zero_backward_compat(tmp_path):
    conn = storage.init_db(str(tmp_path / "digest-zero.db"))
    try:
        n = seg_digest(conn, _raw("first body"), "sid-1", "2026-06-01")
        assert n == 1
        row = conn.execute(
            "SELECT sid, segment_seq, text FROM session_digests"
            " WHERE sid='sid-1'"
        ).fetchone()
        assert row["segment_seq"] == 0
        assert row["text"] == "first body"
    finally:
        conn.close()


def test_seg_digest_segment_one_mid_session(tmp_path):
    conn = storage.init_db(str(tmp_path / "digest-one.db"))
    try:
        n = seg_digest(
            conn, _raw("mid body"), "sid-1", "2026-06-01", segment_seq=1
        )
        assert n == 1
        row = conn.execute(
            "SELECT sid, segment_seq, text FROM session_digests"
            " WHERE sid='sid-1'"
        ).fetchone()
        assert row["segment_seq"] == 1
        assert row["text"] == "mid body"
    finally:
        conn.close()


def test_seg_digest_same_sid_different_segments_create_rows(tmp_path):
    conn = storage.init_db(str(tmp_path / "digest-many.db"))
    try:
        seg_digest(conn, _raw("base body"), "sid-1", "2026-06-01")
        seg_digest(conn, _raw("mid body"), "sid-1", "2026-06-01", segment_seq=1)
        rows = conn.execute(
            "SELECT segment_seq, text FROM session_digests"
            " WHERE sid='sid-1' ORDER BY segment_seq"
        ).fetchall()
        assert [(r["segment_seq"], r["text"]) for r in rows] == [
            (0, "base body"),
            (1, "mid body"),
        ]
    finally:
        conn.close()


def test_seg_digest_ts_is_midpoint_of_user_messages(tmp_path):
    """ts should be midpoint of first/last user msg, not datetime.now()."""
    import datetime as _dt
    conn = storage.init_db(str(tmp_path / "digest-ts.db"))
    try:
        with conn:
            # user msgs at 10:00 and 10:40 → midpoint 10:20
            conn.execute(
                "INSERT INTO events (id, session_id, timestamp, role, content)"
                " VALUES (1, 'sid-ts', '2026-06-01T10:00:00Z', 'user', 'first')"
            )
            conn.execute(
                "INSERT INTO events (id, session_id, timestamp, role, content)"
                " VALUES (2, 'sid-ts', '2026-06-01T10:40:00Z', 'user', 'last')"
            )
        n = seg_digest(conn, _raw("body"), "sid-ts", "2026-06-01")
        assert n == 1
        row = conn.execute(
            "SELECT ts FROM session_digests WHERE sid='sid-ts'"
        ).fetchone()
        assert row["ts"] == "2026-06-01T10:20:00Z"
    finally:
        conn.close()


def test_seg_digest_ts_midpoint_with_after_event_id(tmp_path):
    """after_event_id filters which user msgs are used for midpoint."""
    import datetime as _dt
    conn = storage.init_db(str(tmp_path / "digest-ts-after.db"))
    try:
        with conn:
            # event 1 is from the prior segment (before watermark id=1)
            conn.execute(
                "INSERT INTO events (id, session_id, timestamp, role, content)"
                " VALUES (1, 'sid-ts2', '2026-06-01T08:00:00Z', 'user', 'old')"
            )
            # events 2 and 4 are user msgs in this segment
            conn.execute(
                "INSERT INTO events (id, session_id, timestamp, role, content)"
                " VALUES (2, 'sid-ts2', '2026-06-01T10:00:00Z', 'user', 'seg first')"
            )
            conn.execute(
                "INSERT INTO events (id, session_id, timestamp, role, content)"
                " VALUES (3, 'sid-ts2', '2026-06-01T10:20:00Z', 'assistant', 'reply')"
            )
            conn.execute(
                "INSERT INTO events (id, session_id, timestamp, role, content)"
                " VALUES (4, 'sid-ts2', '2026-06-01T11:00:00Z', 'user', 'seg last')"
            )
        # after_event_id=1 → only events 2,3,4 in scope; user msgs at 10:00 and 11:00
        # midpoint = 10:30
        n = seg_digest(conn, _raw("body"), "sid-ts2", "2026-06-01",
                       after_event_id=1)
        assert n == 1
        row = conn.execute(
            "SELECT ts FROM session_digests WHERE sid='sid-ts2'"
        ).fetchone()
        assert row["ts"] == "2026-06-01T10:30:00Z"
    finally:
        conn.close()


def test_seg_digest_ts_fallback_to_now_when_no_user_msgs(tmp_path, monkeypatch):
    """Falls back to now() when no user messages exist in segment."""
    import datetime as _dt

    frozen = _dt.datetime(2026, 6, 1, 9, 0, 0, tzinfo=_dt.timezone.utc)

    class FrozenDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    import marrow.sessionend_writers as _sw
    monkeypatch.setattr(_sw._dt, "datetime", FrozenDT)

    conn = storage.init_db(str(tmp_path / "digest-ts-fallback.db"))
    try:
        # no events at all
        n = seg_digest(conn, _raw("body"), "sid-empty", "2026-06-01")
        assert n == 1
        row = conn.execute(
            "SELECT ts FROM session_digests WHERE sid='sid-empty'"
        ).fetchone()
        assert row["ts"] == "2026-06-01T09:00:00Z"
    finally:
        conn.close()
