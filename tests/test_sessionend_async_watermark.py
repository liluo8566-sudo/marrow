from unittest.mock import patch

from marrow import config, storage
from marrow import sessionend_async


def _insert_event(conn, event_id: int, sid: str, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, role, content)"
        " VALUES (?, ?, ?, ?, ?)",
        (event_id, sid, f"2026-06-01T00:{event_id:02d}:00Z", role, content),
    )


def test_session_events_text_after_event_id_filters_events(tmp_path):
    conn = storage.init_db(str(tmp_path / "events-filter.db"))
    try:
        with conn:
            _insert_event(conn, 1, "sid-1", "user", "old event")
            _insert_event(conn, 2, "sid-1", "assistant", "new event")
        text, date = sessionend_async._session_events_text(conn, "sid-1", 1)
        assert date == "2026-06-01"
        assert "old event" not in text
        assert "new event" in text
    finally:
        conn.close()


def test_session_events_text_without_after_event_id_returns_all(tmp_path):
    conn = storage.init_db(str(tmp_path / "events-all.db"))
    try:
        with conn:
            _insert_event(conn, 1, "sid-1", "user", "old event")
            _insert_event(conn, 2, "sid-1", "assistant", "new event")
        text, date = sessionend_async._session_events_text(conn, "sid-1")
        assert date == "2026-06-01"
        assert "old event" in text
        assert "new event" in text
    finally:
        conn.close()


def test_user_event_count_after_event_id_filters_turns(tmp_path):
    conn = storage.init_db(str(tmp_path / "turns-filter.db"))
    try:
        with conn:
            _insert_event(conn, 1, "sid-1", "user", "old user")
            _insert_event(conn, 2, "sid-1", "assistant", "assistant")
            _insert_event(conn, 3, "sid-1", "user", "new user")
            _insert_event(conn, 4, "sid-1", "user", "new user two")
        assert sessionend_async._user_event_count(conn, "sid-1") == 3
        assert sessionend_async._user_event_count(conn, "sid-1", 2) == 2
    finally:
        conn.close()


def test_mid_session_main_writes_watermark(tmp_path, monkeypatch):
    db = str(tmp_path / "mid-session.db")
    conn = storage.init_db(db)
    sid = "sid-mid"
    try:
        with conn:
            for event_id in range(1, 4):
                _insert_event(conn, event_id, sid, "user", f"old {event_id}")
            for event_id in range(4, 8):
                _insert_event(conn, event_id, sid, "user", f"new {event_id}")
    finally:
        conn.close()

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: db)
    raw = """===DIGEST===
KIND: casual
TL: mid session anchor
LIFE:
- user made progress
VOICE:
- steady
FACTS:
- one segment extracted
===END==="""

    with patch("marrow.sessionend_async.LLMClient") as mock_client, \
            patch("marrow.dashboard.write_dashboard"), \
            patch("marrow.recall.embed_pending"):
        mock_client.return_value.call.return_value = raw
        rc = sessionend_async.main(
            ["--sid", sid, "--after-event-id", "3", "--segment-seq", "1"]
        )

    assert rc == 0
    conn = storage.connect(db)
    try:
        wm = storage.get_latest_watermark(conn, sid)
        assert wm["segment_seq"] == 1
        assert wm["last_event_id"] == 7
        assert wm["last_turn_idx"] == 4
        row = conn.execute(
            "SELECT segment_seq, text FROM session_digests WHERE sid=?",
            (sid,),
        ).fetchone()
        assert row["segment_seq"] == 1
        assert "user made progress" in row["text"]
    finally:
        conn.close()


def test_mid_session_watermark_written_when_affect_fails(tmp_path, monkeypatch):
    """Watermark must be written even if affect writer fails, as long as digest succeeds."""
    db = str(tmp_path / "partial-fail.db")
    conn = storage.init_db(db)
    sid = "sid-partial"
    try:
        with conn:
            for event_id in range(1, 4):
                _insert_event(conn, event_id, sid, "user", f"old {event_id}")
            for event_id in range(4, 8):
                _insert_event(conn, event_id, sid, "user", f"new {event_id}")
    finally:
        conn.close()

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "db_path", lambda: db)
    raw = """===DIGEST===
KIND: casual
TL: partial failure anchor
LIFE:
- affect writer failed
VOICE:
- steady
FACTS:
- digest still wrote
===END==="""

    with patch("marrow.sessionend_async.LLMClient") as mock_client, \
            patch("marrow.dashboard.write_dashboard"), \
            patch("marrow.recall.embed_pending"), \
            patch("marrow.sessionend_async.seg_affect", side_effect=RuntimeError("affect boom")):
        mock_client.return_value.call.return_value = raw
        rc = sessionend_async.main(
            ["--sid", sid, "--after-event-id", "3", "--segment-seq", "1"]
        )

    # partial failure -> rc should still be 0
    assert rc == 0
    conn = storage.connect(db)
    try:
        wm = storage.get_latest_watermark(conn, sid)
        assert wm is not None, "watermark must be written when digest succeeds despite affect failure"
        assert wm["segment_seq"] == 1
        assert wm["last_event_id"] == 7
    finally:
        conn.close()
