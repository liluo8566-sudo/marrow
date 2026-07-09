"""Tests for recall render helpers and relative timestamp formatting.

Covers:
- format_recall_ts buckets: just now / Xm / Xh / Xd / Xw / Xmo
- hooks._apply_rel_cutoff: top1*rel_cutoff gate
- hooks._render_hit_block: rank_caps respected per rank, anchor truncation,
  context only at rank-0 events
- daemon recall context param plumbing (calls fetch_event_context)
"""
from __future__ import annotations

import datetime
import re
import struct
from unittest.mock import MagicMock, patch

import pytest

from marrow.timeutil import format_recall_ts, reltime_short
from marrow.hooks import _apply_rel_cutoff, _render_hit_block


# ── helpers ───────────────────────────────────────────────────────────────────

_UTC = datetime.timezone.utc


def _ts(delta_secs: float) -> str:
    """Return UTC ISO string `delta_secs` before a fixed reference."""
    ref = datetime.datetime(2026, 6, 10, 12, 0, 0, tzinfo=_UTC)
    return (ref - datetime.timedelta(seconds=delta_secs)).isoformat()


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 6, 10, 12, 0, 0, tzinfo=_UTC)


def _hit(score: float, content: str = "x", kind: str | None = None,
         eid: int = 1, sid: str = "s1") -> dict:
    return {
        "id": eid, "session_id": sid, "kind": kind,
        "score": score, "content": content,
        "timestamp": _ts(60), "role": "user", "channel": None,
    }


# ── format_recall_ts buckets ──────────────────────────────────────────────────

class TestFormatRecallTs:
    def test_just_now(self):
        ts = _ts(30)
        label = format_recall_ts(ts, now=_now())
        assert "just now" in label

    def test_minutes(self):
        ts = _ts(90)   # 1.5 min
        label = format_recall_ts(ts, now=_now())
        assert "1m ago" in label

    def test_hours(self):
        ts = _ts(7200)  # 2h
        label = format_recall_ts(ts, now=_now())
        assert "2h ago" in label

    def test_days(self):
        ts = _ts(3 * 86400)  # 3d
        label = format_recall_ts(ts, now=_now())
        assert "3d ago" in label

    def test_weeks(self):
        ts = _ts(14 * 86400)  # 14d = 2w
        label = format_recall_ts(ts, now=_now())
        assert "2w ago" in label

    def test_months(self):
        ts = _ts(60 * 86400)  # 60d ≈ 2mo
        label = format_recall_ts(ts, now=_now())
        assert "mo ago" in label

    def test_abs_part_present(self):
        # Absolute part is always in the label (MM-DD Day format).
        ts = _ts(3600)
        label = format_recall_ts(ts, now=_now())
        assert "[" in label and "·" in label

    def test_empty_string(self):
        assert format_recall_ts("") == ""

    def test_fallback_on_bad_input(self):
        # Bad parse → falls back to first-10-char slice.
        label = format_recall_ts("not-a-date")
        assert "not-a-dat" in label


# ── reltime_short buckets ─────────────────────────────────────────────────────
# Short single-token label (no 'ago' suffix) used by daemon.recall()'s `when`
# field: <24h -> Xh; <7d -> Xd; 7d-365d -> MM-DD (Melbourne local);
# >=365d -> YYYY (Melbourne local). Distinct from format_recall_ts, which
# still backs hooks.py's hit-block renderer / recall log writer.

class TestReltimeShort:
    def test_hours_bucket(self):
        ts = _ts(5 * 3600)  # 5h
        assert reltime_short(ts, now=_now()) == "5h"

    def test_sub_hour_floors_to_zero(self):
        ts = _ts(1800)  # 30 min — no minute bucket in this format
        assert reltime_short(ts, now=_now()) == "0h"

    def test_days_under_week_bucket(self):
        ts = _ts(3 * 86400)  # 3d
        assert reltime_short(ts, now=_now()) == "3d"

    def test_exactly_seven_days_falls_into_date_bucket(self):
        # Exactly 7d is NOT < 7d — falls into the MM-DD bucket, not '7d'.
        ts = _ts(7 * 86400)
        assert re.fullmatch(r"\d{2}-\d{2}", reltime_short(ts, now=_now()))

    def test_month_scale_renders_mm_dd(self):
        ts = _ts(30 * 86400)  # 30d
        assert re.fullmatch(r"\d{2}-\d{2}", reltime_short(ts, now=_now()))

    def test_year_plus_renders_year_only(self):
        ts = _ts(400 * 86400)  # >1yr
        assert re.fullmatch(r"\d{4}", reltime_short(ts, now=_now()))

    def test_no_ago_suffix_anywhere(self):
        for delta in (3600, 3 * 86400, 30 * 86400, 400 * 86400):
            assert "ago" not in reltime_short(_ts(delta), now=_now())

    def test_empty_string(self):
        assert reltime_short("") == ""

    def test_fallback_on_bad_input(self):
        assert reltime_short("not-a-date") == ""


