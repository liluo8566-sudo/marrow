"""atlas — dir-tree subpage: parse, reconcile, render helpers, fs walk.

Public API:
- reconcile_atlas(conn, md_path) — md marker list -> db upsert/delete
- atlas_sweep_fs(conn) — depth-aware walk: stub new dirs, delete vanished
- rekey_paths(conn, ops) — migrate atlas rows on dir-mv
- lookup_by_prefix(conn, prefix) — path-component prefix query
- resolve_naming(conn, path, roots) — naming guidance with P-walk
- _heading_level(path, root) — compute h-level (2-6) for a path under root
- _root_shorthand(root) — ~/path display form

Each atlas row = one directory. depth=0 means do not auto-expand children.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

try:
    from .drift_sweep import CLAUDE_WHITELIST  # type: ignore[attr-defined]
except ImportError:
    CLAUDE_WHITELIST: frozenset[str] = frozenset({
        "CLAUDE.md", "rules", "commands", "skills",
        "agents", "output-styles", "hooks", "keybindings.json",
        "settings.json",
    })

try:
    from .drift_sweep import EXCLUDE_DIRS_TREE
except ImportError:
    EXCLUDE_DIRS_TREE: set[str] = {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
    }

try:
    from .drift_sweep import CONFIG_BLACKLIST
except ImportError:
    CONFIG_BLACKLIST: frozenset[str] = frozenset({"wechat-claude-bridge"})

_CLAUDE_ROOT = Path.home() / ".claude"
_CONFIG_ROOT = Path.home() / ".config"

# Canonical render order for atlas section headers. Decoupled from
# drift_sweep.AUTHORIZED_ROOTS iteration order so atlas.md stays stable
# even if AUTHORIZED_ROOTS is reshuffled for unrelated reasons.
# Order: NY → Study → CC-Lab → .claude → .config.
ATLAS_ROOT_ORDER: list[Path] = [
    Path.home() / "Desktop" / "NY",
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Study",
    Path.home() / "CC-Lab",
    Path.home() / ".claude",
    Path.home() / ".config",
]

_NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # noqa: E731

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _root_shorthand(root: str | Path) -> str:
    """Absolute path -> `~/relative/` display form."""
    p = Path(root).expanduser().resolve()
    try:
        rel = p.relative_to(Path.home())
        return f"~/{rel}/"
    except ValueError:
        return str(p) + "/"


def _heading_level(path: str | Path, root: str | Path) -> int:
    """Return heading level 2-6 for a path relative to root.

    Root itself -> h2. First-level child -> h3. Each additional depth +1, capped at h6.
    """
    p = Path(path).expanduser().resolve()
    r = Path(root).expanduser().resolve()
    try:
        rel = p.relative_to(r)
        depth = len(rel.parts)
    except ValueError:
        return 6
    return min(2 + depth, 6)


def _root_of(path: str | Path, roots: list[Path]) -> Path | None:
    """Return the AUTHORIZED_ROOT that is an ancestor of path, or None."""
    p = Path(path).expanduser().resolve()
    for root in roots:
        r = root.expanduser().resolve()
        try:
            p.relative_to(r)
            return r
        except ValueError:
            continue
    return None


def _under_any_root(path: str | Path, roots: list[Path]) -> bool:
    """Return True if path is the same as or under any of the given roots."""
    p = Path(path).expanduser().resolve()
    for root in roots:
        r = root.expanduser().resolve()
        if p == r:
            return True
        try:
            p.relative_to(r)
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Naming resolution
# ---------------------------------------------------------------------------


def resolve_naming(conn: sqlite3.Connection, path: str,
                   roots: list[Path]) -> str:
    """Return naming guidance string for a path.

    Empty/None -> mimic fallback message.
    'P' or 'p' -> walk up ancestors until finding a non-P, non-empty naming_hint.
    Anything else -> return verbatim.
    """
    _MIMIC = "(empty -> ls siblings for pattern)"
    _P = {"p", "P"}

    def _fetch_naming(p: str) -> str | None:
        row = conn.execute(
            "SELECT naming_hint FROM atlas WHERE path=?", (p,)
        ).fetchone()
        return row["naming_hint"] if row else None

    hint = _fetch_naming(path)
    if not hint:
        return _MIMIC
    if hint not in _P:
        return hint

    # P-walk: traverse ancestors
    current = Path(path)
    visited: set[str] = {path}
    for ancestor in current.parents:
        s = str(ancestor)
        if s in visited:
            break
        visited.add(s)
        h = _fetch_naming(s)
        if h and h not in _P:
            return h
        if h is None:
            # Check if we've left the authorized roots
            root = _root_of(s, roots)
            if root is None:
                break
    return _MIMIC


# ---------------------------------------------------------------------------
# Render helpers (used by build_atlas_spec)
# ---------------------------------------------------------------------------


def _render_atlas_row(r: dict, roots: list[Path]) -> str:
    """Layer-aware block for one atlas row.

    Layout:
      layer 1 (direct child of root) -> H5 heading [d=N] suffix
        ##### [dirname/](file://...) [d=N]
        <!-- id:/abs/path -->
        - Description: <value>
        - Naming: <value>
      layer 2+ (deeper) -> indented list item
          - [dirname/](file://...) [d=N]
            <!-- id:/abs/path -->
            - Description: <value>
            - Naming: <value>
    """
    path = r["path"]
    name = Path(path).name + "/"
    encoded = urllib.parse.quote(path, safe="/")
    marker = f"<!-- id:{path} -->"
    description = r.get("description") or ""
    naming = r.get("naming_hint") or ""
    depth = r.get("depth") or 0

    root = _root_of(path, roots)
    if root is not None:
        try:
            rel_parts = Path(path).resolve().relative_to(root.resolve()).parts
            layer = len(rel_parts)
        except ValueError:
            layer = 1
    else:
        layer = 1

    if layer <= 1:
        return "\n".join([
            f"##### [{name}](file://{encoded}) [{depth}]",
            marker,
            f"- Description: {description}",
            f"- Naming: {naming}",
        ])
    ind = "  " * (layer - 1)
    body = "  " * (layer - 1) + "  "
    return "\n".join([
        f"{ind}- [{name}](file://{encoded}) [{depth}]",
        f"{body}{marker}",
        f"{body}- Description: {description}",
        f"{body}- Naming: {naming}",
    ])


def _section_header(root_path: str, row: dict | None = None) -> str:
    """## [basename/](file:///abs/path) + marker + description/naming when row given."""
    p = Path(root_path).expanduser().resolve()
    name = p.name + "/"
    encoded = urllib.parse.quote(str(p), safe="/")
    depth = (row.get("depth") or 0) if row else 0
    header = f"## [{name}](file://{encoded}) [{depth}]"
    if row is None:
        return header
    marker = f"<!-- id:{root_path} -->"
    description = row.get("description") or ""
    naming = row.get("naming_hint") or ""
    return "\n".join([
        header,
        marker,
        f"- Description: {description}",
        f"- Naming: {naming}",
    ])


# ---------------------------------------------------------------------------
# Parser — marker-based, reads inline <!-- id:path --> anchors
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"<!--\s*id:(?P<path>[^>]+?)\s*-->")
_BULLET_RE = re.compile(
    r"^\s*-\s+(description|naming)\s*:\s*(.*)$",
    re.IGNORECASE,
)
_DEPTH_RE = re.compile(r"\s\[(\d+)\]\s*$")


