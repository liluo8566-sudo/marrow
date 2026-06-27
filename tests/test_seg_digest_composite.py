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
