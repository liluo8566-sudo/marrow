"""Tests for `mw refresh` — re-render daybrief + monitor (and subpages with --all)."""
from __future__ import annotations

from pathlib import Path

import pytest

from marrow import cli, config, storage


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO alerts(severity,type,message) "
        "VALUES('warn','bug','recall returned 0')"
    )
    conn.commit()
    conn.close()

    sub_folder = tmp_path / "db-pages"
    state = tmp_path / "state"
    monkeypatch.setattr(config, "sub_pages_path", lambda: str(sub_folder))
    monkeypatch.setattr(config, "sub_pages_state_path", lambda: str(state))
    monkeypatch.setattr(config, "daybrief_path", lambda: str(tmp_path / "daybrief.md"))
    monkeypatch.setattr(config, "monitor_path", lambda: str(tmp_path / "monitor.md"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return p, sub_folder


def test_refresh_succeeds(db):
    p, _ = db
    rc = cli.main(["refresh", "--db", p])
    assert rc == 0


def test_refresh_writes_monitor(db, tmp_path):
    p, _ = db
    assert cli.main(["refresh", "--db", p]) == 0
    monitor = tmp_path / "monitor.md"
    assert monitor.exists()
    assert "recall returned 0" in monitor.read_text()


def test_refresh_all_writes_subpages(db):
    p, sub_folder = db
    rc = cli.main(["refresh", "--all", "--db", p])
    assert rc == 0
    md_files = list(Path(sub_folder).glob("*.md"))
    assert md_files, f"no subpage md files in {sub_folder}"


def test_refresh_prints_confirmation(db, capsys):
    p, _ = db
    cli.main(["refresh", "--db", p])
    out = capsys.readouterr().out
    assert "daybrief" in out or "monitor" in out


def test_refresh_all_prints_subpages_marker(db, capsys):
    p, _ = db
    cli.main(["refresh", "--all", "--db", p])
    assert "+ subpages" in capsys.readouterr().out


def test_refresh_all_scans_subpage_md_into_md_index(db, tmp_path):
    """With --all, refresh full_scans subpage md files and re-renders."""
    p, sub_folder = db
    # First --all to bootstrap subpage files.
    assert cli.main(["refresh", "--all", "--db", p]) == 0
    # Hand-edit one subpage by injecting a marker.
    profile = Path(sub_folder) / "profile.md"
    if not profile.exists():
        pytest.skip("profile subpage not produced in this fixture")
    body = profile.read_text(encoding="utf-8")
    profile.write_text(
        body + "\n<!-- id:profile.handadd -->\n- hand line\n",
        encoding="utf-8",
    )
    rc = cli.main(["refresh", "--all", "--db", p])
    assert rc == 0
    conn = storage.connect(p)
    row = conn.execute(
        "SELECT 1 FROM md_index WHERE path=? AND block_id=?",
        (str(profile), "profile.handadd"),
    ).fetchone()
    conn.close()
    assert row is not None, "scan phase did not pick up subpage hand-edit"
