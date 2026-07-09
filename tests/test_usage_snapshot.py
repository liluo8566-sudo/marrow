import json

import pytest

from marrow import usage_snapshot as us


@pytest.fixture(autouse=True)
def _no_side_sources(monkeypatch):
    """Default-off cdx + ccusage collectors so the primary-usage tests never
    touch network / npx. Dedicated tests re-patch these explicitly."""
    monkeypatch.setattr(us, "_codex_rows", lambda: [])
    monkeypatch.setattr(us, "_today_net_rows", lambda: [])


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


_REAL_CODEX_ROWS = us._codex_rows
_REAL_TODAY_ROWS = us._today_net_rows


def test_codex_rows_parses_used_percent(monkeypatch, tmp_path):
    monkeypatch.setattr(us, "_codex_rows", _REAL_CODEX_ROWS)  # opt out of autouse stub
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "t", "account_id": "a"}}))
    monkeypatch.setattr(us, "CDX_AUTH", auth)

    class R:
        def read(self_inner):
            return json.dumps({"rate_limit": {
                "primary_window": {"used_percent": 5},
                "secondary_window": {"used_percent": 12.5}}}).encode()
        def __enter__(self_inner):
            return self_inner
        def __exit__(self_inner, *a):
            return False
    monkeypatch.setattr("marrow.usage_snapshot.urllib.request.urlopen",
                        lambda *a, **k: R())
    rows = dict(us._codex_rows())
    assert rows["cdx_five_hour_pct"] == "5.0"
    assert rows["cdx_seven_day_pct"] == "12.5"


def test_codex_rows_empty_without_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(us, "_codex_rows", _REAL_CODEX_ROWS)
    monkeypatch.setattr(us, "CDX_AUTH", tmp_path / "nope.json")
    assert us._codex_rows() == []


def test_today_net_rows_parses_ccusage(monkeypatch):
    monkeypatch.setattr(us, "_today_net_rows", _REAL_TODAY_ROWS)
    class P:
        returncode = 0
        stdout = json.dumps({"daily": [
            {"cacheCreationTokens": 1000, "outputTokens": 200}]})
        stderr = ""
    monkeypatch.setattr(us.subprocess, "run", lambda *a, **k: P())
    assert us._today_net_rows() == [("today_net_tokens", "1200")]


def test_today_net_rows_empty_on_failure(monkeypatch):
    monkeypatch.setattr(us, "_today_net_rows", _REAL_TODAY_ROWS)
    def boom(*a, **k):
        raise OSError("no npx")
    monkeypatch.setattr(us.subprocess, "run", boom)
    assert us._today_net_rows() == []
