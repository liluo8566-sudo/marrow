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

try:
    from .drift_sweep import CONFIG_BLACKLIST
except ImportError:
    CONFIG_BLACKLIST: frozenset[str] = frozenset({"wechat-claude-bridge"})

_CLAUDE_ROOT = Path.home() / ".claude"
_CONFIG_ROOT = Path.home() / ".config"

# Canonical render order for atlas section headers. Decoupled from
# drift_sweep.AUTHORIZED_ROOTS iteration order so atlas.md stays stable
# even if AUTHORIZED_ROOTS is reshuffled for unrelated reasons.
# Order: NY → Study → CC-Lab → .claude → .config → Toolkit.
ATLAS_ROOT_ORDER: list[Path] = [
    Path.home() / "Desktop" / "NY",
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Study",
    Path.home() / "CC-Lab",
    Path.home() / ".claude",
    Path.home() / ".config",
    Path.home() / "Toolkit",
]

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


def _section_header(root_path: str, row: dict | None = None) -> str:
    """## [basename/](file:///abs/path) + marker + 4 fields when row is given.

    Older renders showed the full shorthand (`## ~/Library/Mobile Documents/
    com~apple~CloudDocs/Study/`) which was unreadable and unclickable.
    The basename is enough to identify the root in context; the link lets
    the user jump to the folder. Encoded path handles spaces / `&` / CJK.

    Pass `row` (atlas row for the root path) to emit the marker + note /
    write / naming / depth fields under the heading; setting depth=0 on
    the root collapses the entire subtree on next reconcile.
    """
    p = Path(root_path).expanduser().resolve()
    name = p.name + "/"
    encoded = urllib.parse.quote(str(p), safe="/")
    header = f"## [{name}](file://{encoded})"
    if row is None:
        return header
    marker = f"<!-- id:{root_path} -->"
    note = row.get("note") or ""
    write = row.get("write_hint") or ""
    naming = row.get("naming_hint") or ""
    depth = row.get("depth") or 0
    return "\n".join([
        header,
        marker,
        f"- note: {note}",
        f"- write: {write}",
        f"- naming: {naming}",
        f"- depth: {depth}",
    ])


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
        # Bug 2 guard: skip md rows whose path is not under any
        # AUTHORIZED_ROOT — they're noise (stale imports, manual mistakes)
        # and must not get inserted/upserted into atlas.
        md_rows = [r for r in md_rows if _under_any_root(r["path"], roots)]
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

    # Always kick a sweep — handles both directions: depth bump (new stubs)
    # and depth shrink (retract deep orphans). Without this, a shrink would
    # have to wait for the 60s AtlasSweepLoop cycle to take effect.
    # User-written content (manual fields) is preserved by the sweep's
    # has_manual check; stub-only md_paths must NOT be protected, or a
    # depth shrink could never collapse leftover stub blocks.
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

    Stub-only rows out of range get retracted (deleted). Rows with manual
    fields (note/write_hint/naming_hint) are always spared.

    Respects EXCLUDE_DIRS_TREE and ~/.claude whitelist.
    Returns {"stubbed": N, "unstaled": N, "staled": N, "purged": N, "retracted": N}.
    """
    from . import drift_sweep
    roots = [r.expanduser().resolve() for r in drift_sweep.AUTHORIZED_ROOTS]
    counts = {"stubbed": 0, "unstaled": 0, "staled": 0, "out_of_root": 0}
    now = _NOW()

    # Bug 2 guard: purge any atlas row whose path is not under any
    # AUTHORIZED_ROOT. These leak in from old code paths, manual edits,
    # or imports. One-time + ongoing — runs every sweep so the table
    # self-heals if new strays appear.
    with conn:
        existing = [r[0] for r in conn.execute(
            "SELECT path FROM atlas"
        ).fetchall()]
        for p_str in existing:
            if not _under_any_root(p_str, roots):
                conn.execute("DELETE FROM atlas WHERE path=?", (p_str,))
                counts["out_of_root"] += 1

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
        # Purge rows for paths the user has explicitly blacklisted (CLAUDE
        # whitelist non-matches, or CONFIG_BLACKLIST hits). These slip in
        # from older sweep passes before the blacklist was applied; without
        # this, they'd live forever as stale rows.
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

        # Retract out-of-range stubs. In-range = some depth>0 atlas row S
        # covers it (path under S, rel_depth ≤ S.depth). When a root flips
        # 1→0 its former children lose coverage and retract.
        # Spare only: AUTHORIZED_ROOTS, depth>0, manual fields, or rows
        # with no atlas ancestor at all (unattached). md_paths is NOT a
        # protection signal — a stub-only row still listed in md is just
        # a leftover render from before the depth shrink; retract is the
        # whole point. Otherwise reconcile→sweep would never collapse,
        # since stale stub blocks re-insert themselves each tick before
        # the sweep can act.
        # Bug 1: AUTHORIZED_ROOT ancestor with depth=0 forces retract of
        # all stub-only descendants, even if some intermediate (non-root)
        # seed with depth>0 would otherwise mark them covered. A collapsed
        # root means "clean up everything under me regardless of stale
        # intermediate seeds".
        counts["retracted"] = 0
        root_strs = {str(r) for r in roots}
        all_rows_full = conn.execute(
            "SELECT path, depth, note, write_hint, naming_hint FROM atlas"
        ).fetchall()
        all_paths_depth = {r[0]: r[1] for r in all_rows_full}
        manual_fields = {r[0]: (r[2], r[3], r[4]) for r in all_rows_full}
        seed_pool = [(p, d) for p, d in all_paths_depth.items() if d > 0]
        atlas_paths_sorted = sorted(all_paths_depth, key=len, reverse=True)
        for p_str, p_depth in all_paths_depth.items():
            if p_str in root_strs:
                continue
            note, write_hint, naming_hint = manual_fields.get(
                p_str, (None, None, None))
            has_manual = any(v not in (None, "")
                             for v in (note, write_hint, naming_hint))
            if has_manual:
                continue
            # Bug 1 fix: if ANY AUTHORIZED_ROOT ancestor sits in atlas with
            # depth=0, force retract — overrides intermediate-seed coverage
            # AND retracts stub-only rows that still carry a stale depth>0.
            # Collapsing a root means "clean everything under me unless the
            # user pinned manual fields".
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
