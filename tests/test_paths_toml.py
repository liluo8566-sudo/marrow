"""Tests for marrow/paths.py — TOML loader + env override + regression smoke."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from marrow.paths import load_paths, _DEFAULTS


def test_load_default(tmp_path):
    """When toml is absent (nonexistent path), fallback defaults are used and expanduser'd."""
    p = load_paths(toml_path=tmp_path / "nonexistent.toml")
    assert p.marrow_db == Path(_DEFAULTS["marrow_db"]).expanduser()
    assert p.logs_dir == Path(_DEFAULTS["logs_dir"]).expanduser()
    assert p.state_dir == Path(_DEFAULTS["state_dir"]).expanduser()
    # ny_root and dashboard_md default to empty (user must configure)
    assert p.ny_root == Path("")
    assert p.dashboard_md == Path("")
    # Non-optional paths must be absolute (expanduser applied)
    assert p.marrow_db.is_absolute()


def test_load_custom(tmp_path, monkeypatch):
    """MARROW_PATHS_FILE env override: values from the custom toml are returned."""
    custom_db = tmp_path / "custom.db"
    custom_toml = tmp_path / "test_paths.toml"
    custom_toml.write_text(
        f'marrow_db = "{custom_db}"\n'
        f'ny_root = "{tmp_path}"\n'
        f'dashboard_md = "{tmp_path / "dash.md"}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARROW_PATHS_FILE", str(custom_toml))
    p = load_paths()  # picks up env var
    assert p.marrow_db == custom_db
    assert p.ny_root == tmp_path
    assert p.dashboard_md == tmp_path / "dash.md"
    # Keys not in the custom toml fall back to defaults
    assert p.logs_dir == Path(_DEFAULTS["logs_dir"]).expanduser()


def test_load_custom_via_arg(tmp_path):
    """Explicit toml_path arg overrides default."""
    custom_toml = tmp_path / "explicit.toml"
    custom_toml.write_text(
        f'logs_dir = "{tmp_path / "my_logs"}"\n',
        encoding="utf-8",
    )
    p = load_paths(toml_path=custom_toml)
    assert p.logs_dir == tmp_path / "my_logs"
    # Other keys still default
    assert p.marrow_db == Path(_DEFAULTS["marrow_db"]).expanduser()


def test_no_regression_paths_import(tmp_path, monkeypatch):
    """Importing paths module doesn't crash and returns a Paths dataclass."""
    import marrow.paths as mp
    # Module-level singleton exists
    assert hasattr(mp, "paths")
    assert hasattr(mp.paths, "marrow_db")
    assert hasattr(mp.paths, "dashboard_md")
    assert hasattr(mp.paths, "logs_dir")
    assert hasattr(mp.paths, "state_dir")
    assert hasattr(mp.paths, "ny_root")


def test_no_regression_config_load(tmp_path, monkeypatch):
    """config.load() works with paths-sourced defaults — no crash, dashboard/db paths sane."""
    import marrow.config as cfg
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.toml")
    # Ensure config.default.toml is accessible
    result = cfg.load()
    assert "paths" in result
    # db should be an absolute path string
    assert Path(result["paths"]["db"]).is_absolute()
    # dashboard should be an absolute path string
    assert Path(result["paths"]["dashboard"]).is_absolute()


def test_no_regression_hooks_import(monkeypatch):
    """hooks.py imports cleanly — no missing path attributes."""
    import marrow.hooks  # noqa: F401 — import must not raise


def test_no_regression_watcher_logs(tmp_path, monkeypatch):
    """watcher._logs_dir() returns a Path under config.DATA_DIR (monkeypatch-safe)."""
    from marrow import config, watcher
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    d = watcher._logs_dir()
    assert d == tmp_path / "logs"
    assert d.is_dir()
