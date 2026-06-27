"""Tests for per-line anchor emission (Phase 1a).

Covers:
- Each life_line row gets its own <!-- tl:sid:seq:LN --> anchor
- segment_seq=1 renders correctly in anchor
- Trail marker captures all per-line anchor values
- Manual events still use <!-- tl:e:ID --> (no regression)
"""
from __future__ import annotations

import datetime as _dt
import re

import pytest

from marrow import storage
from marrow.timeline import _tl_anchor_sid, render_timeline


# ── helpers ──────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@pytest.fixture()
def conn(tmp_path):
    db = str(tmp_path / "test.db")
    c = storage.init_db(db)
    yield c
    c.close()


def _insert_digest(conn, sid: str, life_lines: str, segment_seq: int = 0) -> None:
    ts = _now_utc()
    conn.execute(
        "INSERT INTO session_digests"
        " (sid, segment_seq, date, ts, text, kind, life_lines)"
        " VALUES (?, ?, ?, ?, 'body', 'casual', ?)",
        (sid, segment_seq, ts[:10], ts, life_lines),
    )
    conn.commit()


_TRAIL_SID_RE = re.compile(r"<!-- tl-rendered:[^>]*?s=([^;>]+?)\s*-->")


def _trail_sids(text: str) -> list[str]:
    m = _TRAIL_SID_RE.search(text)
    if not m:
        return []
    return m.group(1).split(",")


# ── unit: _tl_anchor_sid ─────────────────────────────────────────────────────

def test_anchor_with_line_index_seq_zero():
    assert _tl_anchor_sid("sid", 0, line_index=0) == "<!-- tl:sid:0:0 -->"


def test_anchor_with_line_index_seq_one():
    assert _tl_anchor_sid("sid", 1, line_index=0) == "<!-- tl:sid:1:0 -->"


def test_anchor_with_line_index_seq_zero_shown():
    # seq ALWAYS shown when line_index provided, even if 0
    assert ":0:" in _tl_anchor_sid("sid", 0, line_index=2)


def test_anchor_backward_compat_no_line_index():
    # existing callers without line_index unchanged
    assert _tl_anchor_sid("sid") == "<!-- tl:sid -->"
    assert _tl_anchor_sid("sid", 1) == "<!-- tl:sid:1 -->"


# ── integration: per-line anchors in render ───────────────────────────────────

def test_three_life_lines_get_individual_anchors(conn):
    """3 life_lines → 3 separate anchors tl:SID:0:0, :0:1, :0:2."""
    sid = "sid-abc"
    _insert_digest(conn, sid, "09:00 wake up\n10:00 breakfast\n11:00 study")
    result = render_timeline(conn)
    assert f"<!-- tl:{sid}:0:0 -->" in result
    assert f"<!-- tl:{sid}:0:1 -->" in result
    assert f"<!-- tl:{sid}:0:2 -->" in result


def test_segment_seq_one_anchor(conn):
    """segment_seq=1 → anchor tl:SID:1:0."""
    sid = "sid-seg1"
    _insert_digest(conn, sid, "14:00 afternoon", segment_seq=1)
    result = render_timeline(conn)
    assert f"<!-- tl:{sid}:1:0 -->" in result


def test_no_old_session_level_anchor_emitted(conn):
    """Old shared anchor tl:SID (without line index) no longer emitted for life_lines."""
    sid = "sid-noold"
    _insert_digest(conn, sid, "09:00 wake\n10:00 read")
    result = render_timeline(conn)
    # Old format would be exactly "<!-- tl:sid-noold -->" or "<!-- tl:sid-noold:0 -->"
    assert f"<!-- tl:{sid} -->" not in result
    assert f"<!-- tl:{sid}:0 -->" not in result


def test_trail_includes_all_line_anchors(conn):
    """Trail marker s= field lists all per-line anchor values."""
    sid = "sid-trail2"
    _insert_digest(conn, sid, "09:00 a\n10:00 b\n11:00 c")
    result = render_timeline(conn)
    trail = _trail_sids(result)
    assert f"{sid}:0:0" in trail
    assert f"{sid}:0:1" in trail
    assert f"{sid}:0:2" in trail


def test_manual_event_anchor_unchanged(conn):
    """Manual events still use <!-- tl:e:ID --> format."""
    ts = _now_utc()
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel)"
        " VALUES ('manual:xx', ?, 'user', '手动事件', 'manual')",
        (ts,),
    )
    conn.commit()
    eid = conn.execute("SELECT id FROM events WHERE channel='manual'").fetchone()["id"]
    result = render_timeline(conn)
    assert f"<!-- tl:e:{eid} -->" in result
    # must not produce a sid-style anchor for manual events
    assert "<!-- tl:manual:" not in result
