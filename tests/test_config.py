"""Tests for marrow/config.py path accessors."""
from __future__ import annotations

from pathlib import Path

import pytest

from marrow import config


@pytest.fixture(autouse=True)
def _restore_path_accessors(monkeypatch):
    """Undo conftest's autouse vault patches so we can test real fallback."""
    monkeypatch.setattr(
        config, "dashboard_path",
        lambda: config.load()["paths"]["dashboard"],
    )
    monkeypatch.setattr(
        config, "db_pages_path",
        lambda: config.load()["paths"]["db_pages"],
    )
    monkeypatch.setattr(
        config, "sub_pages_path",
        lambda: config.load()["paths"]["db_pages"],
    )


def test_db_pages_path_defaults_under_ny(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    p = config.db_pages_path()
    assert p.endswith("Desktop/NY/db-pages")
    assert Path(p).is_absolute()


def test_db_pages_state_path_defaults_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    p = config.db_pages_state_path()
    assert p == str(tmp_path / "state")
    assert Path(p).is_absolute()


def test_db_pages_path_overridable(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[paths]\n'
        f'db_pages = "{tmp_path / "pages"}"\n'
        f'db_pages_state = "{tmp_path / "state2"}"\n'
    )
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    assert config.db_pages_path() == str(tmp_path / "pages")
    assert config.db_pages_state_path() == str(tmp_path / "state2")


def test_db_pages_path_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[paths]\ndb_pages = "~/mw_pages"\n')
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    p = config.db_pages_path()
    assert "~" not in p
    assert p == str(Path.home() / "mw_pages")


def test_legacy_sub_pages_key_still_honoured(monkeypatch, tmp_path):
    """Old config.toml with `sub_pages` key keeps working until rewritten."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[paths]\n'
        f'sub_pages = "{tmp_path / "old"}"\n'
        f'sub_pages_state = "{tmp_path / "old_state"}"\n'
    )
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    assert config.db_pages_path() == str(tmp_path / "old")
    assert config.db_pages_state_path() == str(tmp_path / "old_state")
    # Legacy aliases still resolve.
    assert config.sub_pages_path() == str(tmp_path / "old")
