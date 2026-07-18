"""D1/D2 tests — events freshness arbitration + carry_trail_t absorb deadlock.

D2: events rows now gate on COALESCE(updated_at, created_at) vs the render t=.
    The stale second surface (dashboard.md ⇄ daybrief.md) must NOT revert a
    newer DB row → no ping-pong.
D1: after a reconcile absorbs an edit, the writer stamps a fresh t= (absorbed
    flag), so a SECOND edit of the SAME line also lands (no db-win deadlock).
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from marrow import storage, timeline
from marrow.reconcile import reconcile_timeline


@pytest.fixture()
def conn(tmp_path):
    db = str(tmp_path / "eg.db")
    c = storage.init_db(db)
    yield c
    c.close()


def _utc(delta_s: float = 0.0) -> str:
    dt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=delta_s)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_self(conn, content: str, created_at: str,
                 updated_at: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel,"
        " created_at, updated_at)"
        " VALUES ('s1', ?, 'tl', ?, 'cli', ?, ?)",
        (created_at, content, created_at, updated_at),
    )
    conn.commit()
    return cur.lastrowid


# ── D2: stale surface must not revert a newer DB row ─────────────────────────

def test_stale_surface_does_not_revert_newer_event_row(conn, tmp_path):
    """DB row content-written AFTER the render (updated_at > t=) AND md differs
    → md is the stale copy from the other surface; DB wins, no revert."""
    t0 = _utc(-120)             # render moment
    row_ts = _utc(-60)          # DB row edited AFTER render (fresh)
    eid = _insert_self(conn, "【a】新值", created_at=t0, updated_at=row_ts)

    path = tmp_path / "daybrief.md"
    # md still carries the OLD text (stale second surface).
    path.write_text(
        "## Timeline\n"
        f"14:00 【a】旧值 <!-- tl:e:{eid} -->\n"
        f"<!-- tl-rendered:e={eid};t={t0} -->\n"
    )
    rpt = reconcile_timeline(conn, path)

    assert rpt.updated == 0, "stale md must not overwrite the newer DB row"
    content = conn.execute(
        "SELECT content FROM events WHERE id=?", (eid,)
    ).fetchone()["content"]
    assert content == "【a】新值"


def test_fresh_edit_absorbs_when_row_older_than_render(conn, tmp_path):
    """Sanity mirror: DB row older than the render → the md hand-edit is
    absorbed into events.content and updated_at is stamped."""
    row_ts = _utc(-180)
    t0 = _utc(-60)
    eid = _insert_self(conn, "【a】旧值", created_at=row_ts, updated_at=row_ts)

    path = tmp_path / "daybrief.md"
    path.write_text(
        "## Timeline\n"
        f"14:00 【a】改后 <!-- tl:e:{eid} -->\n"
        f"<!-- tl-rendered:e={eid};t={t0} -->\n"
    )
    rpt = reconcile_timeline(conn, path)

    assert rpt.updated == 1
    row = conn.execute(
        "SELECT content, updated_at FROM events WHERE id=?", (eid,)
    ).fetchone()
    assert row["content"] == "【a】改后"
    assert row["updated_at"] is not None  # stamped on absorb


# ── D1: second edit of the same line also absorbs (no deadlock) ──────────────

def test_second_edit_of_same_line_absorbs(conn, tmp_path):
    """First edit absorbs (updated_at → now). carry_trail_t(absorbed=True) then
    stamps t=now on the re-render, so the SECOND edit's gate sees t= >= the
    just-written row and absorbs too — no permanent db-win deadlock."""
    row_ts = _utc(-300)
    t0 = _utc(-240)
    eid = _insert_self(conn, "【a】原始", created_at=row_ts, updated_at=row_ts)

    path = tmp_path / "daybrief.md"
    # --- edit #1 ---
    path.write_text(
        "## Timeline\n"
        f"14:00 【a】第一次改 <!-- tl:e:{eid} -->\n"
        f"<!-- tl-rendered:e={eid};t={t0} -->\n"
    )
    rpt1 = reconcile_timeline(conn, path)
    assert rpt1.updated == 1
    r1 = conn.execute("SELECT content, updated_at FROM events WHERE id=?", (eid,)).fetchone()
    assert r1["content"] == "【a】第一次改"
    row_after_1 = r1["updated_at"]  # freshly stamped, ~now

    # Writer re-renders after absorb: carry_trail_t(absorbed=True) keeps t=now.
    # Simulate the fresh trail the writer would emit (t >= row_after_1).
    old_block = (
        "## Timeline\n"
        f"14:00 【a】第一次改 <!-- tl:e:{eid} -->\n"
        f"<!-- tl-rendered:e={eid};t={t0} -->"
    )
    new_block = (
        "## Timeline\n"
        f"14:00 【a】第一次改 <!-- tl:e:{eid} -->\n"
        f"<!-- tl-rendered:e={eid};t={_utc(0)} -->"
    )
    carried = timeline.carry_trail_t(new_block, old_block, absorbed=True)
    assert f"t={t0}" not in carried, "absorbed render must NOT carry the stale t="

    # --- edit #2 on the same line, with the fresh trail t= ---
    fresh_t = _utc(0)
    path.write_text(
        "## Timeline\n"
        f"14:00 【a】第二次改 <!-- tl:e:{eid} -->\n"
        f"<!-- tl-rendered:e={eid};t={fresh_t} -->\n"
    )
    rpt2 = reconcile_timeline(conn, path)
    assert rpt2.updated == 1, "second edit of the same line must also absorb"
    content = conn.execute("SELECT content FROM events WHERE id=?", (eid,)).fetchone()["content"]
    assert content == "【a】第二次改"
    assert row_after_1 is not None
