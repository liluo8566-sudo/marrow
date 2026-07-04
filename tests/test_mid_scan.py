from __future__ import annotations

import datetime as _dt
import fcntl
import json

from marrow import config, mid_scan, storage


FROZEN_NOW = _dt.datetime(2026, 6, 1, 12, 0, tzinfo=_dt.timezone.utc)


def _freeze_now(monkeypatch) -> None:
    class FrozenDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return FROZEN_NOW.replace(tzinfo=None)
            return FROZEN_NOW.astimezone(tz)

    monkeypatch.setattr(mid_scan._dt, "datetime", FrozenDateTime)


def _patch_config(monkeypatch, db: str) -> None:
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(mid_scan.config, "db_path", lambda: db)
    monkeypatch.setattr(
        mid_scan.config,
        "load",
        lambda: {
            "sessionend_mid": {
                "elapsed_hours": 4,
                "turn_threshold_time": 10,
                "turn_threshold_abs": 30,
                "min_hours": 2,
                "min_turns": 4,
                "elapsed_hours_slow": 6,
                "turn_threshold_slow": 4,
            }
        },
    )


def _insert_user_events(
    conn, sid: str, *, start_id: int, count: int, hours_ago: float
) -> None:
    ts = (FROZEN_NOW - _dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        for event_id in range(start_id, start_id + count):
            conn.execute(
                "INSERT INTO events (id, session_id, timestamp, role, content)"
                " VALUES (?, ?, ?, 'user', ?)",
                (event_id, sid, ts, f"turn {event_id}"),
            )


def _write_jsonl(path, sid: str, *, count: int, hours_ago: float = 5) -> None:
    ts = (FROZEN_NOW - _dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        json.dumps({
            "type": "user",
            "sessionId": sid,
            "timestamp": ts,
            "message": {"role": "user", "content": f"jsonl turn {i}"},
        })
        for i in range(count)
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _run_scan(monkeypatch, db: str, sid: str, jsonl_path: str):
    _patch_config(monkeypatch, db)
    _freeze_now(monkeypatch)
    calls = []
    monkeypatch.setattr(
        mid_scan,
        "_spawn_sessionend_async",
        lambda *a, **kw: calls.append((a, kw)),
    )
    rc = mid_scan.main([
        "--sid", sid,
        "--jsonl-path", jsonl_path,
        "--channel", "wx",
    ])
    return rc, calls


def test_mid_scan_below_floor_no_trigger(tmp_path, monkeypatch):
    db = str(tmp_path / "mid.db")
    conn = storage.init_db(db)
    try:
        _insert_user_events(conn, "sid-floor", start_id=1, count=10, hours_ago=1)
    finally:
        conn.close()
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("", encoding="utf-8")

    rc, calls = _run_scan(monkeypatch, db, "sid-floor", str(jsonl))

    assert rc == 0
    assert calls == []


def test_mid_scan_triggers_on_time_and_turn_threshold(tmp_path, monkeypatch):
    db = str(tmp_path / "mid.db")
    conn = storage.init_db(db)
    try:
        _insert_user_events(conn, "sid-time", start_id=1, count=10, hours_ago=5)
    finally:
        conn.close()
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("", encoding="utf-8")

    rc, calls = _run_scan(monkeypatch, db, "sid-time", str(jsonl))

    assert rc == 0
    assert calls == [(("sid-time",), {"after_event_id": None, "segment_seq": 1})]


def test_mid_scan_triggers_on_absolute_turn_threshold(tmp_path, monkeypatch):
    db = str(tmp_path / "mid.db")
    conn = storage.init_db(db)
    try:
        _insert_user_events(conn, "sid-abs", start_id=1, count=30, hours_ago=2.5)
    finally:
        conn.close()
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("", encoding="utf-8")

    rc, calls = _run_scan(monkeypatch, db, "sid-abs", str(jsonl))

    assert rc == 0
    assert calls == [(("sid-abs",), {"after_event_id": None, "segment_seq": 1})]


def test_mid_scan_second_scan_uses_previous_watermark(tmp_path, monkeypatch):
    db = str(tmp_path / "mid.db")
    sid = "sid-watermark"
    conn = storage.init_db(db)
    try:
        _insert_user_events(conn, sid, start_id=1, count=10, hours_ago=7)
        _insert_user_events(conn, sid, start_id=11, count=10, hours_ago=1)
        created_at = (FROZEN_NOW - _dt.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with conn:
            conn.execute(
                "INSERT INTO session_watermarks"
                " (sid, segment_seq, last_event_id, last_turn_idx, created_at)"
                " VALUES (?, 1, 10, 10, ?)",
                (sid, created_at),
            )
    finally:
        conn.close()
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("", encoding="utf-8")

    rc, calls = _run_scan(monkeypatch, db, sid, str(jsonl))

    assert rc == 0
    assert calls == [((sid,), {"after_event_id": 10, "segment_seq": 2})]


def test_mid_scan_prearchives_before_trigger_eval(tmp_path, monkeypatch):
    db = str(tmp_path / "mid.db")
    sid = "sid-jsonl"
    conn = storage.init_db(db)
    conn.close()
    jsonl = tmp_path / "active.jsonl"
    _write_jsonl(jsonl, sid, count=10, hours_ago=5)

    rc, calls = _run_scan(monkeypatch, db, sid, str(jsonl))

    assert rc == 0
    assert calls == [((sid,), {"after_event_id": None, "segment_seq": 1})]
    conn = storage.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE session_id=?", (sid,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 10


def test_mid_scan_lock_dir_fallback_still_prearchives(tmp_path, monkeypatch):
    db = str(tmp_path / "mid.db")
    sid = "sid-lock-fallback"
    conn = storage.init_db(db)
    conn.close()
    jsonl = tmp_path / "active.jsonl"
    _write_jsonl(jsonl, sid, count=10, hours_ago=5)
    blocked_data_dir = tmp_path / "not-a-dir"
    blocked_data_dir.write_text("", encoding="utf-8")

    _patch_config(monkeypatch, db)
    _freeze_now(monkeypatch)
    monkeypatch.setattr(config, "DATA_DIR", blocked_data_dir)
    monkeypatch.setattr(mid_scan.config, "DATA_DIR", blocked_data_dir)
    monkeypatch.setattr(mid_scan.tempfile, "gettempdir", lambda: str(tmp_path))
    calls = []
    monkeypatch.setattr(
        mid_scan,
        "_spawn_sessionend_async",
        lambda *a, **kw: calls.append((a, kw)),
    )

    rc = mid_scan.main([
        "--sid", sid,
        "--jsonl-path", str(jsonl),
        "--channel", "wx",
    ])

    assert rc == 0
    assert calls == [((sid,), {"after_event_id": None, "segment_seq": 1})]
    assert not (tmp_path / f"marrow_mid_{sid}.lock").exists()
    conn = storage.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE session_id=?", (sid,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 10


def test_pre_archive_failure_logged(tmp_path, monkeypatch):
    db = str(tmp_path / "mid.db")
    conn = storage.init_db(db)
    conn.close()
    jsonl = tmp_path / "active.jsonl"
    jsonl.write_text("", encoding="utf-8")

    _patch_config(monkeypatch, db)
    _freeze_now(monkeypatch)
    monkeypatch.setattr(
        mid_scan, "_spawn_sessionend_async", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        mid_scan,
        "transcript_clean",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    mid_scan.main([
        "--sid", "sid-fail",
        "--jsonl-path", str(jsonl),
        "--channel", "wx",
    ])

    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT action, summary FROM audit_log"
            " WHERE target_id=? AND action='mid_scan_pre_archive_fail'",
            ("sid-fail",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert "RuntimeError" in row["summary"]
    assert "boom" in row["summary"]


def test_concurrent_lock_skips(tmp_path, monkeypatch):
    db = str(tmp_path / "mid.db")
    conn = storage.init_db(db)
    conn.close()
    jsonl = tmp_path / "active.jsonl"
    jsonl.write_text("", encoding="utf-8")

    _patch_config(monkeypatch, db)
    _freeze_now(monkeypatch)

    lock_dir = config.DATA_DIR / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "mid_sid-lock.lock"

    with open(lock_path, "w") as held_fd:
        fcntl.flock(held_fd, fcntl.LOCK_EX)
        calls = []
        monkeypatch.setattr(
            mid_scan, "_spawn_sessionend_async", lambda *a, **kw: calls.append(1)
        )
        rc = mid_scan.main([
            "--sid", "sid-lock",
            "--jsonl-path", str(jsonl),
            "--channel", "wx",
        ])
        fcntl.flock(held_fd, fcntl.LOCK_UN)

    assert rc == 0
    assert calls == []


def test_mid_scan_triggers_on_path_c_slow(tmp_path, monkeypatch):
    """Path C: 6h elapsed + 4 user turns → trigger."""
    db = str(tmp_path / "mid.db")
    conn = storage.init_db(db)
    try:
        _insert_user_events(conn, "sid-slow", start_id=1, count=4, hours_ago=7)
    finally:
        conn.close()
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("", encoding="utf-8")

    rc, calls = _run_scan(monkeypatch, db, "sid-slow", str(jsonl))

    assert rc == 0
    assert calls == [(("sid-slow",), {"after_event_id": None, "segment_seq": 1})]


def test_mid_scan_no_trigger_path_c_below_elapsed(tmp_path, monkeypatch):
    """Path C: only 5h elapsed with 4 turns → no trigger (needs 6h)."""
    db = str(tmp_path / "mid.db")
    conn = storage.init_db(db)
    try:
        _insert_user_events(conn, "sid-slow-no", start_id=1, count=4, hours_ago=5)
    finally:
        conn.close()
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("", encoding="utf-8")

    rc, calls = _run_scan(monkeypatch, db, "sid-slow-no", str(jsonl))

    assert rc == 0
    assert calls == []
