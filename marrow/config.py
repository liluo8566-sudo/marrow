"""Paths + config load. Data lives under ~/.config/marrow/, never in the repo."""
from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path.home() / ".config" / "marrow"
CONFIG_PATH = DATA_DIR / "config.toml"
_DEFAULT = Path(__file__).with_name("config.default.toml")


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        shutil.copyfile(_DEFAULT, CONFIG_PATH)
    return DATA_DIR


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursive merge: overlay keys win, dict-valued keys merge in-place.
    Lists/scalars are replaced, not concatenated. Needed so a new
    config.default.toml key (e.g. [recall]) lands on existing installs
    without forcing users to hand-edit ~/.config/marrow/config.toml.
    """
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    ensure_data_dir()
    from .paths import paths as _mpaths  # lazy to avoid circular at module init
    with _DEFAULT.open("rb") as f:
        cfg = tomllib.load(f)
    user_paths: dict = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as f:
            user = tomllib.load(f)
        user_paths = user.get("paths", {})
        cfg = _deep_merge(cfg, user)
    paths = cfg.setdefault("paths", {})
    db = paths.get("db") or str(DATA_DIR / "marrow.db")
    backup = paths.get("backup_dir") or str(DATA_DIR / "backup")
    offsite = paths.get("offsite_backup_dir") or str(
        Path.home() / "Library" / "Mobile Documents"
        / "com~apple~CloudDocs" / "Backup" / "marrow"
    )
    daybrief = paths.get("daybrief") or (
        str(_mpaths.daybrief_md) if _mpaths.daybrief_md != Path("") else
        str(DATA_DIR / "daybrief.md")
    )
    # `db_pages` = folder of md files rendered from DB (was `sub_pages` until
    # 2026-05-24). Name signals provenance: rendered-from-DB vs hand-written
    # notes elsewhere in the Obsidian vault. Legacy `sub_pages` key still read
    # as a fallback so an old config.toml keeps working until rewritten.
    # Check user config directly for legacy key so the default `db_pages` value
    # in config.default.toml does not shadow an explicit user `sub_pages` entry.
    sub = (user_paths.get("db_pages") or user_paths.get("sub_pages")
           or paths.get("db_pages") or paths.get("sub_pages") or (
               str(_mpaths.ny_root / "db-pages") if _mpaths.ny_root != Path("") else
               str(DATA_DIR / "db-pages")
           ))
    sub_state = (
        paths.get("db_pages_state")
        or paths.get("sub_pages_state")
        or str(DATA_DIR / "state")
    )
    paths["db"] = db
    paths["backup_dir"] = backup
    paths["offsite_backup_dir"] = offsite
    paths["daybrief"] = str(Path(daybrief).expanduser())
    paths["db_pages"] = str(Path(sub).expanduser())
    paths["db_pages_state"] = str(Path(sub_state).expanduser())
    # monitor.md (alerts surface). Empty = <db_pages>/monitor.md so it lands
    # beside the other DB-rendered pages without manual path setup.
    monitor = paths.get("monitor") or str(Path(paths["db_pages"]) / "monitor.md")
    paths["monitor"] = str(Path(monitor).expanduser())
    # Legacy keys kept synchronised so any caller still using sub_pages_path()
    # gets the same path.
    paths["sub_pages"] = paths["db_pages"]
    paths["sub_pages_state"] = paths["db_pages_state"]
    cfg.setdefault("backup", {}).setdefault("keep", 14)
    Path(backup).mkdir(parents=True, exist_ok=True)
    return cfg


def persona() -> dict:
    """Merged persona config with sanitized fallbacks."""
    raw = load().get("persona", {})
    uname = (raw.get("user_name") or "").strip() or "User"
    aname = (raw.get("assistant_name") or "").strip() or "Assistant"
    umark = (raw.get("user_marker") or "").strip() or "U"
    amark = (raw.get("assistant_marker") or "").strip() or "A"
    def _strlist(key: str) -> list[str]:
        return [s.strip() for s in raw.get(key, [])
                if isinstance(s, str) and s.strip()]
    return {
        "user_name": uname,
        "assistant_name": aname,
        "user_marker": umark,
        "assistant_marker": amark,
        "user_aliases": _strlist("user_aliases"),
        "assistant_aliases": _strlist("assistant_aliases"),
        "relationship_terms": _strlist("relationship_terms"),
        "anchor_keys": _strlist("anchor_keys"),
        "meme_exclude_terms": _strlist("meme_exclude_terms"),
    }


def all_user_terms() -> list[str]:
    p = persona()
    return [p["user_name"]] + p["user_aliases"] + p["relationship_terms"]


def all_assistant_terms() -> list[str]:
    p = persona()
    return [p["assistant_name"]] + p["assistant_aliases"]


def anchor_keys_set() -> frozenset[str]:
    return frozenset(persona()["anchor_keys"])


def daybrief_path() -> str:
    return load()["paths"]["daybrief"]


def monitor_path() -> str:
    return load()["paths"]["monitor"]


def db_path() -> str:
    return load()["paths"]["db"]


def db_pages_path() -> str:
    return load()["paths"]["db_pages"]


def db_pages_state_path() -> str:
    return load()["paths"]["db_pages_state"]


# Legacy aliases — kept until all callers move to db_pages_path().
sub_pages_path = db_pages_path
sub_pages_state_path = db_pages_state_path


def get_tz() -> ZoneInfo:
    tz_name = load().get("core", {}).get("timezone", "Asia/Shanghai")
    return ZoneInfo(tz_name)
