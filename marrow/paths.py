"""Centralised path registry. Single source of truth for all Marrow data paths.

Usage:
    from marrow.paths import paths
    db = paths.marrow_db
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

_DATA_DIR = Path.home() / ".config" / "marrow"
_DEFAULT_TOML = _DATA_DIR / "paths.toml"

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


def load_paths(toml_path: str | Path | None = None) -> Paths:
    """Load from toml_path (or MARROW_PATHS_FILE env), fallback to hardcoded defaults."""
    env_val = os.environ.get("MARROW_PATHS_FILE", "")
    if toml_path is not None:
        resolved: Path | None = Path(toml_path)
    elif env_val:
        resolved = Path(env_val)
    else:
        resolved = _DEFAULT_TOML
    raw: dict[str, str] = {}
    if resolved is not None and resolved.is_file():
        with resolved.open("rb") as f:
            raw = tomllib.load(f)

    def _p(key: str) -> Path:
        val = raw.get(key) or _DEFAULTS[key]
        if not val:
            return Path("")
        return Path(val).expanduser()

    return Paths(
        marrow_db=_p("marrow_db"),
        ny_root=_p("ny_root"),
        daybrief_md=_p("daybrief_md"),
        drift_pending_dir=_p("drift_pending_dir"),
        drift_backup_dir=_p("drift_backup_dir"),
        dir_tree_md=_p("dir_tree_md"),
        logs_dir=_p("logs_dir"),
        state_dir=_p("state_dir"),
    )


paths: Paths = load_paths()
