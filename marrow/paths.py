"""Centralised path registry. Single source of truth for all Marrow data paths.

Values come from the [paths] section of the config pair:
  repo config.default.toml (defaults) + ~/.config/marrow/config.toml (overrides).
Read directly with tomllib here (no config import) to avoid a circular import.

Usage:
    from marrow.paths import paths
    db = paths.marrow_db
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

_DATA_DIR = Path.home() / ".config" / "marrow"
_USER_CONFIG = _DATA_DIR / "config.toml"
_DEFAULT_CONFIG = Path(__file__).with_name("config.default.toml")

# Config [paths] key -> Paths attribute. Legacy config keys read as fallback.
_KEYS: dict[str, tuple[str, ...]] = {
    "marrow_db": ("db", "marrow_db"),
    "ny_root": ("ny_root",),
    "daybrief_md": ("daybrief", "daybrief_md"),
    "drift_pending_dir": ("drift_pending_dir",),
    "drift_backup_dir": ("drift_backup_dir",),
    "dir_tree_md": ("dir_tree_md",),
    "logs_dir": ("logs_dir",),
    "state_dir": ("state_dir",),
}

_DEFAULTS = {
    "marrow_db": "~/.config/marrow/marrow.db",
    "ny_root": "",
    "daybrief_md": "",
    "drift_pending_dir": "~/.config/marrow/drift_pending",
    "drift_backup_dir": "~/.config/marrow/drift_backup",
    "dir_tree_md": "~/.config/marrow/dir_tree.md",
    "logs_dir": "~/.config/marrow/logs",
    "state_dir": "~/.config/marrow/state",
}


@dataclass
class Paths:
    marrow_db: Path
    ny_root: Path
    daybrief_md: Path
    drift_pending_dir: Path
    drift_backup_dir: Path
    dir_tree_md: Path
    logs_dir: Path
    state_dir: Path


def _read_paths_section(toml_path: Path) -> dict[str, str]:
    if not toml_path.is_file():
        return {}
    with toml_path.open("rb") as f:
        return tomllib.load(f).get("paths", {})


def load_paths(toml_path: str | Path | None = None) -> Paths:
    """Merge [paths] from config.default.toml + user config.toml (overrides win).

    toml_path overrides the user config location (for tests).
    """
    merged = dict(_read_paths_section(_DEFAULT_CONFIG))
    user_file = Path(toml_path) if toml_path is not None else _USER_CONFIG
    merged.update(_read_paths_section(user_file))

    def _p(attr: str) -> Path:
        val = ""
        for key in _KEYS[attr]:
            if merged.get(key):
                val = merged[key]
                break
        if not val:
            val = _DEFAULTS[attr]
        if not val:
            return Path("")
        return Path(val).expanduser()

    return Paths(**{attr: _p(attr) for attr in _KEYS})


paths: Paths = load_paths()
