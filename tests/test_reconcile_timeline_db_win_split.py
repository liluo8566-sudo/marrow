"""db_win split — render residue vs clobbered human edit.

When the freshness gate keeps DB text over a stale md line, we branch on a
CONTENT fingerprint (trail z=) recomputed over the current timeline zone, not
file mtime:
  - residue  (recomputed z= == stored z=): nobody touched the zone since the
    render → silent self-heal → audit_log row only, no alert.
  - clobber  (z= differs, or z= absent/pre-migration): a human may have edited
    the zone → warn alert.

The mtime classifier was structurally wrong on multi-zone pages (daybrief.md):
a Status-zone rewrite bumps file mtime every render while the timeline trail t=
is carried over unchanged, so `mtime > t= + slack` was the NORMAL state and
every render residue got misread as a hand edit (false alert #1133).
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from marrow import storage
from marrow.reconcile import reconcile_timeline
from marrow.timeline import _zone_fingerprint


@pytest.fixture()
def dbpath(tmp_path):
    return str(tmp_path / "dw.db")


@pytest.fixture()
def conn(dbpath):
    c = storage.init_db(dbpath)
    yield c
    c.close()


def _utc(delta_s: float = 0.0) -> str:
    dt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=delta_s)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_self(conn, content: str, created_at: str, updated_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel,"
        " created_at, updated_at)"
        " VALUES ('s1', ?, 'tl', ?, 'cli', ?, ?)",
        (created_at, content, created_at, updated_at),
    )
    conn.commit()
    return cur.lastrowid


def _timeline_zone(eid: int, line: str, t0: str, *, z: str | None = None,
                   with_end: bool = True) -> str:
    """Build a `## Timeline` zone body + trail whose z= matches its own body."""
    body = f"## Timeline\n{line} <!-- tl:e:{eid} -->"
    fp = z if z is not None else _zone_fingerprint(body)
    zseg = "" if fp == "" else f"z={fp};"
    trail = f"<!-- tl-rendered:e={eid};{zseg}t={t0} -->"
    end = f"\n{'<!-- marrow:timeline:end -->'}" if with_end else ""
    return f"{body}\n{trail}{end}\n"


def _daybrief_file(path: Path, zone: str, status: str = "老状态") -> None:
    """A multi-zone daybrief-like page: Status zone above the timeline zone."""
    path.write_text(
        "<!-- marrow:status:start -->\n"
        f"### Status\n{status}\n"
        "<!-- marrow:status:end -->\n\n"
        "<!-- marrow:timeline:start -->\n"
        f"{zone}"
        "<!-- marrow:timeline:end -->\n"
    )


def _audit_rows(conn):
    return conn.execute(
        "SELECT * FROM audit_log WHERE action='md_stale_db_win'"
    ).fetchall()


def _alert_rows(dbpath):
    c = storage.connect(dbpath)
    try:
        return c.execute(
            "SELECT * FROM alerts WHERE fingerprint='timeline_reconcile:db_win'"
        ).fetchall()
    finally:
        c.close()


# ── #1133 scenario: multi-zone residue → silent ──────────────────────────────

def test_multizone_status_rewrite_residue_no_alert(conn, dbpath, tmp_path):
    """The #1133 false-positive: a Status-zone rewrite bumps file mtime WITHOUT
    touching the timeline zone or its trail. z= still matches → render residue →
    DB wins silently: one audit row, no alert, md keeps DB text."""
    t0 = _utc(-120)          # render moment
    row_ts = _utc(-60)       # DB row edited after render → gate keeps DB
    eid = _insert_self(conn, "【a】新值", created_at=t0, updated_at=row_ts)

    zone = _timeline_zone(eid, "14:00 【a】旧值", t0)
    path = tmp_path / "daybrief.md"
    _daybrief_file(path, zone, status="老状态")

    # Simulate a Status-zone rewrite: rebuild the file with new status text,
    # bumping mtime, but leaving the timeline zone + trail byte-identical.
    _daybrief_file(path, zone, status="新状态 - refreshed")

    rpt = reconcile_timeline(conn, path, db=dbpath)

    assert rpt.updated == 0
    assert conn.execute(
        "SELECT content FROM events WHERE id=?", (eid,)
    ).fetchone()["content"] == "【a】新值"
    assert len(_audit_rows(conn)) == 1
    assert len(_alert_rows(dbpath)) == 0


# ── human edit + concurrent DB update → warn ─────────────────────────────────

def test_human_edit_clobbered_warns(conn, dbpath, tmp_path):
    """A real hand edit changes the timeline zone text → recomputed z= differs
    from the stored z= → warn alert fires, no residue audit row."""
    t0 = _utc(-120)
    row_ts = _utc(-60)
    eid = _insert_self(conn, "【a】新值", created_at=t0, updated_at=row_ts)

    # Trail z= was stamped for "旧值"; the file on disk shows a hand-edited body.
    good_zone = _timeline_zone(eid, "14:00 【a】旧值", t0)
    stored_z = None
    import re
    m = re.search(r"z=([0-9a-f]{8})", good_zone)
    stored_z = m.group(1)
    edited_zone = _timeline_zone(eid, "14:00 【a】人类手改", t0, z=stored_z)

    path = tmp_path / "daybrief.md"
    _daybrief_file(path, edited_zone)

    rpt = reconcile_timeline(conn, path, db=dbpath)

    assert rpt.updated == 0
    assert conn.execute(
        "SELECT content FROM events WHERE id=?", (eid,)
    ).fetchone()["content"] == "【a】新值"
    alerts = _alert_rows(dbpath)
    assert len(alerts) == 1
    assert f"e:{eid}" in alerts[0]["message"]
    assert len(_audit_rows(conn)) == 0


# ── z= absent (pre-migration trail) → warn ───────────────────────────────────

def test_z_absent_pre_migration_warns(conn, dbpath, tmp_path):
    """A trail with no z= (pre-migration render) can't prove residue → warn.
    One render after deploy backfills z= and future runs go silent."""
    t0 = _utc(-120)
    row_ts = _utc(-60)
    eid = _insert_self(conn, "【a】新值", created_at=t0, updated_at=row_ts)

    zone = _timeline_zone(eid, "14:00 【a】旧值", t0, z="")   # no z= segment
    path = tmp_path / "daybrief.md"
    _daybrief_file(path, zone)

    rpt = reconcile_timeline(conn, path, db=dbpath)

    assert len(_alert_rows(dbpath)) == 1
    assert len(_audit_rows(conn)) == 0


# ── round-trip: real render output → reconcile recompute matches ─────────────

def test_roundtrip_real_render_fingerprint(conn, tmp_path):
    """Use the REAL render_timeline output written to a file; reconcile
    recomputes the fingerprint over that file zone → must equal the stored z=."""
    from marrow import timeline

    row_ts = _utc(-60)
    conn.execute(
        "INSERT INTO session_digests"
        " (sid, segment_seq, date, ts, text, kind, life_lines, updated_at)"
        " VALUES ('rt', 0, ?, ?, 'body', 'casual', ?, ?)",
        (row_ts[:10], row_ts, "真实渲染行", row_ts),
    )
    conn.commit()

    rendered = timeline.render_timeline(conn)
    assert "tl-rendered:" in rendered
    import re
    stored_z = re.search(r"z=([0-9a-f]{8})", rendered).group(1)

    # Extract the timeline zone the same way reconcile does and recompute.
    start = rendered.find("## Timeline")
    after = rendered[start + len("## Timeline"):]
    n_h2 = re.search(r"\n##\s", after)
    block = after[: n_h2.start()] if n_h2 else after
    recomputed = timeline._zone_fingerprint("## Timeline" + block)
    assert recomputed == stored_z


# ── tool sync: _sync_dashboard_line refreshes z= → later reconcile silent ────

def test_tool_sync_updates_z_not_human_edited(tmp_path, monkeypatch):
    """After _sync_dashboard_line rewrites a line, the trail z= matches a fresh
    recompute over the post-edit zone → a subsequent reconcile does not flag it
    as human-edited."""
    from marrow import tl_writer, timeline

    eid = 42
    t0 = _utc(-120)
    zone = _timeline_zone(eid, "14:00 老内容", t0)
    dash = tmp_path / "dashboard.md"
    dash.write_text("## Timeline\n" + zone.split("## Timeline\n", 1)[1])

    monkeypatch.setattr(tl_writer, "_dashboard_path", lambda: dash)

    ok = tl_writer._sync_dashboard_line(eid, "14:00", None, "新内容")
    assert ok

    text = dash.read_text(encoding="utf-8")
    import re
    stored_z = re.search(r"z=([0-9a-f]{8})", text).group(1)

    start = text.find("## Timeline")
    after = text[start + len("## Timeline"):]
    n_end = after.find("<!-- marrow:timeline:end -->")
    block = after[:n_end] if n_end != -1 else after
    recomputed = timeline._zone_fingerprint("## Timeline" + block)
    assert recomputed == stored_z