# ── _apply_rel_cutoff ─────────────────────────────────────────────────────────

class TestApplyRelCutoff:
    def test_drops_below_cutoff(self):
        hits = [_hit(1.0), _hit(0.7), _hit(0.5), _hit(0.3)]
        # cutoff = 1.0 * 0.6 = 0.6 → keep ≥0.6
        result = _apply_rel_cutoff(hits, 0.6)
        scores = [h["score"] for h in result]
        assert scores == [1.0, 0.7]

    def test_keeps_all_above(self):
        hits = [_hit(0.8), _hit(0.8)]
        result = _apply_rel_cutoff(hits, 0.6)
        assert len(result) == 2

    def test_empty_input(self):
        assert _apply_rel_cutoff([], 0.6) == []

    def test_top1_anchor_row_survives(self):
        # top1 score is the reference; it always survives (score == cutoff boundary)
        hits = [_hit(0.5)]
        result = _apply_rel_cutoff(hits, 0.6)
        assert len(result) == 1  # 0.5 * 0.6 = 0.3; 0.5 >= 0.3

    def test_strict_drop(self):
        hits = [_hit(1.0), _hit(0.59)]
        result = _apply_rel_cutoff(hits, 0.6)
        assert len(result) == 1  # 0.59 < 0.60


# ── _render_hit_block rank caps ───────────────────────────────────────────────

class TestRenderHitBlock:
    _caps = [300, 120, 120, 40, 40]

    def _long_content(self, n: int) -> str:
        return "a" * n

    def test_rank0_event_cap_300(self):
        h = _hit(1.0, content=self._long_content(500))
        block = _render_hit_block(0, h, self._caps)
        # The main bullet line content should be ≤300 chars (cap for rank 0).
        main_line = block[0]
        # Extract content after the "<head>: " marker
        content_part = main_line.split(": ", 1)[-1]
        assert len(content_part) <= 300

    def test_rank1_cap_120(self):
        h = _hit(0.9, content=self._long_content(500))
        block = _render_hit_block(1, h, self._caps)
        main_line = block[0]
        content_part = main_line.split(": ", 1)[-1]
        assert len(content_part) <= 120

    def test_rank3_cap_40(self):
        h = _hit(0.7, content=self._long_content(200))
        block = _render_hit_block(3, h, self._caps)
        main_line = block[0]
        content_part = main_line.split(": ", 1)[-1]
        assert len(content_part) <= 40

    def test_rank_beyond_list_uses_last(self):
        # rank 10 → falls back to caps[-1] = 40
        h = _hit(0.5, content=self._long_content(200))
        block = _render_hit_block(10, h, self._caps)
        # Should not raise; content capped at 40
        main_line = block[0]
        content_part = main_line.split(": ", 1)[-1]
        assert len(content_part) <= 40

    def test_anchor_truncated_no_context(self):
        h = {**_hit(0.8, content=self._long_content(200)), "kind": "milestone"}
        block = _render_hit_block(0, h, self._caps)
        # Anchor rows: exactly 1 line, no context bullets.
        assert len(block) == 1
        content_part = block[0].split(": ", 1)[-1]
        assert len(content_part) <= 300

    def test_rank0_event_context_included(self):
        h = _hit(1.0, content=self._long_content(100))
        h["_context"] = [
            {"id": 0, "role": "assistant", "content": "ctx turn",
             "timestamp": _ts(120), "rel": "prev"},
        ]
        block = _render_hit_block(0, h, self._caps)
        # Should have the main line + at least one context line.
        assert len(block) >= 2
        ctx_line = block[1]
        assert "↑" in ctx_line  # prev indicator
        assert "ctx turn" in ctx_line

    def test_rank1_event_no_context(self):
        h = _hit(0.9, content="hello")
        h["_context"] = [
            {"id": 0, "role": "user", "content": "ignored ctx",
             "timestamp": _ts(60), "rel": "prev"},
        ]
        block = _render_hit_block(1, h, self._caps)
        # Context only rendered at rank 0.
        assert len(block) == 1
        assert "ignored ctx" not in block[0]

    def test_render_hit_block_rank_arg_passthrough(self):
        """rank param correctly routes rank 0 vs rank 1 cap."""
        content = self._long_content(500)
        b0 = _render_hit_block(0, _hit(1.0, content=content), self._caps)
        b1 = _render_hit_block(1, _hit(1.0, content=content), self._caps)
        # rank-0 line is longer (cap=300) than rank-1 (cap=120).
        assert len(b0[0]) > len(b1[0])


