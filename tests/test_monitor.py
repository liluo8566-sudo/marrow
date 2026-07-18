"""Tests for marrow/monitor.py — the alert surface (dashboard block successor).

Covers: render output (H1 + anchored alerts block, resolved=0 only, ordering),
and the md-delete=resolve round-trip through update() — the same reconcile_alerts
mtime-gated absorb the dashboard used, now pointed at monitor.md.
"""
from __future__ import annotations

import os
import time

import pytest

from marrow import config, monitor, repo, storage


@pytest.fixture()
def conn(tmp_path):
    c = storage.init_db(str(tmp_path / "db.db"))
    yield c
    c.close()


@pytest.fixture()
def monitor_file(conn, tmp_path, monkeypatch):
    """Point update()'s out path + alert db at tmp fixtures."""
    path = str(tmp_path / "monitor.md")
    db = str(tmp_path / "db.db")
    monkeypatch.setattr(monitor, "_out_path", lambda: path)
    monkeypatch.setattr(config, "db_path", lambda: db)
    return path


def _add(conn, severity: str, atype: str, fp: str, msg: str, db: str) -> int:
    return repo.add_alert(severity, atype, fp, message=msg, db=db)


# ── render ───────────────────────────────────────────────────────────────────

def test_render_empty_state(conn):
    out = monitor.render(conn)
    assert "# Monitor" in out
    assert "## Alerts" in out
    assert "<!-- alert-block-anchored -->" in out
    assert "<!-- id:monitor.alerts -->" in out
    assert "_none_" in out


def test_render_unresolved_with_anchors(conn, tmp_path):
    db = str(tmp_path / "db.db")
    aid = _add(conn, "warn", "t1", "fp1", "first alert", db)
    out = monitor.render(conn)
    assert "- warn: first alert" in out
    assert f"<!-- id:alert.{aid} -->" in out


def test_render_severity_ordering(conn, tmp_path):
    db = str(tmp_path / "db.db")
    _add(conn, "info", "ti", "fpi", "info msg", db)
    _add(conn, "critical", "tc", "fpc", "crit msg", db)
    _add(conn, "warn", "tw", "fpw", "warn msg", db)
    out = monitor.render(conn)
    ci = out.index("crit msg")
    wi = out.index("warn msg")
    ii = out.index("info msg")
    assert ci < wi < ii  # critical → warn → info


def test_render_excludes_resolved(conn, tmp_path):
    db = str(tmp_path / "db.db")
    aid = _add(conn, "warn", "tr", "fpr", "to resolve", db)
    conn.execute("UPDATE alerts SET resolved=1 WHERE id=?", (aid,))
    conn.commit()
    out = monitor.render(conn)
    assert "to resolve" not in out
    assert "_none_" in out


# ── update() write + md-delete=resolve round-trip ────────────────────────────

def test_update_writes_file(conn, monitor_file, tmp_path):
    db = str(tmp_path / "db.db")
    _add(conn, "warn", "tu", "fpu", "rendered alert", db)
    monitor.update(conn)
    text = open(monitor_file, encoding="utf-8").read()
    assert "# Monitor" in text
    assert "rendered alert" in text


def test_update_empty_state_clean(conn, monitor_file):
    monitor.update(conn)
    text = open(monitor_file, encoding="utf-8").read()
    assert "## Alerts" in text
    assert "_none_" in text


def test_update_delete_line_resolves(conn, monitor_file, tmp_path):
    db = str(tmp_path / "db.db")
    aid = _add(conn, "warn", "td", "fpd", "delete me", db)
    monitor.update(conn)
    text = open(monitor_file, encoding="utf-8").read()
    assert "delete me" in text

    # Backdate created_at so the mtime gate lets the delete through (row must
    # be older than the md snapshot).
    conn.execute(
        "UPDATE alerts SET created_at='2000-01-01T00:00:00Z' WHERE id=?", (aid,))
    conn.commit()

    kept = "\n".join(ln for ln in text.splitlines() if "delete me" not in ln)
    # Ensure a newer mtime than the DB row so reconcile treats it as a delete.
    time.sleep(0.01)
    open(monitor_file, "w", encoding="utf-8").write(kept)
    os.utime(monitor_file, None)

    monitor.update(conn)

    resolved = conn.execute(
        "SELECT resolved FROM alerts WHERE id=?", (aid,)).fetchone()[0]
    assert resolved == 1
    # Re-render no longer shows the line.
    assert "delete me" not in open(monitor_file, encoding="utf-8").read()


def test_update_mtime_gate_keeps_new_alert(conn, monitor_file, tmp_path):
    """An alert created AFTER the md snapshot is not resolved by a delete pass
    — the mtime gate protects background-added alerts (zero-anchor guard path)."""
    db = str(tmp_path / "db.db")
    # First render creates an empty (_none_) file.
    monitor.update(conn)
    # Add an alert with a future created_at (newer than the md mtime).
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() + 3600))
    conn.execute(
        "INSERT INTO alerts (severity, type, fingerprint, message, created_at,"
        " resolved) VALUES ('warn','tg','fpg','future alert',?,0)", (future,))
    conn.commit()
    aid = conn.execute("SELECT id FROM alerts WHERE fingerprint='fpg'").fetchone()[0]

    # md still shows _none_ (no anchors); reconcile must NOT mass-resolve the
    # newer row because its created_at is after the md snapshot.
    monitor.update(conn)

    resolved = conn.execute(
        "SELECT resolved FROM alerts WHERE id=?", (aid,)).fetchone()[0]
    assert resolved == 0
    # And it now renders into the file.
    assert "future alert" in open(monitor_file, encoding="utf-8").read()
