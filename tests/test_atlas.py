"""Atlas subpage tests — schema, spec, reconcile, render, fs walk.

All tests use tmp_path + init_db; never touch ~/.config/marrow/ or the real fs
beyond controlled tmp dirs.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from marrow import storage, subpage_specs
from marrow.atlas import (
    _heading_level,
    _parse_atlas_md,
    _render_atlas_row,
    _root_shorthand,
    atlas_sweep_fs,
    reconcile_atlas,
    rekey_paths,
    seed_atlas_from_roots,
)
from marrow.inserter import write_subpage_inserter
from marrow.md_index import MdIndex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    db_path = str(tmp_path / "t.db")
    c = storage.init_db(db_path)
    yield c
    c.close()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _insert_row(conn, path, note=None, write_hint=None, naming_hint=None,
                depth=0, stale=0):
    conn.execute(
        "INSERT OR REPLACE INTO atlas"
        " (path, note, write_hint, naming_hint, depth, stale, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (path, note, write_hint, naming_hint, depth, stale, _now()),
    )
    conn.commit()


def _marker_md(path: str, note="", write="", naming="", depth=0) -> str:
    """Build a single marker-format atlas block for one path."""
    return (
        f"### [{Path(path).name}/](file://{path})\n"
        f"<!-- id:{path} -->\n"
        f"- note: {note}\n"
        f"- write: {write}\n"
        f"- naming: {naming}\n"
        f"- depth: {depth}\n"
    )


# ---------------------------------------------------------------------------
# 1. Schema migration
# ---------------------------------------------------------------------------

def test_migration_creates_atlas_table(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "atlas" in tables


def test_atlas_schema_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(atlas)")}
    assert cols == {"path", "note", "write_hint", "naming_hint",
                    "depth", "stale", "updated_at"}


def test_atlas_path_is_primary_key(conn):
    info = {r["name"]: r for r in conn.execute("PRAGMA table_info(atlas)")}
    assert info["path"]["pk"] == 1


def test_atlas_depth_default_0(conn):
    conn.execute(
        "INSERT INTO atlas (path, updated_at) VALUES ('/tmp/x', ?)", (_now(),)
    )
    conn.commit()
    row = conn.execute("SELECT depth, stale FROM atlas WHERE path='/tmp/x'").fetchone()
    assert row["depth"] == 0
    assert row["stale"] == 0


def test_schema_version_12(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12


# ---------------------------------------------------------------------------
# 2. build_atlas_spec returns valid InserterSpec
# ---------------------------------------------------------------------------

def test_build_atlas_spec_returns_inserter_spec(tmp_path):
    from marrow.inserter import InserterSpec
    spec = subpage_specs.build_atlas_spec(str(tmp_path))
    assert isinstance(spec, InserterSpec)
    assert spec.key == "atlas"
    assert spec.path.endswith("atlas.md")
    assert callable(spec.fetch)
    assert callable(spec.render_row)
    assert callable(spec.section_of)
    assert callable(spec.render_section_header)


def test_build_atlas_spec_fetch_empty(conn, tmp_path):
    spec = subpage_specs.build_atlas_spec(str(tmp_path))
    rows = spec.fetch(conn)
    assert rows == []


def test_build_atlas_spec_fetch_returns_rows(conn, tmp_path):
    _insert_row(conn, "/tmp/a", note="test dir")
    spec = subpage_specs.build_atlas_spec(str(tmp_path))
    rows = spec.fetch(conn)
    assert len(rows) == 1
    assert rows[0]["path"] == "/tmp/a"
    assert rows[0]["note"] == "test dir"


# ---------------------------------------------------------------------------
# 3. reconcile_atlas parses marker list → db
# ---------------------------------------------------------------------------

def test_reconcile_parses_note_write_naming_depth(conn, tmp_path, monkeypatch):
    root = tmp_path / "fakeroots"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    md_file = tmp_path / "atlas.md"
    child_path = str(child.resolve())
    md_file.write_text(_marker_md(child_path, note="Test note", write="docs/",
                                  naming="snake_case", depth=2))

    n = reconcile_atlas(conn, md_file)
    assert n > 0

    row = conn.execute(
        "SELECT note, write_hint, naming_hint, depth FROM atlas WHERE path=?",
        (child_path,),
    ).fetchone()
    assert row is not None
    assert row["note"] == "Test note"
    assert row["write_hint"] == "docs/"
    assert row["naming_hint"] == "snake_case"
    assert row["depth"] == 2


def test_reconcile_deletes_paths_removed_from_md(conn, tmp_path, monkeypatch):
    root = tmp_path / "fakeroots2"
    root.mkdir()
    child_a = root / "dirA"
    child_a.mkdir()
    child_b = root / "dirB"
    child_b.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    # Pre-insert both with note so reconcile treats them as user-edited
    _insert_row(conn, str(child_a.resolve()), note="manual note A")
    _insert_row(conn, str(child_b.resolve()), note="manual note B")

    # md only has dirA
    md_file = tmp_path / "atlas.md"
    md_file.write_text(_marker_md(str(child_a.resolve()), depth=0))
    reconcile_atlas(conn, md_file)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str(child_a.resolve()) in paths
    assert str(child_b.resolve()) not in paths


# ---------------------------------------------------------------------------
# 4. Render layout
# ---------------------------------------------------------------------------

def test_render_heading_level_first_child():
    root = Path("/tmp/root")
    child = root / "mydir"
    level = _heading_level(str(child), str(root))
    assert level == 3  # first-level = h3


def test_render_heading_level_second():
    root = Path("/tmp/root")
    grandchild = root / "a" / "b"
    level = _heading_level(str(grandchild), str(root))
    assert level == 4


def test_render_heading_level_capped_h6():
    root = Path("/tmp/root")
    deep = root / "a" / "b" / "c" / "d" / "e"
    level = _heading_level(str(deep), str(root))
    assert level == 6


def test_root_shorthand():
    home = Path.home()
    root = home / "cc-lab"
    sh = _root_shorthand(str(root))
    assert sh == "~/cc-lab/"


def test_render_row_bullets(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()

    r = {
        "path": str(child.resolve()),
        "note": "My note",
        "write_hint": "docs/",
        "naming_hint": "snake_case",
        "depth": 2,
        "stale": 0,
    }
    rendered = _render_atlas_row(r, [root.resolve()])
    assert "- note: My note" in rendered
    assert "- write: docs/" in rendered
    assert "- naming: snake_case" in rendered
    assert "- depth: 2" in rendered
    assert "mydir/" in rendered
    # new: inline id marker
    assert f"<!-- id:{str(child.resolve())} -->" in rendered


def test_render_row_empty_fields_show_placeholders(tmp_path):
    """Empty fields must emit placeholder lines so user can see where to type."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()

    r = {
        "path": str(child.resolve()),
        "note": None,
        "write_hint": None,
        "naming_hint": None,
        "depth": 0,
        "stale": 0,
    }
    rendered = _render_atlas_row(r, [root.resolve()])
    # All four lines always emitted even when values are empty
    assert "- note: " in rendered
    assert "- write: " in rendered
    assert "- naming: " in rendered
    assert "- depth: 0" in rendered