# ── fix call with missing `h` arg in one test above ──────────────────────────

# (The test_rank_beyond_list_uses_last above had a typo — h missing. Fix it:)
def test_render_rank_beyond_list_uses_last_cap():
    caps = [300, 120, 120, 40, 40]
    h = {"id": 1, "session_id": "s", "kind": None,
         "score": 0.5, "content": "a" * 200,
         "timestamp": _ts(60), "role": "user"}
    block = _render_hit_block(10, h, caps)
    main_line = block[0]
    content_part = main_line.split(": ", 1)[-1]
    assert len(content_part) <= 40


# ── per-kind recall head format ──────────────────────────────────────────────

_CAPS = [300, 120, 120, 40, 40]


def test_head_event_channel_and_id():
    h = {**_hit(1.0, content="hello"), "channel": "wx"}
    line = _render_hit_block(0, h, _CAPS)[0]
    assert line.startswith("- [wx ")
    assert "] ev#1: hello" in line


def test_head_event_channel_fallback_cli():
    h = _hit(1.0, content="hi")  # channel None
    line = _render_hit_block(0, h, _CAPS)[0]
    assert line.startswith("- [cli ")
    assert "ev#1: hi" in line


def test_head_milestone_strips_t00():
    h = {**_hit(0.8, content="grad", kind="milestone"),
         "timestamp": "2013-03-15T00:00:00Z"}
    line = _render_hit_block(0, h, _CAPS)[0]
    assert "T00:00" not in line
    assert line == "- [2013-03-15] ms#1: grad"


def test_head_milestone_year_only():
    h = {**_hit(0.8, content="born", kind="milestone"),
         "timestamp": "2013T00:00:00Z"}
    line = _render_hit_block(0, h, _CAPS)[0]
    assert line == "- [2013] ms#1: born"


def test_head_entity_no_time():
    h = {**_hit(0.8, content="Melbourne", kind="entity"),
         "timestamp": "2026-01-01T00:00:00Z"}
    line = _render_hit_block(0, h, _CAPS)[0]
    assert line == "- en#1: Melbourne"


def test_head_meme_empty_ts_no_bracket():
    h = {**_hit(0.8, content="梗: 内容", kind="memes"), "timestamp": ""}
    line = _render_hit_block(0, h, _CAPS)[0]
    assert line == "- me#1: 梗: 内容"


# ── mcp recall context param plumbing ────────────────────────────────────────

def test_daemon_recall_context_param(tmp_path):
    """context=True attaches _context to event rows; context=False does not."""
    from marrow import storage, repo, daemon as _daemon_mod

    db_path = str(tmp_path / "t.db")
    conn = storage.init_db(db_path)

    # Insert one event.
    repo.archive_events(conn, [{
        "session_id": "sess1",
        "timestamp": "2026-06-01T10:00:00Z",
        "role": "user",
        "content": "hello world test context",
    }])
    # Insert a neighbouring event so fetch_event_context has something.
    repo.archive_events(conn, [{
        "session_id": "sess1",
        "timestamp": "2026-06-01T10:00:01Z",
        "role": "assistant",
        "content": "response turn",
    }])
    conn.close()

    # Patch _DB + recall_with_config to return one event row.
    fake_hit = {
        "id": 1, "session_id": "sess1", "kind": None,
        "score": 0.8, "content": "hello world test context",
        "timestamp": "2026-06-01T10:00:00Z", "role": "user", "channel": None,
    }

    import marrow.recall as _recall_mod

    with patch.object(_daemon_mod, "_DB", db_path), \
         patch.object(_daemon_mod._recall_mod, "recall_with_config",
                      return_value=[dict(fake_hit)]) as mock_rwc:
        result_no_ctx = _daemon_mod.recall("test", context=False)
        assert "_context" not in result_no_ctx[0]
        assert "when" in result_no_ctx[0]  # when field always present

    with patch.object(_daemon_mod, "_DB", db_path), \
         patch.object(_daemon_mod._recall_mod, "recall_with_config",
                      return_value=[dict(fake_hit)]):
        result_ctx = _daemon_mod.recall("test", context=True)
        # _context key should be present for event row with valid session_id + id.
        assert "_context" in result_ctx[0]
