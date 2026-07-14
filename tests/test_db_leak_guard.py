"""Regression: pytest must never write to the real ~/.config/marrow/marrow.db.

History: a `_repo.add_alert(...)` call in reconcile.py omitted `db=`, so the
fallback `config.db_path()` resolved to production and leaked
`unanchored task dedup: ...` rows into the real alerts table during pytest.

Root-cause fix: that call site is now silent (35032ea4). Belt-and-braces:
`tests/conftest.py` redirects `marrow.config.DATA_DIR`/`CONFIG_PATH` to a
session tmp dir so any future slip still misses prod.

This test bypasses the patched config to read the *real* db via a
side-channel sqlite3 connection, snapshots the alerts row count, exercises
two leak vectors (direct `add_alert` without `db=`, plus a reconcile flow),
then asserts the real count is unchanged.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from marrow import config, reconcile, repo, storage


_REAL_DB = Path(os.path.expanduser("~/.config/marrow/marrow.db"))


def _real_alerts_count() -> int | None:
    """Side-channel count of real production alerts table. Returns None if
    the real db file does not exist (fresh machine / CI).

    Connects READ-ONLY (file: URI mode=ro): the conftest barrier now rejects any
    WRITABLE sqlite3.connect under the real ~/.config/marrow/ tree, and a
    read-only count must never migrate or create the db."""
    if not _REAL_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{_REAL_DB}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    finally:
        conn.close()


def test_sqlite_connect_to_real_db_raises():
    """The barrier must reject a plain (writable) sqlite3.connect to the real
    marrow.db — the C-extension path that bypasses the open() patches. A
    read-only `file:...?mode=ro` connection stays allowed."""
    with pytest.raises(AssertionError, match="real ~/.config/marrow"):
        sqlite3.connect(str(_REAL_DB))
    with pytest.raises(AssertionError, match="real ~/.config/marrow"):
        sqlite3.connect(f"file:{_REAL_DB}?mode=rwc", uri=True)
    # read-only connection is permitted (skip if the real db is absent)
    if _REAL_DB.exists():
        conn = sqlite3.connect(f"file:{_REAL_DB}?mode=ro", uri=True)
        conn.close()


def test_write_barrier_blocks_real_marrow_writes():
    """The conftest hard wall must FAIL LOUDLY on any WRITE under the real
    ~/.config/marrow/ tree — the guard that keeps a non-isolated test from
    polluting the live wake_signal.log / wake_audit.log (07-14 incident).
    Reads stay allowed; only the write attempt raises."""
    real_cortex_log = _REAL_DB.parent / "cortex" / "wake_signal.log"
    for opener in (
        lambda: open(real_cortex_log, "a"),
        lambda: Path(real_cortex_log).open("w"),
        lambda: os.open(str(real_cortex_log), os.O_CREAT | os.O_WRONLY),
    ):
        with pytest.raises(AssertionError, match="real ~/.config/marrow"):
            opener()


def test_conftest_redirects_db_path_off_production():
    """The session-scoped guard must point config.db_path() away from real db."""
    p = config.db_path()
    assert str(_REAL_DB) != p, (
        f"config.db_path()={p} still resolves to production db"
    )
    assert "/marrow-data" in p or "pytest" in p.lower(), (
        f"config.db_path()={p} is not under a pytest tmp dir"
    )


def test_add_alert_without_db_arg_does_not_touch_real_db():
    """Simulate the historical bug: caller forgets `db=`. Guard must absorb.

    Initialise the redirected tmp db with full schema so the write lands in
    tmp (proving the redirect path is exercised end-to-end); then verify the
    real db is untouched. Without the guard, this write would hit production.
    """
    before = _real_alerts_count()
    # Initialise schema on the tmp db that config.db_path() now points to.
    storage.init_db(config.db_path()).close()
    # Deliberately omit db= — pre-guard this leaked to production.
    aid = repo.add_alert("info", "leak_test",
                         "regression probe: would have leaked to prod")
    assert aid is not None
    # Confirm the write hit the tmp db, not real.
    tmp_conn = sqlite3.connect(config.db_path())
    try:
        n_tmp = tmp_conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE type='leak_test'"
        ).fetchone()[0]
    finally:
        tmp_conn.close()
    assert n_tmp == 1, "alert should have landed in tmp db, not vanished"
    after = _real_alerts_count()
    if before is None and after is None:
        pytest.skip("real db absent — nothing to regress against")
    assert after == before, (
        f"add_alert() without db= leaked into real db ({before} -> {after})"
    )


def test_reconcile_tasks_dup_path_does_not_leak(tmp_path):
    """Exercise the historical dup-title reconcile path; assert real db
    unchanged. The current code dedups silently, but if a future revert
    re-adds the alert without db=, the conftest guard still catches it."""
    before = _real_alerts_count()

    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    try:
        conn.execute(
            "INSERT INTO tasks (category, title, status, updated_at)"
            " VALUES ('Study', 'dup title matches active id=1', 'active',"
            "         strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        )
        conn.commit()
        dash = tmp_path / "dashboard.md"
        dash.write_text(
            "## Tasks\n"
            "- [ ] [Study] dup title matches active id=1\n"
            "<!-- cand:task:ids=[] -->\n",
            encoding="utf-8",
        )
        reconcile.reconcile_tasks(conn, dash)
    finally:
        conn.close()

    after = _real_alerts_count()
    if before is None and after is None:
        pytest.skip("real db absent — nothing to regress against")
    assert after == before, (
        f"reconcile_tasks dup path leaked into real db ({before} -> {after})"
    )
