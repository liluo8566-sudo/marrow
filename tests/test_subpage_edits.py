"""End-to-end: subpage hand-edits survive `mw refresh --all`.

Subpages use the inserter (`marrow/inserter.py`) which preserves any
md block whose `<!-- id:* -->` is already on disk. This test exercises
the full mw refresh path including the new observe-only scan phase.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from marrow import cli, config, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    conn.commit()
    conn.close()
    sub = tmp_path / "db-pages"
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    monkeypatch.setattr(config, "dashboard_path", lambda: str(dash))
    monkeypatch.setattr(config, "sub_pages_path", lambda: str(sub))
    monkeypatch.setattr(config, "sub_pages_state_path", lambda: str(state))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return p, dash, sub


def test_subpage_hand_edit_survives_refresh_all(env):
    """User appends a hand-anchored row to profile.md, runs refresh --all
    twice — the row must remain across both refreshes."""
    db, _, sub = env
    # First refresh bootstraps subpage files.
    assert cli.main(["refresh", "--all", "--db", db]) == 0
    profile = Path(sub) / "profile.md"
    if not profile.exists():
        pytest.skip("profile subpage not produced in this fixture")
    body = profile.read_text(encoding="utf-8")
    profile.write_text(
        body
        + "\n<!-- id:profile.user.note -->\n- private note from user\n",
        encoding="utf-8",
    )

    # Second refresh — must NOT clobber the appended block.
    assert cli.main(["refresh", "--all", "--db", db]) == 0
    after = profile.read_text(encoding="utf-8")
    assert "id:profile.user.note" in after
    assert "private note from user" in after

    # Third refresh — same invariant.
    assert cli.main(["refresh", "--all", "--db", db]) == 0
    after2 = profile.read_text(encoding="utf-8")
    assert "private note from user" in after2


def test_subpage_existing_block_edit_survives(env):
    """User edits the body of an existing inserter-managed block; refresh
    must not rewrite the body (inserter preserves md when block_id in md)."""
    db, _, sub = env
    assert cli.main(["refresh", "--all", "--db", db]) == 0
    profile = Path(sub) / "profile.md"
    if not profile.exists():
        pytest.skip("profile subpage not produced in this fixture")
    # Add a hand block we own end-to-end.
    profile.write_text(
        profile.read_text(encoding="utf-8")
        + "\n<!-- id:profile.handadd -->\n- original\n",
        encoding="utf-8",
    )
    assert cli.main(["refresh", "--all", "--db", db]) == 0
    # Now edit the body verbatim.
    raw = profile.read_text(encoding="utf-8").replace("- original",
                                                       "- edited by user")
    profile.write_text(raw, encoding="utf-8")
    assert cli.main(["refresh", "--all", "--db", db]) == 0
    final = profile.read_text(encoding="utf-8")
    assert "- edited by user" in final
    assert "- original" not in final