def _parse_atlas_md(md_text: str, roots: list[Path]) -> list[dict]:
    """Parse atlas marker list into row dicts.

    Scans for <!-- id:/abs/path --> markers; sub-bullets populate fields.
    Depth extracted from heading line via [d=N] suffix.
    Each dict: {path, description, naming_hint, depth}.
    """
    rows: list[dict] = []
    cur_row: dict | None = None
    last_heading: str | None = None

    def _flush() -> None:
        nonlocal cur_row
        if cur_row and cur_row.get("path"):
            rows.append(cur_row)
        cur_row = None

    for raw in md_text.splitlines():
        line = raw.rstrip()

        # Track the most recent heading or list-item line with [d=N] so we can
        # read depth when the id marker appears on the very next line.
        stripped = line.lstrip()
        if stripped.startswith("#") or (stripped.startswith("-") and "[d=" in line):
            last_heading = line

        m = _ID_RE.search(line)
        if m:
            _flush()
            depth = 0
            if last_heading:
                dm = _DEPTH_RE.search(last_heading)
                if dm:
                    try:
                        depth = int(dm.group(1))
                    except (ValueError, TypeError):
                        depth = 0
            cur_row = {
                "path": m.group("path"),
                "description": None,
                "naming_hint": None,
                "depth": depth,
            }
            continue

        if cur_row is None:
            continue

        bm = _BULLET_RE.match(line)
        if bm:
            field, value = bm.group(1).lower(), bm.group(2).strip()
            if field == "description":
                cur_row["description"] = value or None
            elif field == "naming":
                cur_row["naming_hint"] = value or None

    _flush()
    return rows


