"""atlas — dir-tree subpage: parse, reconcile, render helpers, fs walk.

Public API:
- reconcile_atlas(conn, md_path) — md marker list -> db upsert/delete
- atlas_sweep_fs(conn) — depth-aware walk: stub new dirs, mark vanished stale
- rekey_paths(conn, ops) — migrate atlas rows on dir-mv
- _heading_level(path, root) — compute h-level (2-6) for a path under root
- _root_shorthand(root) — ~/path display form

Each atlas row = one directory. depth=0 means "don't auto-expand children".
stale=1 means fs walk could not find the path (never deleted; preserves fields).
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

# ~/.claude whitelisted top-level entries — only these are recurse-able.
# S2a (DriftWatcher) may later expose CLAUDE_WHITELIST; if it's there import
# it, otherwise fall back to this hard-coded set.
# TODO: dedupe after S2a merges and exposes drift_sweep.CLAUDE_WHITELIST.
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

_CLAUDE_ROOT = Path.home() / ".claude"

_NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # noqa: E731

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _root_shorthand(root: str | Path) -> str:
    """Absolute path → `~/relative/` display form."""
    p = Path(root).expanduser().resolve()
    try:
        rel = p.relative_to(Path.home())
        return f"~/{rel}/"
    except ValueError:
        return str(p) + "/"


def _heading_level(path: str | Path, root: str | Path) -> int:
    """Return heading level 2–6 for a path relative to root.

    Root itself → h2 (used for section headers, not row headings).
    First-level child → h3. Each additional depth +1, capped at h6.
    """
    p = Path(path).expanduser().resolve()
    r = Path(root).expanduser().resolve()
    try:
        rel = p.relative_to(r)
        depth = len(rel.parts)  # 1 for direct child, 2 for grandchild …
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


# ---------------------------------------------------------------------------
# Render helpers (used by build_atlas_spec)
# ---------------------------------------------------------------------------


def _render_atlas_row(r: dict, roots: list[Path]) -> str:
    """Layer-aware block for one atlas row; dir name itself is the open link.

    Layout depends on the row's depth relative to its owning root:
      layer 1 (direct child of root) -> H5 heading
        ##### [dirname/](file:///abs)
        <!-- id:/abs -->
        - note: ...
        - write: ...
        - naming: ...
        - depth: N
      layer 2+ (deeper) -> indented list item, 2 spaces per extra layer
          - [dirname/](file:///abs)
            <!-- id:/abs -->
            - note: ...
            - write: ...
            - naming: ...
            - depth: N

    Section header (## ~/root/) is emitted separately by the spec.
    Outline panel surfaces section + first-layer dirs; deeper levels
    flow as nested list items so the tree shape stays readable without
    hammering H6 / heading clutter.
    """
    path = r["path"]
    name = Path(path).name + "/"
    stale_sfx = " (stale)" if r.get("stale") else ""
    encoded = urllib.parse.quote(path, safe="/")
    marker = f"<!-- id:{path} -->"
    note = r.get("note") or ""
    write = r.get("write_hint") or ""
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
            f"##### [{name}](file://{encoded}){stale_sfx}",
            marker,
            f"- note: {note}",
            f"- write: {write}",
            f"- naming: {naming}",
            f"- depth: {depth}",
        ])
    ind = "  " * (layer - 1)
    body = "  " * (layer - 1) + "  "  # 2 extra spaces so bullets sit under the list item
    return "\n".join([
        f"{ind}- [{name}](file://{encoded}){stale_sfx}",
        f"{body}{marker}",
        f"{body}- note: {note}",
        f"{body}- write: {write}",
        f"{body}- naming: {naming}",
        f"{body}- depth: {depth}",
    ])


def _section_header(root_path: str) -> str:
    return f"## {_root_shorthand(root_path)}"


# ---------------------------------------------------------------------------
# Parser — marker-based, reads inline <!-- id:path --> anchors
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"<!--\s*id:(?P<path>[^>]+?)\s*-->")
_BULLET_RE = re.compile(
    r"^\s*-\s+(note|write|naming|depth)\s*:\s*(.*)$"
)
_STALE_RE = re.compile(r"\s*\(stale\)\s*$")


def _parse_atlas_md(md_text: str, roots: list[Path]) -> list[dict]:
    """Parse atlas marker list into row dicts.

    Scans for <!-- id:/abs/path --> markers; sub-bullets populate fields.
    `roots` kept in signature for back-compat but unused (path from marker).
    Each dict: {path, note, write_hint, naming_hint, depth}.
    """
    rows: list[dict] = []
    cur_row: dict | None = None

    def _flush() -> None:
        nonlocal cur_row
        if cur_row and cur_row.get("path"):
            rows.append(cur_row)
        cur_row = None

    for raw in md_text.splitlines():
        line = raw.rstrip()

        m = _ID_RE.search(line)
        if m:
            _flush()
            cur_row = {
                "path": m.group("path"),
                "note": None,
                "write_hint": None,
                "naming_hint": None,
                "depth": 0,
            }
            continue

        if cur_row is None:
            continue

        bm = _BULLET_RE.match(line)
        if bm:
            field, value = bm.group(1), bm.group(2).strip()
            if field == "note":
                cur_row["note"] = value or None
            elif field == "write":
                cur_row["write_hint"] = value or None
            elif field == "naming":
                cur_row["naming_hint"] = value or None
            elif field == "depth":
                try:
                    cur_row["depth"] = int(value)
                except (ValueError, TypeError):
                    cur_row["depth"] = 0

    _flush()
    return rows


# ---------------------------------------------------------------------------
# rekey_paths — migrate atlas rows on dir-mv
# ---------------------------------------------------------------------------

def rekey_paths(conn: sqlite3.Connection,
                ops: list[tuple[str, str]]) -> int:
    """For each (src, dest), migrate atlas row from src → dest.

    Preserves note/write_hint/naming_hint/depth. If dest already exists,
    delete the src row (sweep reconciles on next pass).
    Returns number of rows updated.
    """
    now = _NOW()
    updated = 0
    with conn:
        for src, dest in ops:
            row = conn.execute(
                "SELECT note, write_hint, naming_hint, depth FROM atlas WHERE path=?",
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


def reconcile_atlas(conn: sqlite3.Connection, md_path: Path) -> int:
    """Parse atlas.md heading tree → upsert into atlas table.

    Paths in md → upsert (preserves stale flag from db, updates fields).
    Paths in db NOT in md → DELETE (user explicitly removed the row).
    Returns number of rows changed (insert + update + delete).
    """
    from . import drift_sweep
    roots = [r.expanduser().resolve() for r in drift_sweep.AUTHORIZED_ROOTS]

    md_path = Path(md_path)
    if not md_path.exists():
        return 0

    text = md_path.read_text(encoding="utf-8")
    md_rows = _parse_atlas_md(text, roots)

    now = _NOW()
    changed = 0

    with conn:
        md_paths = {r["path"] for r in md_rows}

        # Upsert rows present in md
        for r in md_rows:
            conn.execute(
                "INSERT INTO atlas (path, note, write_hint, naming_hint,"
                " depth, stale, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 0, ?)"
                " ON CONFLICT(path) DO UPDATE SET"
                "  note=excluded.note,"
                "  write_hint=excluded.write_hint,"
                "  naming_hint=excluded.naming_hint,"
                "  depth=excluded.depth,"
                "  updated_at=excluded.updated_at",
                (r["path"], r.get("note"), r.get("write_hint"),
                 r.get("naming_hint"), r.get("depth", 0), now),
            )
            changed += 1

        # Delete db rows not present in md. Two protections:
        # 1. Root rows (## ~/root/ are section markers, parser intentionally
        #    skips them — would otherwise be deleted on every reconcile and
        #    depth-aware sweep would lose its seeds).
        # 2. Stub-only rows (no manual hint fields) — these are produced by
        #    atlas_sweep_fs and not yet rendered to md. Without this guard,
        #    sweep→reconcile→render in a single refresh would delete every
        #    new stub before render saw it. User-modified rows (any hint
        #    non-null) still get deleted when removed from md.
        root_strs = {str(r) for r in roots}
        db_rows = conn.execute(
            "SELECT path, note, write_hint, naming_hint FROM atlas"
        ).fetchall()
        for row in db_rows:
            path = row[0]
            if path in md_paths or path in root_strs:
                continue
            has_manual = any(v not in (None, "") for v in (row[1], row[2], row[3]))
            if not has_manual:
                continue
            conn.execute("DELETE FROM atlas WHERE path=?", (path,))
            changed += 1

    # When the user just bumped any row's depth above 0, kick a fs walk
    # immediately so new subdir stubs appear in the next render tick.
    # Otherwise they'd have to wait for the 60s AtlasSweepLoop cadence.
    if any((r.get("depth") or 0) > 0 for r in md_rows):
        try:
            atlas_sweep_fs(conn)
        except Exception:  # noqa: BLE001
            pass

    return changed


# ---------------------------------------------------------------------------
# fs walk
# ---------------------------------------------------------------------------

def _is_claude_allowed(entry: Path) -> bool:
    """Check if a path directly under ~/.claude is whitelisted for recursion."""
    return entry.name in CLAUDE_WHITELIST


def atlas_sweep_fs(conn: sqlite3.Connection) -> dict[str, int]:
    """Depth-aware fs walk: stub new dirs, mark vanished dirs stale.

    For each atlas row with depth > 0:
    - Walk subdirs up to that depth.
    - New subdir not in atlas → INSERT stub (depth=0, stale=0).
    - Existing row found → clear stale if it was stale.
    For each atlas row under a walked root:
    - Not found in walk results → set stale=1 (NEVER delete).

    Respects EXCLUDE_DIRS_TREE and ~/.claude whitelist.
    Returns {"stubbed": N, "unstaled": N, "staled": N}.
    """
    from . import drift_sweep
    roots = [r.expanduser().resolve() for r in drift_sweep.AUTHORIZED_ROOTS]
    counts = {"stubbed": 0, "unstaled": 0, "staled": 0}
    now = _NOW()

    # Load all rows with depth > 0 — these are the "expand" seeds.
    seed_rows = [
        dict(r) for r in conn.execute(
            "SELECT path, depth FROM atlas WHERE depth > 0"
        ).fetchall()
    ]

    # Collect all subdir paths found in fs for each seed.
    found_paths: set[str] = set()  # absolute path strings found during walk

    for row in seed_rows:
        seed_path = Path(row["path"])
        max_depth = row["depth"]
        if not seed_path.exists() or not seed_path.is_dir():
            continue
        _walk_collect(seed_path, max_depth, found_paths)

    # Collect all atlas paths that live UNDER any seed (children to check stale)
    seed_path_strs = {row["path"] for row in seed_rows}

    # Build set of all atlas paths that are under one of the seeds
    all_rows = {
        row[0]: row[1]
        for row in conn.execute("SELECT path, stale FROM atlas").fetchall()
    }

    # Paths to check stale = atlas rows that are NOT seeds themselves but
    # could be children of a seed.
    children_to_check: set[str] = set()
    for p in all_rows:
        if p in seed_path_strs:
            continue  # seeds themselves — stale via caller; not auto-managed
        # Is this path a child of any seed?
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
        # Stub new dirs
        for p_str in found_paths:
            if p_str not in all_rows:
                conn.execute(
                    "INSERT OR IGNORE INTO atlas"
                    " (path, note, write_hint, naming_hint, depth, stale, updated_at)"
                    " VALUES (?, '', NULL, NULL, 0, 0, ?)",
                    (p_str, now),
                )
                counts["stubbed"] += 1
            elif all_rows[p_str] == 1:
                # Was stale — it's back
                conn.execute(
                    "UPDATE atlas SET stale=0, updated_at=? WHERE path=?",
                    (now, p_str),
                )
                counts["unstaled"] += 1

        # Mark stale: children in atlas that weren't found in walk
        for p_str in children_to_check:
            if p_str not in found_paths and all_rows.get(p_str, 0) == 0:
                conn.execute(
                    "UPDATE atlas SET stale=1, updated_at=? WHERE path=?",
                    (now, p_str),
                )
                counts["staled"] += 1

        # Retract: when a seed's depth shrinks, stub-only descendants whose
        # distance from their nearest-ancestor seed exceeds that seed's depth
        # should disappear. User-edited rows (any manual field) survive — the
        # user invested in them, so they live on even out-of-range.
        # `counts["retracted"]` counts deleted rows for visibility.
        counts["retracted"] = 0
        seed_paths_sorted = sorted(seed_path_strs, key=len, reverse=True)
        manual_rows = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT path, note, write_hint, naming_hint FROM atlas"
            ).fetchall()
        }
        seed_depth_map = {row["path"]: row["depth"] for row in seed_rows}
        for p_str in children_to_check:
            nearest = None
            for sp in seed_paths_sorted:
                if p_str.startswith(sp + os.sep):
                    nearest = sp
                    break
            if nearest is None:
                continue
            try:
                rel = Path(p_str).relative_to(Path(nearest))
                rel_depth = len(rel.parts)
            except ValueError:
                continue
            seed_max = seed_depth_map.get(nearest, 0)
            if rel_depth <= seed_max:
                continue
            note, write_hint, naming_hint = manual_rows.get(
                p_str, (None, None, None))
            has_manual = any(
                v not in (None, "") for v in (note, write_hint, naming_hint)
            )
            if has_manual:
                continue
            conn.execute("DELETE FROM atlas WHERE path=?", (p_str,))
            counts["retracted"] += 1

    return counts


def _walk_collect(root: Path, max_depth: int, found: set[str],
                  _current_depth: int = 0) -> None:
    """Recursively collect direct subdir paths up to max_depth.

    Respects EXCLUDE_DIRS_TREE. For ~/.claude root, only recurses into
    CLAUDE_WHITELIST entries.
    """
    if _current_depth >= max_depth:
        return
    try:
        entries = sorted(root.iterdir())
    except (PermissionError, OSError):
        return

    is_claude = root.resolve() == _CLAUDE_ROOT.resolve()

    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name in EXCLUDE_DIRS_TREE:
            continue
        if is_claude and not _is_claude_allowed(entry):
            continue
        found.add(str(entry.resolve()))
        _walk_collect(entry, max_depth, found, _current_depth + 1)


# ---------------------------------------------------------------------------
# Seed after migration
# ---------------------------------------------------------------------------

def seed_atlas_from_roots(conn: sqlite3.Connection) -> int:
    """Insert one stub row per AUTHORIZED_ROOTS entry with depth=1.

    Called once after v12 migration (or first `mw refresh atlas`).
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
                " (path, note, write_hint, naming_hint, depth, stale, updated_at)"
                " VALUES (?, NULL, NULL, NULL, 1, 0, ?)",
                (str(p), now),
            )
            if conn.execute(
                "SELECT changes()"
            ).fetchone()[0]:
                inserted += 1
    return inserted
