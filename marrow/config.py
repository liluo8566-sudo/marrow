"""Paths + config load. Data lives under ~/.config/marrow/, never in the repo."""
from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

DATA_DIR = Path.home() / ".config" / "marrow"
CONFIG_PATH = DATA_DIR / "config.toml"
_DEFAULT = Path(__file__).with_name("config.default.toml")


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        shutil.copyfile(_DEFAULT, CONFIG_PATH)
    return DATA_DIR


def load() -> dict:
    ensure_data_dir()
    with CONFIG_PATH.open("rb") as f:
        cfg = tomllib.load(f)
    paths = cfg.setdefault("paths", {})
    db = paths.get("db") or str(DATA_DIR / "marrow.db")
    backup = paths.get("backup_dir") or str(DATA_DIR / "backup")
    offsite = paths.get("offsite_backup_dir") or str(
        Path.home() / "Library" / "Mobile Documents"
        / "com~apple~CloudDocs" / "marrow-backup"
    )
    dash = paths.get("dashboard") or str(
        Path.home() / "Desktop" / "NY" / "dashboard.md"
    )
    paths["db"] = db
    paths["backup_dir"] = backup
    paths["offsite_backup_dir"] = offsite
    paths["dashboard"] = dash
    cfg.setdefault("backup", {}).setdefault("keep", 14)
    Path(backup).mkdir(parents=True, exist_ok=True)
    return cfg


def dashboard_path() -> str:
    return load()["paths"]["dashboard"]


def db_path() -> str:
    return load()["paths"]["db"]
