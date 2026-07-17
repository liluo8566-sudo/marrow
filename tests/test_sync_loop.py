"""sync_loop — unit tests.

All tests use tmp_path + in-memory or tmp-path dbs; no real db touched.
Monkey-patched targets (fake db_mtime, reconcile, render callables) so we
can drive the loop deterministically without real subpage machinery.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from marrow.sync_loop import (
    SyncLoop,
    SyncTarget,
    USER_ACTIVE_WINDOW_S,
    _MTIME_EPSILON_S,
    last_db_mtime_daybrief,
    last_db_mtime_subpage,
)


def _backdate(p: Path, seconds: float = 10.0) -> float:
    """Push md mtime back so it falls outside the user-active window."""
    past = time.time() - seconds
    os.utime(str(p), (past, past))
    return p.stat().st_mtime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def _target(
    md_path: str,
    db_mtime_fn: Callable,
    render_fn=None,
    has_md_to_db: bool = True,
    name: str = "test",
) -> SyncTarget:
    return SyncTarget(
        name=name,
        md_path=md_path,
        db_mtime_fn=db_mtime_fn,
        render_fn=render_fn or (lambda c: None),
        has_md_to_db=has_md_to_db,
    )


# ---------------------------------------------------------------------------
# last_db_mtime_subpage
# ---------------------------------------------------------------------------

def test_last_db_mtime_subpage_unknown_key():
    c = _conn()
    assert last_db_mtime_subpage(c, "nonexistent_key") is None


def test_last_db_mtime_subpage_empty_sources():
    """wallet and cheatsheet have no sources → None."""
    c = _conn()
    assert last_db_mtime_subpage(c, "wallet") is None
    assert last_db_mtime_subpage(c, "cheatsheet") is None


def test_last_db_mtime_subpage_table_absent():
    """If the table doesn't exist, return None (not an error)."""
    c = _conn()
    result = last_db_mtime_subpage(c, "milestone")
    assert result is None  # milestones table absent in :memory: without init


def test_last_db_mtime_subpage_with_data(tmp_path):
    from marrow import storage
    db = str(tmp_path / "t.db")
    c = storage.init_db(db)
    c.execute(
        "INSERT INTO milestones (scope, date, title, updated_at)"
        " VALUES ('Us','2026-05-27','t1','2026-05-27T10:00:00Z')"
    )
    c.commit()
    ts = last_db_mtime_subpage(c, "milestone")
    assert ts is not None
    assert ts > 0.0
    c.close()


    c.close()


# ---------------------------------------------------------------------------
# SyncLoop — tick fires / shutdown
# ---------------------------------------------------------------------------

def test_sync_loop_tick_fires(tmp_path):
    """Loop calls _process on each iteration (db newer → render path)."""
    md = tmp_path / "t.md"
    md.write_text("# test")
    md_mtime = _backdate(md)

    calls: list[str] = []
    db_mtime_base = md_mtime + 10.0  # db is newer → render path

    def db_fn(c):
        return db_mtime_base

    def render_fn(c):
        calls.append("render")

    t = _target(str(md), db_fn, render_fn=render_fn, name="tick-test")
    loop = SyncLoop(_conn, [t], tick_s=0.05)
    loop.start()
    time.sleep(0.3)
    loop.stop()
    assert len(calls) >= 2  # boot tick + at least one timed tick


def test_sync_loop_shutdown_stops_cleanly(tmp_path):
    """stop() terminates the thread without hanging."""
    md = tmp_path / "x.md"
    md.write_text("x")
    t = _target(str(md), lambda c: None, name="shutdown-test")
    loop = SyncLoop(_conn, [t], tick_s=0.1)
    loop.start()
    time.sleep(0.05)
    loop.stop(timeout=2.0)
    assert loop._thread is not None
    assert not loop._thread.is_alive()


# ---------------------------------------------------------------------------
# md newer → reconcile called, render NOT called independently
# ---------------------------------------------------------------------------

def test_md_newer_calls_render(tmp_path):
    """md newer → render_fn called (write_subpage owns reconcile internally)."""
    md = tmp_path / "subpage.md"
    md.write_text("# hello")
    md_mtime = _backdate(md)
    db_mtime = md_mtime - 10.0  # md is newer

    rendered: list[int] = []

    def render(c):
        rendered.append(1)

    t = _target(str(md), lambda c: db_mtime, render_fn=render)
    loop = SyncLoop(_conn, [t], tick_s=100.0)  # only boot tick
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert rendered, "render_fn should be called when md newer (single reconcile via write_subpage)"


