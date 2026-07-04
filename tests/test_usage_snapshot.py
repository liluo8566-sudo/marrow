import json

import pytest

from marrow import usage_snapshot as us


def _kv(conn, key):
    row = conn.execute(
        "SELECT value FROM ct_rate_limit WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _patch_db(monkeypatch, tmp_path):
    from marrow import storage as stor

    db = str(tmp_path / "test.db")
    real_connect = stor.connect
    stor.init_db(db)
    monkeypatch.setattr(
        "marrow.usage_snapshot.storage.connect", lambda path=None: real_connect(db))
    return real_connect, db


def test_fetch_and_write_writes_pct_and_reset_at(monkeypatch, tmp_path):
    real_connect, db = _patch_db(monkeypatch, tmp_path)
    monkeypatch.setattr(us, "_load_token", lambda: "tok-123")
    body = json.dumps({
        "five_hour": {"utilization": 42, "resets_at": "2026-07-04T10:00:00+00:00"},
        "seven_day": {"utilization": 17.5, "resets_at": "2026-07-10T00:00:00+00:00"},
    }).encode()
    monkeypatch.setattr(us, "_http_get", lambda url, headers: (200, body))

    us.fetch_and_write()

    conn = real_connect(db)
    assert _kv(conn, "five_hour_pct") == "42.0"
    assert _kv(conn, "five_hour_reset_at") == "2026-07-04T10:00:00+00:00"
    assert _kv(conn, "seven_day_pct") == "17.5"
    assert _kv(conn, "seven_day_reset_at") == "2026-07-10T00:00:00+00:00"
    conn.close()


def test_missing_token_raises_and_writes_nothing(monkeypatch, tmp_path):
    real_connect, db = _patch_db(monkeypatch, tmp_path)
    monkeypatch.setattr(us, "_load_token", lambda: None)

    with pytest.raises(us.UsageSnapshotError):
        us.fetch_and_write()

    conn = real_connect(db)
    n = conn.execute("SELECT COUNT(*) c FROM ct_rate_limit").fetchone()["c"]
    conn.close()
    assert n == 0


def test_http_failure_raises_and_writes_nothing(monkeypatch, tmp_path):
    real_connect, db = _patch_db(monkeypatch, tmp_path)
    monkeypatch.setattr(us, "_load_token", lambda: "tok-123")

    def _boom(url, headers):
        raise TimeoutError("connect timed out")
    monkeypatch.setattr(us, "_http_get", _boom)

    with pytest.raises(us.UsageSnapshotError):
        us.fetch_and_write()

    conn = real_connect(db)
    n = conn.execute("SELECT COUNT(*) c FROM ct_rate_limit").fetchone()["c"]
    conn.close()
    assert n == 0


def test_non_200_raises_and_writes_nothing(monkeypatch, tmp_path):
    real_connect, db = _patch_db(monkeypatch, tmp_path)
    monkeypatch.setattr(us, "_load_token", lambda: "tok-123")
    monkeypatch.setattr(us, "_http_get", lambda url, headers: (429, b"{}"))

    with pytest.raises(us.UsageSnapshotError):
        us.fetch_and_write()

    conn = real_connect(db)
    n = conn.execute("SELECT COUNT(*) c FROM ct_rate_limit").fetchone()["c"]
    conn.close()
    assert n == 0


def test_bad_json_raises_and_writes_nothing(monkeypatch, tmp_path):
    real_connect, db = _patch_db(monkeypatch, tmp_path)
    monkeypatch.setattr(us, "_load_token", lambda: "tok-123")
    monkeypatch.setattr(us, "_http_get", lambda url, headers: (200, b"not json"))

    with pytest.raises(us.UsageSnapshotError):
        us.fetch_and_write()

    conn = real_connect(db)
    n = conn.execute("SELECT COUNT(*) c FROM ct_rate_limit").fetchone()["c"]
    conn.close()
    assert n == 0


def test_main_returns_0_on_success(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    monkeypatch.setattr(us, "fetch_and_write", lambda: None)
    assert us.main() == 0


def test_main_returns_1_and_logs_stderr_on_failure(monkeypatch, tmp_path, capsys):
    _patch_db(monkeypatch, tmp_path)

    def _raise():
        raise us.UsageSnapshotError("no oauth token available")
    monkeypatch.setattr(us, "fetch_and_write", _raise)

    assert us.main() == 1
    err = capsys.readouterr().err
    assert "no oauth token available" in err
