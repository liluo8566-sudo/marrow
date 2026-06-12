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
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config as _config
from . import repo
from . import subpage_specs
from ._atomic import atomic_write as _atomic_write
from .atlas import atlas_sweep_fs, reconcile_atlas, seed_atlas_from_roots
from .inserter import InserterSpec, write_subpage_inserter
from .md_index import MdIndex
from .reconcile import reconcile_milestones
from .reconcile_inserter import (
    reconcile_memes,
    reconcile_profile,
    reconcile_diary,
    reconcile_stickers,
    reconcile_wallet,
)
from .subpages_render import (
    render_cheatsheet,
    render_diary,
    render_memes,
    render_milestone,
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
    """Render config for one sub-page or sub-page folder.

    Two write paths share this config:
    - inserter (Phase 3, Plan M Phase B): `inserter` set → block-level
      upsert via md_index, hand-edits preserved.
    - legacy full-render (cheatsheet read-only + children that haven't
      been ported): `render` produces the whole block, write_subpage
      atomic-writes the file.
    Exactly one of (inserter, render) must be set per config; the inserter
    path takes precedence when both are present.
    """
    key: str                          # marker key
    render: Callable[[sqlite3.Connection], str]  # full-block fallback render
    path: str                         # absolute path to the .md file
    state_dir: str                    # dir for sub-page state (reserved)
    read_only: bool = False           # always overwrite full file
    # md->DB reconcile callback. Runs BEFORE render so Lumi's md edits flow
    # back to DB and the freshly-rendered block reflects them. None = skip.
    reconcile: Callable[[sqlite3.Connection, "Path"], object] | None = None
    subpages: list["SubPageConfig"] = field(default_factory=list)
    inserter: InserterSpec | None = None


# ---------------------------------------------------------------------------
# Write (atomic)
# ---------------------------------------------------------------------------

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

    Two write paths:
    - inserter (cfg.inserter set): md-as-SoT block-level upsert. Reconcile
      still runs first so anchored md edits flow back to DB before the
      inserter computes which blocks need appending.
    - legacy full-render (no inserter): reconcile -> render -> atomic write
      of the marker block. Free-form text inside the rendered block is
      silently overwritten. Used by cheatsheet (read_only) + children.
    """
    path, key = cfg.path, cfg.key
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)

    # Run reconcile BEFORE the writer so the new render reflects Lumi's edits.
    if cfg.reconcile is not None and os.path.exists(path) and not cfg.read_only:
        try:
            cfg.reconcile(conn, Path(path))
        except Exception as e:
            repo.add_alert(
                "warn", "db_pages",
                f"subpage_reconcile_failed:{key}",
                source="subpages.py", db=db,
                message=f"{key} reconcile failed: {e}; falling through to render",
            )

    if cfg.inserter is not None:
        try:
            store = MdIndex(conn)
            write_subpage_inserter(cfg.inserter, conn, store)
        except Exception as e:
            repo.add_alert(
                "warn", "db_pages",
                f"subpage_inserter_failed:{key}",
                source="subpages.py", db=db,
                message=f"{key} inserter failed: {e}",
            )
    else:
        if not cfg.read_only:
            repo.add_alert(
                "warn", "db_pages",
                f"subpage_missing_inserter:{key}",
                source="subpages.py", db=db,
                message=f"{key} missing inserter — legacy full-render path bypasses SoT",
            )
        block = cfg.render(conn)
        existing = (Path(path).read_text(encoding="utf-8")
                    if os.path.exists(path) else "")
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
            read_only=True,
        )
        for u in unit_list
    ]

    return SubPageConfig(
        key="study",
        render=_render_index,
        path=index_path,
        state_dir=state_dir,
        subpages=children,
        inserter=subpage_specs.build_study_index_spec(folder),
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
            key=f"project-{t['title']}",
            render=_make_proj_render(t),
            path=str(Path(folder) / "projects" / f"{t['title']}.md"),
            state_dir=proj_state,
            read_only=True,
        )
        for t in tasks
    ]

    return SubPageConfig(
        key="projects",
        render=render_projects_index,
        path=str(Path(folder) / "projects.md"),
        state_dir=state_dir,
        subpages=children,
        inserter=subpage_specs.build_projects_index_spec(folder),
    )


# ---------------------------------------------------------------------------
# Entry points — config-driven (DESIGN L43-65, [subpages] in config.toml)
# ---------------------------------------------------------------------------

# Registry: known keys → builder(conn, folder, state_dir) returning a
# SubPageConfig. Plan M Phase B: per-subpage inserter specs wired here.
# Each entry pairs the legacy `render` (kept as fallback if inserter fails)
# with an `inserter` spec when the subpage has been flipped to md-as-SoT.
# Folder-based views (study, projects) build their own children.
def _flat_with_inserter(key: str, render, filename: str, spec_builder,
                        *, reconcile=None):
    """Helper: SubPageConfig with both the legacy render and an InserterSpec."""
    def _build(_c: sqlite3.Connection, f: str, s: str) -> SubPageConfig:
        return SubPageConfig(
            key=key,
            render=render,
            path=str(Path(f) / filename),
            state_dir=s,
            reconcile=reconcile,
            inserter=spec_builder(f),
        )
    return _build


def _build_atlas_config(conn: sqlite3.Connection,
                        folder: str, state_dir: str) -> SubPageConfig:
    """Atlas — runs seed + sweep before inserter writes.

    Seed: first call inserts root stubs (depth=1) if atlas table is empty.
    Sweep: atlas_sweep_fs runs before reconcile so new dirs are already
    stubbed when md->db reconcile runs, and stale flags are up-to-date.
    Reconcile (reconcile_atlas): md heading tree → db upsert/delete.
    Inserter (build_atlas_spec): db rows → md heading tree.
    """
    # Ensure root stubs (depth=1) exist every refresh — INSERT OR IGNORE,
    # safe to re-run; reconcile DELETE skips root rows so this is the
    # canonical place to re-establish missing roots.
    seed_atlas_from_roots(conn)

    # Sweep: stub new dirs, mark stale. Runs before reconcile so the md
    # reflects newly-discovered dirs on the same refresh pass.
    try:
        atlas_sweep_fs(conn)
    except Exception as e:
        repo.add_alert("warn", "atlas", "atlas_sweep_failed",
                       source="subpages.py",
                       message=f"sweep failed: {e}")

    return SubPageConfig(
        key="atlas",
        render=lambda _c: "",  # inserter is SoT; legacy path not used
        path=str(Path(folder) / "atlas.md"),
        state_dir=state_dir,
        reconcile=reconcile_atlas,
        inserter=subpage_specs.build_atlas_spec(folder),
    )


_REGISTRY: dict[str, Callable[[sqlite3.Connection, str, str], SubPageConfig]] = {
    "profile":    _flat_with_inserter(
        "profile", render_profile, "profile.md",
        subpage_specs.build_profile_spec,
        reconcile=reconcile_profile),
    "milestone":  _flat_with_inserter(
        "milestone", render_milestone, "milestone.md",
        subpage_specs.build_milestone_spec,
        reconcile=reconcile_milestones),
    "diary":      _flat_with_inserter(
        "diary", render_diary, "diary.md",
        subpage_specs.build_diary_spec,
        reconcile=reconcile_diary),
    "memes":      _flat_with_inserter(
        "memes", render_memes, "memes.md",
        subpage_specs.build_memes_spec,
        reconcile=reconcile_memes),
    "stickers":   _flat_with_inserter(
        "stickers", render_stickers, "stickers.md",
        subpage_specs.build_stickers_spec,
        reconcile=reconcile_stickers),
    "wallet":     _flat_with_inserter(
        "wallet", render_wallet, "wallet.md",
        subpage_specs.build_wallet_spec,
        reconcile=reconcile_wallet),
    "cheatsheet": lambda c, f, s: SubPageConfig(
        "cheatsheet", render_cheatsheet, str(Path(f) / "cheatsheet.md"), s,
        read_only=True),
    "study":      lambda c, f, s: build_study_configs(c, f, s),
    "projects":   lambda c, f, s: build_projects_configs(c, f, s),
    "atlas":      _build_atlas_config,
}

# Render order when [subpages] is absent from config — covers fresh installs
# and tests. Mirrors DESIGN L43-65 default order.
_DEFAULT_TOP = ["profile", "milestone", "diary", "memes",
                "stickers", "wallet"]
_DEFAULT_BOTTOM = ["study", "projects", "cheatsheet", "atlas"]


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
                    f"subpage_unknown_key:{key}",
                    source="subpages.py", db=db,
                    message=(f"unknown subpage key '{key}' in [subpages].{section}"
                             " — skipped (registry: " + ", ".join(sorted(_REGISTRY)) + ")"),
                )
                continue
            try:
                out.append(builder(conn, folder, state_dir))
            except Exception as e:
                repo.add_alert(
                    "warn", "db_pages",
                    f"subpage_build_failed:{key}",
                    source="subpages.py", db=db,
                    message=f"subpage '{key}' build failed: {e}",
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


_FILENAME: dict[str, str] = {}


# Display names for dashboard Content section. Falls back to key.capitalize().
_DISPLAY = {
    "profile":    "Profile",
    "milestone":  "Milestone",
    "diary":      "Diary",
    "memes":      "Memes",
    "stickers":   "Stickers",
    "wallet":     "Wallet",
    "study":      "Study",
    "projects":   "Projects",
    "cheatsheet": "Cheatsheet",
    "atlas":      "Atlas",
}


def write_all_subpages(conn: sqlite3.Connection, *,
                       folder: str, state_dir: str,
                       db: str | None = None) -> None:
    """Render and write all sub-pages atomically."""
    for cfg in build_all_configs(conn, folder=folder, state_dir=state_dir, db=db):
        write_subpage(cfg, conn, db=db)
