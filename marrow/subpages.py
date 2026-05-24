"""Config-driven sub-page render from SQLite (DESIGN L90-101).

Contract:
- New sub-page = new SubPageConfig entry + table, not a base rewrite (goal 7).
- Same render contract for all views: marker-partition, atomic temp+replace
  write. md->DB reconcile runs first so anchored hand-edits flow back to DB.
- Free-form hand-edits inside the rendered block are silently overwritten
  on next render (DB is SoT; non-anchored text is not preserved).
- Cheatsheet exception: read_only=True, always overwrites.
- Render functions live in subpages_render.py.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config as _config
from . import repo
from .reconcile import reconcile_milestones
from .subpages_render import (
    render_cheatsheet,
    render_diary,
    render_goose,
    render_memes,
    render_milestone,
    render_pit,
    render_profile,
    render_project_page,
    render_projects_index,
    render_stickers,
    render_study_index,
    render_study_unit,
    render_wallet,
)

_MARKER_START = "<!-- marrow:{key}:start -->"
_MARKER_END = "<!-- marrow:{key}:end -->"


def _m0(key: str) -> str:
    return _MARKER_START.format(key=key)


def _m1(key: str) -> str:
    return _MARKER_END.format(key=key)


# ---------------------------------------------------------------------------
# Render config
# ---------------------------------------------------------------------------

@dataclass
class SubPageConfig:
    """Render config for one sub-page or sub-page folder."""
    key: str                          # marker key
    render: Callable[[sqlite3.Connection], str]  # returns full block incl markers
    path: str                         # absolute path to the .md file
    state_dir: str                    # dir for sub-page state (reserved)
    read_only: bool = False           # always overwrite full file
    # md->DB reconcile callback. Runs BEFORE render so Lumi's md edits flow
    # back to DB and the freshly-rendered block reflects them. None = skip.
    reconcile: Callable[[sqlite3.Connection, "Path"], object] | None = None
    subpages: list["SubPageConfig"] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Write (atomic)
# ---------------------------------------------------------------------------

def _atomic_write(path: str, data: str) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".mrw.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _split(text: str, key: str) -> tuple[str, str, str]:
    """(before, block_incl_markers, after); block '' if markers absent."""
    m0, m1 = _m0(key), _m1(key)
    i, j = text.find(m0), text.find(m1)
    if i == -1 or j == -1 or j < i:
        return text, "", ""
    return text[:i], text[i:j + len(m1)], text[j + len(m1):]


def write_subpage(cfg: SubPageConfig, conn: sqlite3.Connection,
                  db: str | None = None) -> None:
    """Render + write one sub-page; recurse into children.

    Order: reconcile (md->DB) -> render -> atomic write.
    Reconcile absorbs anchored md edits into DB so the new render reflects
    them. Free-form text inside the rendered block is silently overwritten;
    Lumi's hand-edits are intentional and DB stays SoT.
    """
    path, key = cfg.path, cfg.key
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)

    # Run reconcile BEFORE render so the new block reflects Lumi's edits.
    if cfg.reconcile is not None and os.path.exists(path) and not cfg.read_only:
        try:
            cfg.reconcile(conn, Path(path))
        except Exception as e:
            repo.add_alert(
                "warn", "db_pages",
                f"{key} reconcile failed: {e}; falling through to render",
                source="subpages.py", db=db,
            )

    block = cfg.render(conn)
    existing = Path(path).read_text(encoding="utf-8") if os.path.exists(path) else ""

    if cfg.read_only:
        _atomic_write(path, block + "\n")
    else:
        before, cur_block, after = _split(existing, key)
        if cur_block:
            new = before + block + after
        elif existing:
            new = block + "\n\n" + existing
        else:
            new = block + "\n"
        _atomic_write(path, new)

    for child in cfg.subpages:
        write_subpage(child, conn, db=db)


# ---------------------------------------------------------------------------
# Config builders for folder-based views (Study + Projects)
# ---------------------------------------------------------------------------

def build_study_configs(conn: sqlite3.Connection,
                        folder: str, state_dir: str) -> SubPageConfig:
    """Study index + one child per unit (tasks grouped by title prefix)."""
    rows = conn.execute(
        "SELECT id, title, due, status, next_step "
        "FROM tasks WHERE category = 'study' "
        "ORDER BY (due IS NULL), due, created_at"
    ).fetchall()
    tasks = [dict(r) for r in rows]

    units: dict[str, list[dict]] = defaultdict(list)
    for t in tasks:
        unit = t["title"].split(":")[0].strip() if ":" in t["title"] else t["title"]
        units[unit].append(t)

    unit_list = [{"name": n} for n in sorted(units.keys())]
    index_path = str(Path(folder) / "study.md")

    def _render_index(_conn: sqlite3.Connection) -> str:
        return render_study_index(unit_list)

    def _make_unit_render(n: str, ts: list[dict]):
        def _render(_conn: sqlite3.Connection) -> str:
            return render_study_unit(n, ts)
        return _render

    children = [
        SubPageConfig(
            key=f"study-{u['name']}",
            render=_make_unit_render(u["name"], units[u["name"]]),
            path=str(Path(folder) / "study" / f"{u['name']}.md"),
            state_dir=str(Path(state_dir) / "study"),
        )
        for u in unit_list
    ]

    return SubPageConfig(
        key="study",
        render=_render_index,
        path=index_path,
        state_dir=state_dir,
        subpages=children,
    )


def build_projects_configs(conn: sqlite3.Connection,
                           folder: str, state_dir: str) -> SubPageConfig:
    """Projects index + pit child + one child per project task."""
    rows = conn.execute(
        "SELECT id, title, status, next_step, due, "
        "last_session_summary, context_pointers, outcome_log "
        "FROM tasks WHERE category = 'project'"
    ).fetchall()
    tasks = [dict(r) for r in rows]

    proj_state = str(Path(state_dir) / "projects")

    def _make_proj_render(snap: dict):
        def _render(_conn: sqlite3.Connection) -> str:
            return render_project_page(snap)
        return _render

    children = [
        SubPageConfig(
            key="pit",
            render=render_pit,
            path=str(Path(folder) / "projects" / "pit.md"),
            state_dir=proj_state,
        )
    ] + [
        SubPageConfig(
            key=f"project-{t['title']}",
            render=_make_proj_render(t),
            path=str(Path(folder) / "projects" / f"{t['title']}.md"),
            state_dir=proj_state,
        )
        for t in tasks
    ]

    return SubPageConfig(
        key="projects",
        render=render_projects_index,
        path=str(Path(folder) / "projects.md"),
        state_dir=state_dir,
        subpages=children,
    )


# ---------------------------------------------------------------------------
# Entry points — config-driven (DESIGN L43-65, [subpages] in config.toml)
# ---------------------------------------------------------------------------

# Registry: known keys → builder(conn, folder, state_dir) returning a
# SubPageConfig. Folder-based views (study, projects) build their own
# children; the rest are single-file flat configs.
_REGISTRY: dict[str, Callable[[sqlite3.Connection, str, str], SubPageConfig]] = {
    "profile":    lambda c, f, s: SubPageConfig(
        "profile",   render_profile,   str(Path(f)/"profile.md"),   s),
    "milestone":  lambda c, f, s: SubPageConfig(
        "milestone", render_milestone, str(Path(f)/"milestone.md"), s,
        reconcile=reconcile_milestones),
    "diary":      lambda c, f, s: SubPageConfig(
        "diary",     render_diary,     str(Path(f)/"diary.md"),     s),
    "memes":      lambda c, f, s: SubPageConfig(
        "memes",     render_memes,     str(Path(f)/"memes.md"),     s),
    "stickers":   lambda c, f, s: SubPageConfig(
        "stickers",  render_stickers,  str(Path(f)/"stickers.md"),  s),
    "wallet":     lambda c, f, s: SubPageConfig(
        "wallet",    render_wallet,    str(Path(f)/"wallet.md"),    s),
    "goose":      lambda c, f, s: SubPageConfig(
        "goose",     render_goose,     str(Path(f)/"goose-bites.md"),     s),
    "cheatsheet": lambda c, f, s: SubPageConfig(
        "cheatsheet", render_cheatsheet, str(Path(f)/"cheatsheet.md"), s,
        read_only=True),
    "study":      lambda c, f, s: build_study_configs(c, f, s),
    "projects":   lambda c, f, s: build_projects_configs(c, f, s),
}

# Render order when [subpages] is absent from config — covers fresh installs
# and tests. Mirrors DESIGN L43-65 default order.
_DEFAULT_TOP = ["profile", "milestone", "diary", "memes",
                "stickers", "wallet", "goose"]
_DEFAULT_BOTTOM = ["study", "projects", "cheatsheet"]


def _subpages_cfg() -> dict:
    """Load [subpages] from config.toml, fall back to defaults. Never raises.

    Distinguishes missing key (use default) from empty list (honour empty).
    """
    try:
        cfg = _config.load().get("subpages") or {}
    except Exception:
        cfg = {}
    top    = cfg["top"]    if "top"    in cfg else _DEFAULT_TOP
    bottom = cfg["bottom"] if "bottom" in cfg else _DEFAULT_BOTTOM
    hidden = cfg["hidden"] if "hidden" in cfg else []
    return {"top": list(top), "bottom": list(bottom), "hidden": list(hidden)}


def build_all_configs(conn: sqlite3.Connection, *,
                      folder: str, state_dir: str,
                      db: str | None = None) -> list[SubPageConfig]:
    """Config-driven sub-page list (DESIGN L43-65).

    Order: top items, then bottom items. Unknown keys = warn + skip + alert.
    `hidden` keys still build (so md files stay current) but the dashboard
    Content list excludes them — gate happens at content_list().
    """
    sub_cfg = _subpages_cfg()
    out: list[SubPageConfig] = []
    seen: set[str] = set()
    for section in ("top", "bottom"):
        for key in sub_cfg[section]:
            if key in seen:
                continue
            seen.add(key)
            builder = _REGISTRY.get(key)
            if builder is None:
                repo.add_alert(
                    "warn", "db_pages",
                    f"unknown subpage key '{key}' in [subpages].{section}"
                    " — skipped (registry: " + ", ".join(sorted(_REGISTRY)) + ")",
                    source="subpages.py", db=db,
                )
                continue
            try:
                out.append(builder(conn, folder, state_dir))
            except Exception as e:
                repo.add_alert(
                    "warn", "db_pages",
                    f"subpage '{key}' build failed: {e}",
                    source="subpages.py", db=db,
                )
    return out


def content_list(*, folder: str | None = None) -> dict:
    """Return ordered subpage display info for dashboard `## Content`.

    Returns {"top": [(label, rel_path), ...], "bottom": [(label, rel_path), ...]}
    Hidden keys excluded. `folder` defaults to config.db_pages_path() so the
    dashboard can compute md links relative to its own path.
    """
    sub_cfg = _subpages_cfg()
    hidden = set(sub_cfg["hidden"])
    if folder is None:
        try:
            folder = _config.db_pages_path()
        except Exception:
            folder = "."
    base = Path(folder)
    out: dict[str, list[tuple[str, str]]] = {"top": [], "bottom": []}
    for section in ("top", "bottom"):
        for key in sub_cfg[section]:
            if key in hidden or key not in _REGISTRY:
                continue
            label = _DISPLAY.get(key, key.capitalize())
            filename = _FILENAME.get(key, f"{key}.md")
            out[section].append((label, str(base / filename)))
    return out


# Filename override for keys whose written file != "<key>.md".
# Historical: goose key writes to goose-bites.md (see _REGISTRY).
_FILENAME = {"goose": "goose-bites.md"}


# Display names for dashboard Content section. Falls back to key.capitalize().
_DISPLAY = {
    "profile":    "Profile",
    "milestone":  "Milestone",
    "diary":      "Diary",
    "memes":      "Memes",
    "stickers":   "Stickers",
    "wallet":     "Wallet",
    "goose":      "Goose-bites",
    "study":      "Study",
    "projects":   "Projects",
    "cheatsheet": "Cheatsheet",
}


def write_all_subpages(conn: sqlite3.Connection, *,
                       folder: str, state_dir: str,
                       db: str | None = None) -> None:
    """Render and write all sub-pages atomically."""
    for cfg in build_all_configs(conn, folder=folder, state_dir=state_dir, db=db):
        write_subpage(cfg, conn, db=db)
