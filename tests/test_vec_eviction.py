"""Tests for aging.evict_vec_window — vec rolling window eviction.
Covers: window math, exemptions, safety caps, backup gate, dry-run."""
from __future__ import annotations

import struct
from datetime import date, timedelta

import pytest

from marrow import aging, storage


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    yield conn, p
    conn.close()


@pytest.fixture()
def fresh_backup(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    (d / f"marrow-{date.today().isoformat()}.db").touch()
    return str(d)


def _ins_event(conn, content, *, age_days, sid="s1"):
    ts = (date.today() - timedelta(days=age_days)).isoformat() + "T10:00:00Z"
    cur = conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content)"
        " VALUES (?, ?, 'user', ?)",
        (sid, ts, content),
    )
    conn.commit()  # release write lock — evict opens a 2nd conn for alerts
    return cur.lastrowid


def _ins_vec(conn, event_id):
    blob = struct.pack("1024f", *([0.1] * 1024))
    conn.execute(
        "INSERT INTO events_vec (rowid, embedding) VALUES (?, ?)",
        (event_id, blob),
    )
    conn.execute(
        "INSERT INTO events_vec_meta (rowid, embedder_id, dim)"
        " VALUES (?, 'bge-m3', 1024)",
        (event_id,),
    )
    conn.commit()


def _vec_ids(conn):
    return {r[0] for r in conn.execute("SELECT rowid FROM events_vec_meta")}


def test_out_window_evicted_in_window_kept(db, fresh_backup):
    conn, p = db
    old = _ins_event(conn, "old turn", age_days=100)
    new = _ins_event(conn, "new turn", age_days=10)
    _ins_vec(conn, old)
    _ins_vec(conn, new)
    res = aging.evict_vec_window(
        conn, window_days=90, backup_dir=fresh_backup, alert_db=p
    )
    assert res["evicted"] == 1 and not res["skipped"] and not res["aborted"]
    assert _vec_ids(conn) == {new}
    # events rows untouched — FTS lane intact
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_exempt_affect_importance_link(db, fresh_backup):
    conn, p = db
    old = _ins_event(conn, "milestone-ish turn", age_days=100)
    _ins_vec(conn, old)
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label,"
        " source, event_id) VALUES ('2026-01-01', 1, 0.9, 0.5, 3, 'x',"
        " 'test', ?)",
        (old,),
    )
    res = aging.evict_vec_window(
        conn, window_days=90, backup_dir=fresh_backup, alert_db=p
    )
    assert res["evicted"] == 0 and res["exempted"] == 1
    assert _vec_ids(conn) == {old}


def test_exempt_recall_count(db, fresh_backup):
    conn, p = db
    old = _ins_event(conn, "frequently recalled", age_days=100)
    _ins_vec(conn, old)
    conn.execute("UPDATE events SET recall_count=2 WHERE id=?", (old,))
    res = aging.evict_vec_window(
        conn, window_days=90, backup_dir=fresh_backup, alert_db=p
    )
    assert res["evicted"] == 0 and res["exempted"] == 1
    assert _vec_ids(conn) == {old}


def test_low_importance_link_not_exempt(db, fresh_backup):
    conn, p = db
    old = _ins_event(conn, "minor moment", age_days=100)
    _ins_vec(conn, old)
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label,"
        " source, event_id) VALUES ('2026-01-01', 1, 0.2, 0.2, 2, 'x',"
        " 'test', ?)",
        (old,),
    )
    res = aging.evict_vec_window(
        conn, window_days=90, backup_dir=fresh_backup, alert_db=p
    )
    assert res["evicted"] == 1
    assert _vec_ids(conn) == set()


def test_cap_pct_aborts_with_critical_alert(db, fresh_backup, monkeypatch):
    conn, p = db
    monkeypatch.setattr(aging, "_VEC_EVICT_CAP_MIN", 0)
    for i in range(4):
        eid = _ins_event(conn, f"old {i}", age_days=100)
        _ins_vec(conn, eid)
    res = aging.evict_vec_window(
        conn, window_days=90, backup_dir=fresh_backup, alert_db=p
    )
    assert res["aborted"] and res["evicted"] == 0
    assert len(_vec_ids(conn)) == 4
    sev = conn.execute(
        "SELECT severity FROM alerts WHERE fingerprint='vec_evict_cap_pct'"
    ).fetchone()
    assert sev and sev[0] == "critical"


def test_cap_abs_aborts(db, fresh_backup, monkeypatch):
    conn, p = db
    monkeypatch.setattr(aging, "_VEC_EVICT_CAP_ABS", 3)
    # 16 vec rows total, 4 old → 25% == pct cap boundary (not >), abs 4 > 3.
    for i in range(4):
        eid = _ins_event(conn, f"old {i}", age_days=100)
        _ins_vec(conn, eid)
    for i in range(12):
        eid = _ins_event(conn, f"new {i}", age_days=5)
        _ins_vec(conn, eid)
    res = aging.evict_vec_window(
        conn, window_days=90, backup_dir=fresh_backup, alert_db=p
    )
    assert res["aborted"] and len(_vec_ids(conn)) == 16
    assert conn.execute(
        "SELECT 1 FROM alerts WHERE fingerprint='vec_evict_cap_abs'"
    ).fetchone()


def test_stale_backup_skips_with_warn(db, tmp_path):
    conn, p = db
    old = _ins_event(conn, "old turn", age_days=100)
    _ins_vec(conn, old)
    d = tmp_path / "stale-backups"
    d.mkdir()
    stale = (date.today() - timedelta(days=8)).isoformat()
    (d / f"marrow-{stale}.db").touch()
    res = aging.evict_vec_window(
        conn, window_days=90, backup_dir=str(d), alert_db=p
    )
    assert res["skipped"] and res["evicted"] == 0
    assert _vec_ids(conn) == {old}
    sev = conn.execute(
        "SELECT severity FROM alerts WHERE fingerprint='vec_evict_backup_stale'"
    ).fetchone()
    assert sev and sev[0] == "warn"


def test_missing_backup_skips(db, tmp_path):
    conn, p = db
    old = _ins_event(conn, "old turn", age_days=100)
    _ins_vec(conn, old)
    empty = tmp_path / "no-backups"
    empty.mkdir()
    res = aging.evict_vec_window(
        conn, window_days=90, backup_dir=str(empty), alert_db=p
    )
    assert res["skipped"]
    assert _vec_ids(conn) == {old}


def test_dry_run_writes_nothing(db, fresh_backup):
    conn, p = db
    old = _ins_event(conn, "old turn", age_days=100)
    _ins_vec(conn, old)
    res = aging.evict_vec_window(
        conn, window_days=90, backup_dir=fresh_backup, dry_run=True, alert_db=p
    )
    assert res["evicted"] == 1  # would-evict count
    assert _vec_ids(conn) == {old}  # nothing deleted


def test_window_zero_disables(db, fresh_backup):
    conn, p = db
    old = _ins_event(conn, "ancient turn", age_days=400)
    _ins_vec(conn, old)
    res = aging.evict_vec_window(
        conn, window_days=0, backup_dir=fresh_backup, alert_db=p
    )
    assert res == {"evicted": 0, "exempted": 0, "skipped": False,
                   "aborted": False}
    assert _vec_ids(conn) == {old}
