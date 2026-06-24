from marrow import storage


def test_get_latest_watermark_returns_none_when_empty(tmp_path):
    conn = storage.init_db(str(tmp_path / "watermark-empty.db"))
    try:
        assert storage.get_latest_watermark(conn, "sid-empty") is None
    finally:
        conn.close()


def test_insert_watermark_and_get_latest_watermark(tmp_path):
    conn = storage.init_db(str(tmp_path / "watermark-one.db"))
    try:
        storage.insert_watermark(conn, "sid-1", 1, 42, 7)
        row = storage.get_latest_watermark(conn, "sid-1")
        assert row["segment_seq"] == 1
        assert row["last_event_id"] == 42
        assert row["last_turn_idx"] == 7
        assert row["created_at"]
    finally:
        conn.close()


def test_latest_watermark_uses_highest_segment_seq(tmp_path):
    conn = storage.init_db(str(tmp_path / "watermark-many.db"))
    try:
        storage.insert_watermark(conn, "sid-1", 1, 42, 7)
        storage.insert_watermark(conn, "sid-1", 3, 99, 12)
        storage.insert_watermark(conn, "sid-1", 2, 55, 9)
        row = storage.get_latest_watermark(conn, "sid-1")
        assert row["segment_seq"] == 3
        assert row["last_event_id"] == 99
        assert row["last_turn_idx"] == 12
    finally:
        conn.close()
