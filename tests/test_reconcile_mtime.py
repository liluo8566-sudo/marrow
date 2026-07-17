"""mtime gate tests for reconcile UPDATE/archive passes.

For each gate: insert a row with a future timestamp, run reconcile,
assert the row is not overwritten by stale md content.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from marrow import reconcile, storage


@pytest.fixture()
def conn(tmp_path):
    c = storage.init_db(str(tmp_path / "t.db"))
    yield c
    c.close()


def _future_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))


def _past_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))


# ── 1. milestones UPDATE gate ─────────────────────────────────────────────────

def _ms_md(mid: int, title: str, desc: str) -> str:
    return (
        f"<!-- marrow:milestone:start -->\n"
        f"## Me\n"
        f"##### [2026-01-01] {title}\n"
        f"{desc} <!-- id:{mid} -->\n"
        f"<!-- marrow:milestone:end -->\n"
    )


def test_milestones_update_gate_skips_newer_row(conn, tmp_path):
    """Row updated after md snapshot must not be overwritten."""
    cur = conn.execute(
        "INSERT INTO milestones (scope, date, title, description, theme, pinned, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        ("Me", "2026-01-01", "Old title", "Old desc", None, _past_ts(), _past_ts()),
    )
    conn.commit()
    mid = cur.lastrowid

    # md reflects old title — reconcile would normally update to "Old title"
    # but DB row now has a future updated_at, so gate should block it
    md = tmp_path / "milestones.md"
    md.write_text(_ms_md(mid, "Old title", "Old desc"))

    # Simulate: DB row updated AFTER the md was written
    conn.execute(
        "UPDATE milestones SET title=?, updated_at=? WHERE id=?",
        ("Newer title", _future_ts(), mid),
    )
    conn.commit()

    rpt = reconcile.reconcile_milestones(conn, md)

    row = conn.execute("SELECT title FROM milestones WHERE id=?", (mid,)).fetchone()
    assert row["title"] == "Newer title", "mtime gate failed: DB row was overwritten"
    assert rpt.updated == 0


def test_milestones_update_gate_allows_older_row(conn, tmp_path):
    """Row updated before md snapshot IS allowed to be overwritten."""
    cur = conn.execute(
        "INSERT INTO milestones (scope, date, title, description, theme, pinned, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        ("Me", "2026-01-01", "Old title", "Old desc", None, _past_ts(), _past_ts()),
    )
    conn.commit()
    mid = cur.lastrowid

    md = tmp_path / "milestones.md"
    md.write_text(_ms_md(mid, "New title", "New desc"))

    rpt = reconcile.reconcile_milestones(conn, md)

    row = conn.execute("SELECT title FROM milestones WHERE id=?", (mid,)).fetchone()
    assert row["title"] == "New title"
    assert rpt.updated == 1