def test_md_newer_no_md_to_db_skips(tmp_path):
    """Target with has_md_to_db=False: md→db direction is skipped entirely."""
    md = tmp_path / "t.md"
    md.write_text("x")
    md_mtime = _backdate(md)
    db_mtime = md_mtime - 10.0

    rendered: list[int] = []

    t = _target(str(md), lambda c: db_mtime,
                has_md_to_db=False, render_fn=lambda c: rendered.append(1))
    loop = SyncLoop(_conn, [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    # has_md_to_db=False → process returns early; render not called
    assert rendered == []


# ---------------------------------------------------------------------------
# db newer → render called
# ---------------------------------------------------------------------------

def test_db_newer_calls_render(tmp_path):
    md = tmp_path / "t.md"
    md.write_text("x")
    md_mtime = _backdate(md)
    db_mtime = md_mtime + 10.0  # db is newer

    rendered: list[int] = []

    t = _target(str(md), lambda c: db_mtime, render_fn=lambda c: rendered.append(1))
    loop = SyncLoop(_conn, [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert len(rendered) >= 1


# ---------------------------------------------------------------------------
# Equal mtimes (within epsilon) → noop
# ---------------------------------------------------------------------------

def test_equal_mtimes_noop(tmp_path):
    """md ≥ db within md→db epsilon → neither branch fires."""
    md = tmp_path / "t.md"
    md.write_text("x")
    md_mtime = md.stat().st_mtime

    rendered: list[int] = []

    # db_mtime equal-to-or-just-behind md_mtime. md→db epsilon still applies
    # (avoids spurious reconciles on fs jitter), and db is NOT newer than md
    # so the render branch also does not fire.
    db_mtime = md_mtime - 0.1

    t = _target(str(md), lambda c: db_mtime,
                render_fn=lambda c: rendered.append(1))
    loop = SyncLoop(_conn, [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert rendered == []


def test_db_slightly_newer_renders(tmp_path):
    """Regression: db just-a-bit-newer-than-md must trigger render in next tick.

    Pre-fix the 1s db→md epsilon would swallow this and md never reflected
    the db change (atlas depth-shrink / dashboard refresh freeze symptom).
    """
    md = tmp_path / "t.md"
    md.write_text("x")
    md_mtime = _backdate(md)

    rendered: list[int] = []
    # 0.5s ahead — used to be inside epsilon, now must render.
    db_mtime = md_mtime + 0.5

    t = _target(str(md), lambda c: db_mtime,
                render_fn=lambda c: rendered.append(1))
    loop = SyncLoop(_conn, [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert len(rendered) >= 1


# ---------------------------------------------------------------------------
# Race防御: mid-reconcile md write → render skipped
# ---------------------------------------------------------------------------

def test_race_defense_mid_render_md_write(tmp_path):
    """If md mtime advances during render, a debug log fires (next tick absorbs it)."""
    md = tmp_path / "race.md"
    md.write_text("initial")
    md_mtime_initial = _backdate(md)
    db_mtime = md_mtime_initial - 10.0  # md is newer → render path

    rendered: list[int] = []

    def render_with_side_write(c):
        # Simulate external md edit arriving during render
        time.sleep(0.02)
        md.write_text("mid-render edit")
        now = time.time() + 1.0
        os.utime(str(md), (now, now))
        rendered.append(1)

    t = _target(str(md), lambda c: db_mtime, render_fn=render_with_side_write)
    loop = SyncLoop(_conn, [t], tick_s=100.0)
    loop.start()
    time.sleep(0.2)
    loop.stop()
    # render_fn was still called; the race defense only logs, does not skip
    assert rendered, "render_fn must be called; race defense logs and defers to next tick"


# ---------------------------------------------------------------------------
# Multiple targets processed independently
# ---------------------------------------------------------------------------

def test_multiple_targets_independent(tmp_path):
    md1 = tmp_path / "a.md"
    md2 = tmp_path / "b.md"
    md1.write_text("a")
    md2.write_text("b")
    _backdate(md1)
    _backdate(md2)
    now = time.time()

    calls: dict[str, list] = {"a_render": [], "b_render": []}

    # md1: db newer → render
    t1 = _target(str(md1), lambda c: now + 10.0,
                 render_fn=lambda c: calls["a_render"].append(1), name="a")
    # md2: db newer → render
    t2 = _target(str(md2), lambda c: now + 10.0,
                 render_fn=lambda c: calls["b_render"].append(1), name="b")

    loop = SyncLoop(_conn, [t1, t2], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert len(calls["a_render"]) >= 1
    assert len(calls["b_render"]) >= 1


# ---------------------------------------------------------------------------
# Missing md file → skip
# ---------------------------------------------------------------------------

def test_missing_md_skipped(tmp_path):
    rendered: list[int] = []
    t = _target(
        str(tmp_path / "nonexistent.md"),
        lambda c: time.time() + 10.0,
        render_fn=lambda c: rendered.append(1),
    )
    loop = SyncLoop(_conn, [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert rendered == []


# ---------------------------------------------------------------------------
# db_mtime None → skip
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# User-active guard: skip render when md touched within USER_ACTIVE_WINDOW_S
# Protects atlas.md (and any subpage) from inserter bootstrap rewriting
# the file while the user is typing in Obsidian.
# ---------------------------------------------------------------------------

def test_user_active_md_to_db_skipped(tmp_path):
    """md_mtime ~ now → md→db render SKIPPED (user actively typing)."""
    md = tmp_path / "atlas.md"
    md.write_text("# atlas")
    md_mtime = md.stat().st_mtime  # fresh — within active window
    db_mtime = md_mtime - 10.0  # md is newer → would normally render

    rendered: list[int] = []
    t = _target(str(md), lambda c: db_mtime,
                render_fn=lambda c: rendered.append(1))
    loop = SyncLoop(_conn, [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert rendered == [], (
        "render_fn must NOT be called when md touched within "
        f"USER_ACTIVE_WINDOW_S ({USER_ACTIVE_WINDOW_S}s)"
    )


def test_user_active_db_to_md_skipped(tmp_path):
    """md_mtime ~ now → db→md render SKIPPED (user actively typing)."""
    md = tmp_path / "atlas.md"
    md.write_text("# atlas")
    md_mtime = md.stat().st_mtime  # fresh — within active window
    db_mtime = md_mtime + 10.0  # db is newer → would normally render

    rendered: list[int] = []
    t = _target(str(md), lambda c: db_mtime,
                render_fn=lambda c: rendered.append(1))
    loop = SyncLoop(_conn, [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert rendered == [], (
        "render_fn must NOT be called when md touched within "
        f"USER_ACTIVE_WINDOW_S ({USER_ACTIVE_WINDOW_S}s)"
    )


def test_user_idle_md_to_db_renders(tmp_path):
    """md_mtime = now-5s → md→db render PROCEEDS (user idle, outside window)."""
    md = tmp_path / "atlas.md"
    md.write_text("# atlas")
    md_mtime = _backdate(md, seconds=5.0)
    db_mtime = md_mtime - 10.0  # md newer

    rendered: list[int] = []
    t = _target(str(md), lambda c: db_mtime,
                render_fn=lambda c: rendered.append(1))
    loop = SyncLoop(_conn, [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert rendered, "render_fn must be called when md is older than user-active window"


def test_user_idle_db_to_md_renders(tmp_path):
    """md_mtime = now-5s → db→md render PROCEEDS (user idle, outside window)."""
    md = tmp_path / "atlas.md"
    md.write_text("# atlas")
    md_mtime = _backdate(md, seconds=5.0)
    db_mtime = md_mtime + 10.0  # db newer

    rendered: list[int] = []
    t = _target(str(md), lambda c: db_mtime,
                render_fn=lambda c: rendered.append(1))
    loop = SyncLoop(_conn, [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert rendered, "render_fn must be called when md is older than user-active window"


def test_db_mtime_none_skipped(tmp_path):
    md = tmp_path / "t.md"
    md.write_text("x")
    rendered: list[int] = []
    t = _target(str(md), lambda c: None, render_fn=lambda c: rendered.append(1))
    loop = SyncLoop(_conn, [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert rendered == []


# ---------------------------------------------------------------------------
# Daybrief target — timeline-only db subset + bidirectional wiring
# ---------------------------------------------------------------------------

def test_last_db_mtime_daybrief_timeline_subset(tmp_path):
    """Only timeline tables count; a usage/rate-limit kv change does not move it."""
    from datetime import datetime, timezone

    from marrow import storage
    db = str(tmp_path / "t.db")
    c = storage.init_db(db)
    assert last_db_mtime_daybrief(c) is None
    # A new tl line (events.created_at) — this IS a timeline source.
    c.execute(
        "INSERT INTO events (session_id, timestamp, role, content, created_at)"
        " VALUES ('s1','2026-05-27T09:00:00Z','tl','x [3]',"
        " '2026-05-27T09:00:00Z')"
    )
    c.commit()
    ts = last_db_mtime_daybrief(c)
    assert ts is not None
    expected = datetime(2026, 5, 27, 9, 0, 0, tzinfo=timezone.utc).timestamp()
    assert abs(ts - expected) < 1.0
    c.close()


def test_daybrief_raw_conversation_event_does_not_move_clock(tmp_path):
    """D3: raw conversation events (role user/assistant, not tl/manual) must NOT
    advance the daybrief clock, or the loop re-renders every chat turn."""
    from datetime import datetime, timezone

    from marrow import storage
    db = str(tmp_path / "t.db")
    c = storage.init_db(db)
    assert last_db_mtime_daybrief(c) is None
    # A rendered tl line establishes a baseline clock.
    c.execute(
        "INSERT INTO events (session_id, timestamp, role, content, created_at)"
        " VALUES ('s1','2026-05-27T09:00:00Z','tl','x [3]',"
        " '2026-05-27T09:00:00Z')"
    )
    c.commit()
    base = last_db_mtime_daybrief(c)
    assert base is not None
    # A later raw conversation turn (role='assistant') — NOT rendered by the
    # timeline. Its created_at is far newer but must be ignored.
    c.execute(
        "INSERT INTO events (session_id, timestamp, role, content, created_at)"
        " VALUES ('s1','2026-05-27T10:00:00Z','assistant','chatter',"
        " '2026-05-27T10:00:00Z')"
    )
    c.commit()
    after = last_db_mtime_daybrief(c)
    assert after == base, "raw conversation event must not move the daybrief clock"
    expected = datetime(2026, 5, 27, 9, 0, 0, tzinfo=timezone.utc).timestamp()
    assert abs(after - expected) < 1.0
    c.close()


def test_daybrief_tl_inplace_edit_moves_clock(tmp_path):
    """D2/D3: an in-place tl edit bumps events.updated_at → daybrief clock
    advances so the ≤5s loop reflects the edit to md."""
    from datetime import datetime, timezone

    from marrow import storage
    db = str(tmp_path / "t.db")
    c = storage.init_db(db)
    c.execute(
        "INSERT INTO events (session_id, timestamp, role, content, created_at)"
        " VALUES ('s1','2026-05-27T09:00:00Z','tl','x [3]',"
        " '2026-05-27T09:00:00Z')"
    )
    c.commit()
    base = last_db_mtime_daybrief(c)
    # In-place edit stamps updated_at newer than created_at.
    c.execute(
        "UPDATE events SET content='y [3]', updated_at='2026-05-27T11:00:00Z'"
        " WHERE role='tl'"
    )
    c.commit()
    after = last_db_mtime_daybrief(c)
    expected = datetime(2026, 5, 27, 11, 0, 0, tzinfo=timezone.utc).timestamp()
    assert abs(after - expected) < 1.0
    assert after > base
    c.close()


def test_daybrief_db_change_triggers_render(tmp_path):
    """Insert a timeline row → db-newer → render_fn invoked (db→md path)."""
    from marrow import storage
    db = str(tmp_path / "t.db")
    real_conn = storage.init_db(db)

    md = tmp_path / "daybrief.md"
    md.write_text("# daybrief")
    _backdate(md, seconds=10.0)  # md older + outside user-active window

    # A fresh tl line lands with created_at = now → db strictly newer than md.
    now_iso = "2999-01-01T00:00:00Z"
    real_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, created_at)"
        " VALUES ('s1','2026-05-27T09:00:00Z','tl','edit [3]', ?)",
        (now_iso,),
    )
    real_conn.commit()

    real_conn.close()  # loop opens its own thread conn via conn_factory

    rendered: list[int] = []
    t = _target(
        str(md),
        lambda c: last_db_mtime_daybrief(c),
        render_fn=lambda c: rendered.append(1),
        name="daybrief",
    )
    loop = SyncLoop(lambda: storage.connect(db), [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert rendered, "db-newer timeline row must trigger daybrief render_fn"


def test_daybrief_md_edit_absorbs_to_db(tmp_path):
    """md hand-edit (md-newer) → render_fn runs; daybrief.update reconciles it
    into the DB before rendering (P2 reconcile-before-render)."""
    from marrow import storage
    db = str(tmp_path / "t.db")
    storage.init_db(db).close()

    md = tmp_path / "daybrief.md"
    md.write_text("# daybrief hand-edited timeline line")
    md_mtime = _backdate(md, seconds=5.0)  # md newer, but user-idle

    # db older than md → md→db (reconcile-inside-render) path.
    reconciled: list[str] = []

    def render_fn(c):
        # daybrief.update does reconcile-before-render internally; assert the
        # loop dispatches a single render call for the md-newer branch.
        reconciled.append("reconcile+render")

    t = _target(
        str(md),
        lambda c: md_mtime - 10.0,
        render_fn=render_fn,
        has_md_to_db=True,
        name="daybrief",
    )
    loop = SyncLoop(lambda: storage.connect(db), [t], tick_s=100.0)
    loop.start()
    time.sleep(0.1)
    loop.stop()
    assert reconciled == ["reconcile+render"], (
        "md-newer edit must invoke render_fn exactly once (reconcile lives "
        "inside daybrief.update, not a second loop call)")
