"""Tests for the P0 render-timestamp gate + block terminator (mw-daybrief-bidir).

Covers:
- carry_trail_t carries old t= when block content unchanged, stamps new t= when a line changes.
- reconcile_timeline gate reads t= (render timestamp), not file mtime: a Status-zone
  rewrite bumps mtime but the DB row updated after the render still wins (edit not absorbed).
- block terminator: <!-- marrow:timeline:end --> ends the block before trailing H3 sections.
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

import pytest

from marrow import storage, timeline
from marrow.reconcile import reconcile_timeline


@pytest.fixture()
def conn(tmp_path):
    db = str(tmp_path / "tg.db")
    c = storage.init_db(db)
    yield c
    c.close()


def _utc(delta_s: float = 0.0) -> str:
    dt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=delta_s)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── carry_trail_t ────────────────────────────────────────────────────────────

def test_carry_trail_t_unchanged_carries_old_t():
    old = "## Timeline\n14:00 a <!-- tl:s1 -->\n<!-- tl-rendered:s=s1;t=2020-01-01T00:00:00Z -->"
    new = "## Timeline\n14:00 a <!-- tl:s1 -->\n<!-- tl-rendered:s=s1;t=2026-07-12T00:00:00Z -->"
    out = timeline.carry_trail_t(new, old)
    assert "t=2020-01-01T00:00:00Z" in out


def test_carry_trail_t_changed_keeps_new_t():
    old = "## Timeline\n14:00 a <!-- tl:s1 -->\n<!-- tl-rendered:s=s1;t=2020-01-01T00:00:00Z -->"
    new = "## Timeline\n14:00 EDITED <!-- tl:s1 -->\n<!-- tl-rendered:s=s1;t=2026-07-12T00:00:00Z -->"
    out = timeline.carry_trail_t(new, old)
    assert "t=2026-07-12T00:00:00Z" in out


def test_carry_trail_t_no_old_block_keeps_new():
    new = "## Timeline\n14:00 a <!-- tl:s1 -->\n<!-- tl-rendered:s=s1;t=2026-07-12T00:00:00Z -->"
    assert timeline.carry_trail_t(new, None) == new


def test_carry_trail_t_old_missing_t_keeps_new():
    old = "## Timeline\n14:00 a <!-- tl:s1 -->\n<!-- tl-rendered:s=s1 -->"
    new = "## Timeline\n14:00 a <!-- tl:s1 -->\n<!-- tl-rendered:s=s1;t=2026-07-12T00:00:00Z -->"
    out = timeline.carry_trail_t(new, old)
    assert "t=2026-07-12T00:00:00Z" in out


# ── gate uses t= not mtime ───────────────────────────────────────────────────

def test_gate_uses_trail_t_not_mtime(conn, tmp_path):
    """Render at T0, then a Status-only rewrite bumps mtime past a DB row that was
    updated after T0. With the t= gate the DB row still wins → the hand-edit is
    NOT absorbed (row_ts > t=)."""
    sid = "sid-gate"
    t0 = _utc(-120)            # render moment (2 min ago)
    row_ts = _utc(-60)         # DB row updated 1 min ago (AFTER render)
    conn.execute(
        "INSERT INTO session_digests"
        " (sid, segment_seq, date, ts, text, kind, life_lines, updated_at)"
        " VALUES (?, 0, ?, ?, 'body', 'casual', ?, ?)",
        (sid, t0[:10], t0, "原始行", row_ts),
    )
    conn.commit()

    path = tmp_path / "daybrief.md"
    path.write_text(
        "## Timeline\n"
        f"14:00 手改了 <!-- tl:{sid}:0:0 -->\n"
        f"<!-- tl-rendered:s={sid}:0:0;t={t0} -->\n"
    )
    # Simulate a Status-zone rewrite far in the future → mtime jumps ahead of row_ts.
    future = _dt.datetime.now().timestamp() + 3600
    os.utime(path, (future, future))

    rpt = reconcile_timeline(conn, path)

    # DB wins: edit skipped, row unchanged.
    assert rpt.updated == 0
    life = conn.execute(
        "SELECT life_lines FROM session_digests WHERE sid=? AND segment_seq=0", (sid,)
    ).fetchone()["life_lines"]
    assert life == "原始行"


def test_gate_absorbs_edit_when_row_older_than_t(conn, tmp_path):
    """Sanity mirror: DB row updated BEFORE the render → hand-edit is absorbed."""
    sid = "sid-gate2"
    row_ts = _utc(-180)        # DB row 3 min ago
    t0 = _utc(-60)             # render 1 min ago (AFTER the row)
    conn.execute(
        "INSERT INTO session_digests"
        " (sid, segment_seq, date, ts, text, kind, life_lines, updated_at)"
        " VALUES (?, 0, ?, ?, 'body', 'casual', ?, ?)",
        (sid, row_ts[:10], row_ts, "原始行", row_ts),
    )
    conn.commit()

    path = tmp_path / "daybrief.md"
    path.write_text(
        "## Timeline\n"
        f"14:00 手改了 <!-- tl:{sid}:0:0 -->\n"
        f"<!-- tl-rendered:s={sid}:0:0;t={t0} -->\n"
    )
    rpt = reconcile_timeline(conn, path)

    assert rpt.updated == 1
    life = conn.execute(
        "SELECT life_lines FROM session_digests WHERE sid=? AND segment_seq=0", (sid,)
    ).fetchone()["life_lines"]
    assert life == "14:00 手改了"


# ── block terminator ─────────────────────────────────────────────────────────

def test_end_marker_stops_block_before_h3(conn, tmp_path):
    """Lines after <!-- marrow:timeline:end --> (e.g. ### First H3 sections) are
    never parsed as timeline content → no phantom insert / edit from them."""
    sid = "sid-term"
    t0 = _utc(-60)
    conn.execute(
        "INSERT INTO session_digests"
        " (sid, segment_seq, date, ts, text, kind, life_lines, updated_at)"
        " VALUES (?, 0, ?, ?, 'body', 'casual', ?, ?)",
        (sid, t0[:10], t0, "原始行", t0),
    )
    conn.commit()

    path = tmp_path / "daybrief.md"
    path.write_text(
        "## Timeline\n"
        f"14:00 原始行 <!-- tl:{sid}:0:0 -->\n"
        f"<!-- tl-rendered:s={sid}:0:0;t={t0} -->\n"
        "<!-- marrow:timeline:end -->\n"
        "### First\n"
        "+ 09:00 这行不该被当成 timeline 插入\n"
        "### Timetrack\n"
        "+ 10:00 这行也不该\n"
    )
    rpt = reconcile_timeline(conn, path)

    # No manual events inserted from the H3 zone.
    assert rpt.inserted == 0
    n_events = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
    assert n_events == 0
