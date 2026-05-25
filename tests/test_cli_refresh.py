"""Tests for `mw refresh` — re-render dashboard (and subpages with --all)."""
from __future__ import annotations

from pathlib import Path

import pytest

from marrow import cli, config, storage


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO tasks(category,title,status,due,next_step) "
        "VALUES('study','Essay 370','active','2026-05-20','write intro')"
    )
    conn.execute(
        "INSERT INTO alerts(severity,type,message) "
        "VALUES('warn','bug','recall returned 0')"
    )
    conn.commit()
    conn.close()

    dash = tmp_path / "dashboard.md"
    sub_folder = tmp_path / "db-pages"
    state = tmp_path / "state"
    monkeypatch.setattr(config, "dashboard_path", lambda: str(dash))
    monkeypatch.setattr(config, "sub_pages_path", lambda: str(sub_folder))
    monkeypatch.setattr(config, "sub_pages_state_path", lambda: str(state))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return p, dash, sub_folder


def test_refresh_writes_dashboard(db):
    p, dash, _ = db
    rc = cli.main(["refresh", "--db", p])
    assert rc == 0
    assert dash.exists()
    assert "<!-- marrow:top:start -->" in dash.read_text()


def test_refresh_all_writes_subpages(db):
    p, dash, sub_folder = db
    rc = cli.main(["refresh", "--all", "--db", p])
    assert rc == 0
    assert dash.exists()
    md_files = list(Path(sub_folder).glob("*.md"))
    assert md_files, f"no subpage md files in {sub_folder}"


def test_refresh_prints_confirmation(db, capsys):
    p, _, _ = db
    cli.main(["refresh", "--db", p])
    assert "dashboard refreshed" in capsys.readouterr().out


def test_refresh_all_prints_subpages_marker(db, capsys):
    p, _, _ = db
    cli.main(["refresh", "--all", "--db", p])
    assert "+ subpages" in capsys.readouterr().out


# ── mw handover --sid ───────────────────────────────────────────────────────

def test_handover_cli_fires_async_popen(db, capsys, monkeypatch):
    """mw handover --sid <sid> spawns sessionend_async detached + prints log path."""
    p, _, _ = db
    spawned: list[list[str]] = []

    def fake_popen(args, log_path):  # noqa: ARG001
        spawned.append(list(args))

    monkeypatch.setattr("marrow.popen_detach.popen_detach", fake_popen)
    rc = cli.main(["handover", "--db", p, "--sid", "test-sid-99"])
    assert rc == 0
    assert len(spawned) == 1
    assert "sessionend_async" in " ".join(spawned[0])
    assert "test-sid-99" in spawned[0]
    out = capsys.readouterr().out
    assert "test-sid-99" in out
    assert "sessionend_async_test-sid-99.log" in out