# ---------------------------------------------------------------------------
# rekey_paths — migrate atlas rows on dir-mv
# ---------------------------------------------------------------------------

def rekey_paths(conn: sqlite3.Connection,
                ops: list[tuple[str, str]]) -> int:
    """For each (src, dest), migrate atlas row from src -> dest.

    Preserves description/naming_hint/depth. If dest already exists,
    delete the src row (sweep reconciles on next pass).
    Returns number of rows updated.
    """
    now = _NOW()
    updated = 0
    with conn:
        for src, dest in ops:
            row = conn.execute(
                "SELECT description, naming_hint, depth FROM atlas WHERE path=?",
                (src,),
            ).fetchone()
            if row is None:
                continue
            dest_exists = conn.execute(
                "SELECT 1 FROM atlas WHERE path=?", (dest,)
            ).fetchone()
            if dest_exists:
                conn.execute("DELETE FROM atlas WHERE path=?", (src,))
            else:
                conn.execute(
                    "UPDATE atlas SET path=?, updated_at=? WHERE path=?",
                    (dest, now, src),
                )
                updated += 1
    return updated


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


def reconcile_atlas(conn: sqlite3.Connection, md_path: Path):
    """Parse atlas.md heading tree -> upsert into atlas table.

    Paths in md -> upsert (updates fields).
    Paths in db NOT in md -> DELETE (user explicitly removed the row).
    Returns ReconcileReport.
    """
    from .reconcile import ReconcileReport
    from . import drift_sweep
    roots = [r.expanduser().resolve() for r in drift_sweep.AUTHORIZED_ROOTS]

    rpt = ReconcileReport()
    md_path = Path(md_path)
    if not md_path.exists():
        return rpt

    text = md_path.read_text(encoding="utf-8")
    md_rows = _parse_atlas_md(text, roots)

    md_mtime_iso = None
    try:
        md_mtime_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(md_path.stat().st_mtime)
        )
    except OSError:
        pass

    now = _NOW()

    with conn:
        md_rows = [r for r in md_rows if _under_any_root(r["path"], roots)]
        md_paths = {r["path"] for r in md_rows}

        for r in md_rows:
            new_desc = r.get("description")
            new_naming = r.get("naming_hint")
            new_depth = r.get("depth", 0)
            existing = conn.execute(
                "SELECT description, naming_hint, depth, updated_at FROM atlas WHERE path=?",
                (r["path"],),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO atlas (path, description, naming_hint, depth, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (r["path"], new_desc, new_naming, new_depth, now),
                )
                rpt.inserted += 1
                continue
            if (existing["description"] == new_desc
                    and existing["naming_hint"] == new_naming
                    and existing["depth"] == new_depth):
                rpt.unchanged += 1
                continue
            if md_mtime_iso and (existing["updated_at"] or "") > md_mtime_iso:
                rpt.unchanged += 1
                continue
            conn.execute(
                "UPDATE atlas SET description=?, naming_hint=?, depth=?, updated_at=?"
                " WHERE path=?",
                (new_desc, new_naming, new_depth, now, r["path"]),
            )
            rpt.updated += 1

        root_strs = {str(r) for r in roots}
        db_rows = conn.execute(
            "SELECT path, description, naming_hint FROM atlas"
        ).fetchall()
        for row in db_rows:
            path = row[0]
            if path in md_paths or path in root_strs:
                continue
            has_manual = any(v not in (None, "") for v in (row[1], row[2]))
            if not has_manual:
                continue
            row_full = conn.execute(
                "SELECT updated_at FROM atlas WHERE path=?", (path,)
            ).fetchone()
            if md_mtime_iso and row_full and (row_full["updated_at"] or "") > md_mtime_iso:
                continue
            conn.execute("DELETE FROM atlas WHERE path=?", (path,))
            rpt.deleted += 1

    try:
        atlas_sweep_fs(conn)
    except Exception:  # noqa: BLE001
        pass

    return rpt


# ---------------------------------------------------------------------------
# fs walk
# ---------------------------------------------------------------------------

def _is_claude_allowed(entry: Path) -> bool:
    return entry.name in CLAUDE_WHITELIST


def atlas_sweep_fs(conn: sqlite3.Connection) -> dict[str, int]:
    """Depth-aware fs walk: stub new dirs, delete vanished dirs.

    For each atlas row with depth > 0:
    - Walk subdirs up to that depth.
    - New subdir not in atlas -> INSERT stub (depth=0).
    - Vanished dir -> DELETE FROM atlas (no more stale flag).

    Respects EXCLUDE_DIRS_TREE and ~/.claude whitelist.
    Returns {"stubbed": N, "deleted": N, "purged": N, "retracted": N}.
    """
    from . import drift_sweep
    roots = [r.expanduser().resolve() for r in drift_sweep.AUTHORIZED_ROOTS]
    counts = {"stubbed": 0, "deleted": 0, "out_of_root": 0}
    now = _NOW()

    # Bug 2 guard: purge any atlas row whose path is not under any
    # AUTHORIZED_ROOT. One-time + ongoing self-heal.
    with conn:
        existing = [r[0] for r in conn.execute(
            "SELECT path FROM atlas"
        ).fetchall()]
        for p_str in existing:
            if not _under_any_root(p_str, roots):
                conn.execute("DELETE FROM atlas WHERE path=?", (p_str,))
                counts["out_of_root"] += 1

    seed_rows = [
        dict(r) for r in conn.execute(
            "SELECT path, depth FROM atlas WHERE depth > 0"
        ).fetchall()
    ]

    found_paths: set[str] = set()

    for row in seed_rows:
        seed_path = Path(row["path"])
        max_depth = row["depth"]
        if not seed_path.exists() or not seed_path.is_dir():
            continue
        _walk_collect(seed_path, max_depth, found_paths)

    seed_path_strs = {row["path"] for row in seed_rows}

    all_rows = {
        row[0]: row[1]
        for row in conn.execute("SELECT path, depth FROM atlas").fetchall()
    }

    children_to_check: set[str] = set()
    for p in all_rows:
        if p in seed_path_strs:
            continue
        pp = Path(p)
        for row in seed_rows:
            seed = Path(row["path"])
            try:
                pp.relative_to(seed)
                children_to_check.add(p)
                break
            except ValueError:
                continue

    with conn:
        counts["purged"] = 0
        for p_str in list(all_rows):
            p = Path(p_str)
            try:
                rel_claude = p.resolve().relative_to(_CLAUDE_ROOT.resolve())
                if rel_claude.parts and rel_claude.parts[0] not in CLAUDE_WHITELIST:
                    conn.execute("DELETE FROM atlas WHERE path=?", (p_str,))
                    counts["purged"] += 1
                    all_rows.pop(p_str, None)
                    continue
            except ValueError:
                pass
            try:
                rel_cfg = p.resolve().relative_to(_CONFIG_ROOT.resolve())
                if rel_cfg.parts and rel_cfg.parts[0] in CONFIG_BLACKLIST:
                    conn.execute("DELETE FROM atlas WHERE path=?", (p_str,))
                    counts["purged"] += 1
                    all_rows.pop(p_str, None)
                    continue
            except ValueError:
                pass

        for p_str in found_paths:
            if p_str not in all_rows:
                conn.execute(
                    "INSERT OR IGNORE INTO atlas"
                    " (path, description, naming_hint, depth, updated_at)"
                    " VALUES (?, NULL, NULL, 0, ?)",
                    (p_str, now),
                )
                counts["stubbed"] += 1

        # Delete vanished children (previously: mark stale=1)
        for p_str in children_to_check:
            if p_str not in found_paths:
                row_info = conn.execute(
                    "SELECT description, naming_hint FROM atlas WHERE path=?",
                    (p_str,),
                ).fetchone()
                if row_info is None:
                    continue
                has_manual = any(
                    v not in (None, "") for v in (row_info[0], row_info[1])
                )
                if not has_manual:
                    conn.execute("DELETE FROM atlas WHERE path=?", (p_str,))
                    counts["deleted"] += 1

        # Retract out-of-range stubs. In-range = some depth>0 row covers
        # it (rel_depth ≤ seed.depth). Bug 1: AUTHORIZED_ROOT depth=0
        # forces retract of all stub-only descendants.

        counts["retracted"] = 0
        root_strs = {str(r) for r in roots}
        all_rows_full = conn.execute(
            "SELECT path, depth, description, naming_hint FROM atlas"
        ).fetchall()
        all_paths_depth = {r[0]: r[1] for r in all_rows_full}
        manual_fields = {r[0]: (r[2], r[3]) for r in all_rows_full}
        seed_pool = [(p, d) for p, d in all_paths_depth.items() if d > 0]
        atlas_paths_sorted = sorted(all_paths_depth, key=len, reverse=True)
        for p_str, p_depth in all_paths_depth.items():
            if p_str in root_strs:
                continue
            desc, naming = manual_fields.get(p_str, (None, None))
            has_manual = any(v not in (None, "") for v in (desc, naming))
            if has_manual:
                continue
            # Bug 1: collapsed AUTHORIZED_ROOT forces retract of descendants.
            collapsed_root_ancestor = any(
                rs != p_str
                and p_str.startswith(rs + os.sep)
                and all_paths_depth.get(rs) == 0
                for rs in root_strs
            )
            if collapsed_root_ancestor:
                conn.execute("DELETE FROM atlas WHERE path=?", (p_str,))
                counts["retracted"] += 1
                continue
            if p_depth > 0:
                continue
            covered = False
            for sp, sd in seed_pool:
                if not p_str.startswith(sp + os.sep):
                    continue
                try:
                    rel = Path(p_str).relative_to(Path(sp))
                    if len(rel.parts) <= sd:
                        covered = True
                        break
                except ValueError:
                    continue
            if covered:
                continue
            has_ancestor = any(
                ap != p_str and p_str.startswith(ap + os.sep)
                for ap in atlas_paths_sorted
            )
            if not has_ancestor:
                continue
            conn.execute("DELETE FROM atlas WHERE path=?", (p_str,))
            counts["retracted"] += 1

    return counts


def _walk_collect(root: Path, max_depth: int, found: set[str],
                  _current_depth: int = 0) -> None:
    """Recursively collect direct subdir paths up to max_depth."""
    if _current_depth >= max_depth:
        return
    try:
        entries = sorted(root.iterdir())
    except (PermissionError, OSError):
        return

    is_claude = root.resolve() == _CLAUDE_ROOT.resolve()
    is_config = root.resolve() == _CONFIG_ROOT.resolve()

    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name in EXCLUDE_DIRS_TREE:
            continue
        if is_claude and not _is_claude_allowed(entry):
            continue
        if is_config and entry.name in CONFIG_BLACKLIST:
            continue
        found.add(str(entry.resolve()))
        _walk_collect(entry, max_depth, found, _current_depth + 1)


# ---------------------------------------------------------------------------
# Seed after migration
# ---------------------------------------------------------------------------

def seed_atlas_from_roots(conn: sqlite3.Connection) -> int:
    """Insert one stub row per AUTHORIZED_ROOTS entry with depth=1.

    Called once after v13 migration (or first `mw refresh atlas`).
    Idempotent — INSERT OR IGNORE.
    """
    from . import drift_sweep

    now = _NOW()
    inserted = 0
    with conn:
        for root in drift_sweep.AUTHORIZED_ROOTS:
            p = root.expanduser().resolve()
            conn.execute(
                "INSERT OR IGNORE INTO atlas"
                " (path, description, naming_hint, depth, updated_at)"
                " VALUES (?, NULL, NULL, 1, ?)",
                (str(p), now),
            )
            if conn.execute(
                "SELECT changes()"
            ).fetchone()[0]:
                inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Prefix lookup
# ---------------------------------------------------------------------------

def lookup_by_prefix(conn: sqlite3.Connection, prefix: str) -> list[dict]:
    """Path-component prefix query. Matches path == prefix OR path starts with prefix + '/'.

    Returns rows as dicts with keys path, description, naming_hint, depth.
    """
    prefix = str(Path(prefix).expanduser().resolve())
    rows = conn.execute(
        "SELECT path, description, naming_hint, depth FROM atlas"
        " WHERE path = ? OR path LIKE ?"
        " ORDER BY path",
        (prefix, prefix + "/%"),
    ).fetchall()
    return [dict(r) for r in rows]