def test_render_row_stale_suffix(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    child = root / "gone"
    child.mkdir()

    r = {
        "path": str(child.resolve()),
        "note": None,
        "write_hint": None,
        "naming_hint": None,
        "depth": 0,
        "stale": 1,
    }
    rendered = _render_atlas_row(r, [root.resolve()])
    assert "(stale)" in rendered


def test_render_section_header():
    from marrow.atlas import _section_header
    home = Path.home()
    root = str(home / "cc-lab")
    header = _section_header(root)
    assert header == "## ~/cc-lab/"


def test_render_row_name_is_open_link(tmp_path):
    """Dir name itself must be the file:// link (no separate 'open' tag)."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()
    r = {"path": str(child.resolve()), "note": None, "write_hint": None,
         "naming_hint": None, "depth": 0, "stale": 0}
    rendered = _render_atlas_row(r, [root.resolve()])
    assert f"[mydir/](file://{child.resolve()})" in rendered
    assert "[open](" not in rendered


def test_render_row_emits_h3_heading(tmp_path):
    """_render_atlas_row must emit an H3 heading so the dir shows in outline."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()
    r = {"path": str(child.resolve()), "note": None, "write_hint": None,
         "naming_hint": None, "depth": 0, "stale": 0}
    rendered = _render_atlas_row(r, [root.resolve()])
    first_line = rendered.splitlines()[0]
    assert first_line.startswith("### ")


def test_build_atlas_spec_bootstrap_writes_sections(conn, tmp_path, monkeypatch):
    """Bootstrap renders ## per root section header, marker bullet per dir."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    _insert_row(conn, str(child.resolve()), note="hello", depth=0)

    spec = subpage_specs.build_atlas_spec(str(tmp_path))
    store = MdIndex(conn)
    write_subpage_inserter(spec, conn, store)

    md = Path(spec.path).read_text(encoding="utf-8")
    try:
        rel = root.resolve().relative_to(Path.home())
        shorthand = f"~/{rel}/"
    except ValueError:
        shorthand = str(root.resolve()) + "/"
    assert f"## {shorthand}" in md
    # H3-heading layout: dir name is an open link, marker on next line
    assert f"### [mydir/](file://{child.resolve()})" in md
    assert f"<!-- id:{str(child.resolve())} -->" in md
    assert "- note: hello" in md
    assert "- depth: 0" in md


def test_build_atlas_spec_fetch_skips_root_rows(conn, tmp_path, monkeypatch):
    """fetch() must not return rows for AUTHORIZED_ROOTS paths."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    # Insert both root and child
    _insert_row(conn, str(root.resolve()), depth=1)
    _insert_row(conn, str(child.resolve()), note="child note")

    spec = subpage_specs.build_atlas_spec(str(tmp_path))
    rows = spec.fetch(conn)
    paths = [r["path"] for r in rows]
    assert str(root.resolve()) not in paths
    assert str(child.resolve()) in paths


# ---------------------------------------------------------------------------
# 5. depth=0: sweep does NOT auto-stub sub-dirs
# ---------------------------------------------------------------------------

def test_sweep_depth0_no_stub(conn, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    child = root / "dir_with_children"
    child.mkdir()
    (child / "subA").mkdir()
    (child / "subB").mkdir()

    # Insert with depth=0 — sweep should NOT stub children
    _insert_row(conn, str(child.resolve()), depth=0)

    atlas_sweep_fs(conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str((child / "subA").resolve()) not in paths
    assert str((child / "subB").resolve()) not in paths


# ---------------------------------------------------------------------------
# 6. depth=1 stubs first-level only
# ---------------------------------------------------------------------------

def test_sweep_depth1_stubs_first_level_only(conn, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    child = root / "expand_me"
    child.mkdir()
    sub1 = child / "level1_a"
    sub1.mkdir()
    sub2 = child / "level1_b"
    sub2.mkdir()
    # deeper level — should NOT be stubbed at depth=1
    (sub1 / "level2").mkdir()

    _insert_row(conn, str(child.resolve()), depth=1)
    atlas_sweep_fs(conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str(sub1.resolve()) in paths
    assert str(sub2.resolve()) in paths
    assert str((sub1 / "level2").resolve()) not in paths


# ---------------------------------------------------------------------------
# 7. depth=2 stubs two levels
# ---------------------------------------------------------------------------

def test_sweep_depth2_stubs_two_levels(conn, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    seed = root / "seed"
    seed.mkdir()
    l1 = seed / "level1"
    l1.mkdir()
    l2 = l1 / "level2"
    l2.mkdir()
    l3 = l2 / "level3_too_deep"
    l3.mkdir()

    _insert_row(conn, str(seed.resolve()), depth=2)
    atlas_sweep_fs(conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str(l1.resolve()) in paths
    assert str(l2.resolve()) in paths
    assert str(l3.resolve()) not in paths


# ---------------------------------------------------------------------------
# 8. Vanished dir → stale=1, render shows (stale)
# ---------------------------------------------------------------------------

def test_sweep_marks_vanished_dir_stale(conn, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    seed = root / "seed_for_stale"
    seed.mkdir()
    child = seed / "soon_gone"
    child.mkdir()

    _insert_row(conn, str(seed.resolve()), depth=1)
    atlas_sweep_fs(conn)  # stubs child

    # Now remove child
    child.rmdir()
    atlas_sweep_fs(conn)  # should mark stale

    row = conn.execute(
        "SELECT stale FROM atlas WHERE path=?",
        (str(child.resolve()),),
    ).fetchone()
    assert row is not None
    assert row["stale"] == 1


def test_render_stale_row_shows_stale_suffix(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    child = root / "gone"
    child.mkdir()

    r = {"path": str(child.resolve()), "note": None, "write_hint": None,
         "naming_hint": None, "depth": 0, "stale": 1}
    rendered = _render_atlas_row(r, [root.resolve()])
    assert "(stale)" in rendered


# ---------------------------------------------------------------------------
# 9. Stale row returning → stale cleared
# ---------------------------------------------------------------------------

def test_sweep_clears_stale_when_dir_returns(conn, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    seed = root / "seed_return"
    seed.mkdir()
    child = seed / "comes_back"
    child.mkdir()

    _insert_row(conn, str(seed.resolve()), depth=1)
    atlas_sweep_fs(conn)  # stubs child

    # Mark it stale manually
    child.rmdir()
    atlas_sweep_fs(conn)
    row = conn.execute(
        "SELECT stale FROM atlas WHERE path=?",
        (str(child.resolve()),),
    ).fetchone()
    assert row["stale"] == 1

    # Restore the dir
    child.mkdir()
    atlas_sweep_fs(conn)
    row = conn.execute(
        "SELECT stale FROM atlas WHERE path=?",
        (str(child.resolve()),),
    ).fetchone()
    assert row["stale"] == 0


# ---------------------------------------------------------------------------
# 10. reconcile preserves manual fields across path change
# ---------------------------------------------------------------------------

def test_reconcile_preserves_fields_on_path_rekey(conn, tmp_path, monkeypatch):
    """Simulated rename: old path has note; new md has new path + same note."""
    root = tmp_path / "root"
    root.mkdir()
    old_dir = root / "old_name"
    old_dir.mkdir()
    new_dir = root / "new_name"
    new_dir.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    # Pre-load old path with manual fields
    _insert_row(conn, str(old_dir.resolve()), note="precious note",
                write_hint="docs/", depth=1)

    # md now has new path + same note (simulate user updated md after rename)
    new_path = str(new_dir.resolve())
    md_file = tmp_path / "atlas.md"
    md_file.write_text(_marker_md(new_path, note="precious note",
                                  write="docs/", depth=1))
    reconcile_atlas(conn, md_file)

    # New path is in db with preserved fields
    row = conn.execute(
        "SELECT note, write_hint, depth FROM atlas WHERE path=?",
        (new_path,),
    ).fetchone()
    assert row is not None
    assert row["note"] == "precious note"
    assert row["write_hint"] == "docs/"
    assert row["depth"] == 1

    # Old path is gone
    old_row = conn.execute(
        "SELECT path FROM atlas WHERE path=?",
        (str(old_dir.resolve()),),
    ).fetchone()
    assert old_row is None


# ---------------------------------------------------------------------------
# 11. EXCLUDE_DIRS_TREE honored
# ---------------------------------------------------------------------------

def test_sweep_excludes_excluded_dirs(conn, tmp_path):
    from marrow.drift_sweep import EXCLUDE_DIRS_TREE

    root = tmp_path / "root"
    root.mkdir()
    seed = root / "seed_excl"
    seed.mkdir()

    # Create one excluded and one normal dir
    excluded_name = next(iter(EXCLUDE_DIRS_TREE))
    (seed / excluded_name).mkdir()
    (seed / "normal_dir").mkdir()

    _insert_row(conn, str(seed.resolve()), depth=1)
    atlas_sweep_fs(conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str((seed / excluded_name).resolve()) not in paths
    assert str((seed / "normal_dir").resolve()) in paths


# ---------------------------------------------------------------------------
# 12. ~/.claude whitelist honored
# ---------------------------------------------------------------------------

def test_sweep_claude_whitelist_honored(conn, tmp_path, monkeypatch):
    """Dirs under ~/.claude not in CLAUDE_WHITELIST are not recursed into."""
    from marrow import atlas as _atlas_module

    fake_claude = tmp_path / "fake_claude"
    fake_claude.mkdir()
    allowed = fake_claude / "rules"
    allowed.mkdir()
    not_allowed = fake_claude / "some_private_dir"
    not_allowed.mkdir()

    # Monkeypatch CLAUDE_WHITELIST and _CLAUDE_ROOT
    monkeypatch.setattr(_atlas_module, "CLAUDE_WHITELIST", frozenset({"rules"}))
    monkeypatch.setattr(_atlas_module, "_CLAUDE_ROOT", fake_claude.resolve())

    _insert_row(conn, str(fake_claude.resolve()), depth=1)
    atlas_sweep_fs(conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str(allowed.resolve()) in paths
    assert str(not_allowed.resolve()) not in paths


# ---------------------------------------------------------------------------
# 13. atlas in subpages._REGISTRY
# ---------------------------------------------------------------------------

def test_atlas_registered_in_subpages():
    from marrow import subpages
    assert "atlas" in subpages._REGISTRY
    assert "atlas" in subpages._DISPLAY
    assert "atlas" in subpages._DEFAULT_BOTTOM


# ---------------------------------------------------------------------------
# 14. build_atlas_spec key and path
# ---------------------------------------------------------------------------

def test_atlas_spec_key_and_path(tmp_path):
    spec = subpage_specs.build_atlas_spec(str(tmp_path))
    assert spec.key == "atlas"
    assert str(tmp_path / "atlas.md") == spec.path


# ---------------------------------------------------------------------------
# 15. _parse_atlas_md round-trip
# ---------------------------------------------------------------------------

def test_parse_atlas_md_round_trip(tmp_path):
    """render → parse gives back the same row dict."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()
    from marrow import drift_sweep
    from unittest.mock import patch
    roots = [root.resolve()]

    r = {
        "path": str(child.resolve()),
        "note": "round trip note",
        "write_hint": "src/",
        "naming_hint": "kebab-case",
        "depth": 3,
        "stale": 0,
    }
    rendered = _render_atlas_row(r, roots)
    parsed = _parse_atlas_md(rendered, roots)
    assert len(parsed) == 1
    p = parsed[0]
    assert p["path"] == r["path"]
    assert p["note"] == r["note"]
    assert p["write_hint"] == r["write_hint"]
    assert p["naming_hint"] == r["naming_hint"]
    assert p["depth"] == r["depth"]


def test_parse_atlas_md_empty_fields(tmp_path):
    """Empty field placeholders parse back as None (note/write/naming) or 0 (depth)."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "emptydir"
    child.mkdir()
    roots = [root.resolve()]

    r = {"path": str(child.resolve()), "note": None, "write_hint": None,
         "naming_hint": None, "depth": 0, "stale": 0}
    rendered = _render_atlas_row(r, roots)
    parsed = _parse_atlas_md(rendered, roots)
    assert len(parsed) == 1
    p = parsed[0]
    assert p["note"] is None
    assert p["write_hint"] is None
    assert p["naming_hint"] is None
    assert p["depth"] == 0


def test_parse_atlas_md_depth_field(tmp_path):
    """Parser must handle - depth: N and not revert to 0."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "depthdir"
    child.mkdir()
    roots = [root.resolve()]

    r = {"path": str(child.resolve()), "note": None, "write_hint": None,
         "naming_hint": None, "depth": 5, "stale": 0}
    rendered = _render_atlas_row(r, roots)
    parsed = _parse_atlas_md(rendered, roots)
    assert parsed[0]["depth"] == 5


# ---------------------------------------------------------------------------
# 16. rekey_paths
# ---------------------------------------------------------------------------

def test_rekey_paths_migrates_note(conn, tmp_path):
    """rekey_paths moves src row to dest, preserving all fields."""
    src = str(tmp_path / "old")
    dest = str(tmp_path / "new")
    _insert_row(conn, src, note="keep me", write_hint="x/", depth=2)

    n = rekey_paths(conn, [(src, dest)])
    assert n == 1

    dest_row = conn.execute(
        "SELECT note, write_hint, depth FROM atlas WHERE path=?", (dest,)
    ).fetchone()
    assert dest_row is not None
    assert dest_row["note"] == "keep me"
    assert dest_row["write_hint"] == "x/"
    assert dest_row["depth"] == 2

    src_row = conn.execute(
        "SELECT path FROM atlas WHERE path=?", (src,)
    ).fetchone()
    assert src_row is None


def test_rekey_paths_conflict_drops_src(conn, tmp_path):
    """If dest already exists in atlas, src is removed; dest is untouched."""
    src = str(tmp_path / "old")
    dest = str(tmp_path / "new")
    _insert_row(conn, src, note="src note")
    _insert_row(conn, dest, note="dest note")

    n = rekey_paths(conn, [(src, dest)])
    assert n == 0  # dest pre-existed → src deleted, no UPDATE

    src_row = conn.execute("SELECT path FROM atlas WHERE path=?", (src,)).fetchone()
    assert src_row is None

    dest_row = conn.execute("SELECT note FROM atlas WHERE path=?", (dest,)).fetchone()
    assert dest_row["note"] == "dest note"  # dest untouched


def test_rekey_paths_src_absent_is_noop(conn, tmp_path):
    """Missing src row is silently skipped."""
    n = rekey_paths(conn, [(str(tmp_path / "ghost"), str(tmp_path / "dest"))])
    assert n == 0
