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
    lookup_by_prefix,
    reconcile_atlas,
    rekey_paths,
    resolve_naming,
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


def _insert_row(conn, path, description=None, naming_hint=None, depth=0):
    conn.execute(
        "INSERT OR REPLACE INTO atlas"
        " (path, description, naming_hint, depth, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (path, description, naming_hint, depth, _now()),
    )
    conn.commit()


def _marker_md(path: str, description="", naming="", depth=0) -> str:
    """Build a single marker-format atlas block for one path."""
    return (
        f"##### [{Path(path).name}/](file://{path}) [{depth}]\n"
        f"<!-- id:{path} -->\n"
        f"- Description: {description}\n"
        f"- Naming: {naming}\n"
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
    assert cols == {"path", "description", "naming_hint", "depth", "updated_at"}
    assert "note" not in cols
    assert "write_hint" not in cols
    assert "stale" not in cols


def test_atlas_path_is_primary_key(conn):
    info = {r["name"]: r for r in conn.execute("PRAGMA table_info(atlas)")}
    assert info["path"]["pk"] == 1


def test_atlas_depth_default_0(conn):
    conn.execute(
        "INSERT INTO atlas (path, updated_at) VALUES ('/tmp/x', ?)", (_now(),)
    )
    conn.commit()
    row = conn.execute("SELECT depth FROM atlas WHERE path='/tmp/x'").fetchone()
    assert row["depth"] == 0


def test_schema_version_current(conn):
    # Bumped to 15 with alerts fingerprint/hit_count dedup hardening.
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 15


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
    _insert_row(conn, "/tmp/a", description="test dir")
    spec = subpage_specs.build_atlas_spec(str(tmp_path))
    rows = spec.fetch(conn)
    assert len(rows) == 1
    assert rows[0]["path"] == "/tmp/a"
    assert rows[0]["description"] == "test dir"


# ---------------------------------------------------------------------------
# 3. reconcile_atlas parses marker list -> db
# ---------------------------------------------------------------------------

def test_reconcile_parses_description_naming_depth(conn, tmp_path, monkeypatch):
    root = tmp_path / "fakeroots"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    md_file = tmp_path / "atlas.md"
    child_path = str(child.resolve())
    md_file.write_text(_marker_md(child_path, description="Test desc",
                                  naming="snake_case", depth=2))

    n = reconcile_atlas(conn, md_file)
    assert n > 0

    row = conn.execute(
        "SELECT description, naming_hint, depth FROM atlas WHERE path=?",
        (child_path,),
    ).fetchone()
    assert row is not None
    assert row["description"] == "Test desc"
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

    _insert_row(conn, str(child_a.resolve()), description="manual A")
    _insert_row(conn, str(child_b.resolve()), description="manual B")

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
    assert level == 3


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
    root = home / "CC-Lab"
    sh = _root_shorthand(str(root))
    assert sh == "~/CC-Lab/"


def test_render_row_bullets(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()

    r = {
        "path": str(child.resolve()),
        "description": "My desc",
        "naming_hint": "snake_case",
        "depth": 2,
    }
    rendered = _render_atlas_row(r, [root.resolve()])
    assert "- Description: My desc" in rendered
    assert "- Naming: snake_case" in rendered
    assert "[2]" in rendered
    assert "mydir/" in rendered
    assert f"<!-- id:{str(child.resolve())} -->" in rendered


def test_render_row_empty_fields_show_placeholders(tmp_path):
    """Empty fields must emit placeholder lines so user can see where to type."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()

    r = {
        "path": str(child.resolve()),
        "description": None,
        "naming_hint": None,
        "depth": 0,
    }
    rendered = _render_atlas_row(r, [root.resolve()])
    assert "- Description: " in rendered
    assert "- Naming: " in rendered
    assert "[0]" in rendered


def test_render_row_no_stale(tmp_path):
    """No stale suffix in new schema."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "gone"
    child.mkdir()

    r = {
        "path": str(child.resolve()),
        "description": None,
        "naming_hint": None,
        "depth": 0,
    }
    rendered = _render_atlas_row(r, [root.resolve()])
    assert "(stale)" not in rendered


def test_render_section_header():
    from marrow.atlas import _section_header
    home = Path.home()
    root = str(home / "CC-Lab")
    header = _section_header(root)
    assert header.startswith("## [CC-Lab/](file://")
    assert header.endswith("/CC-Lab) [0]")


def test_render_row_name_is_open_link(tmp_path):
    """Dir name itself must be the file:// link."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()
    r = {"path": str(child.resolve()), "description": None, "naming_hint": None, "depth": 0}
    rendered = _render_atlas_row(r, [root.resolve()])
    assert f"[mydir/](file://{child.resolve()})" in rendered
    assert "[open](" not in rendered


def test_render_row_emits_h5_heading(tmp_path):
    """_render_atlas_row must emit an H5 heading."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()
    r = {"path": str(child.resolve()), "description": None, "naming_hint": None, "depth": 0}
    rendered = _render_atlas_row(r, [root.resolve()])
    first_line = rendered.splitlines()[0]
    assert first_line.startswith("##### ")


def test_build_atlas_spec_bootstrap_writes_sections(conn, tmp_path, monkeypatch):
    """Bootstrap renders ## per root section header, marker bullet per dir."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    _insert_row(conn, str(child.resolve()), description="hello", depth=0)

    spec = subpage_specs.build_atlas_spec(str(tmp_path))
    store = MdIndex(conn)
    write_subpage_inserter(spec, conn, store)

    md = Path(spec.path).read_text(encoding="utf-8")
    assert f"## [{root.name}/](file://{root.resolve()})" in md
    assert f"##### [mydir/](file://{child.resolve()})" in md
    assert f"<!-- id:{str(child.resolve())} -->" in md
    assert "- Description: hello" in md
    assert "[0]" in md


def test_build_atlas_spec_fetch_skips_root_rows(conn, tmp_path, monkeypatch):
    """fetch() must not return rows for AUTHORIZED_ROOTS paths."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    _insert_row(conn, str(root.resolve()), depth=1)
    _insert_row(conn, str(child.resolve()), description="child note")

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

    _insert_row(conn, str(child.resolve()), depth=0)

    atlas_sweep_fs(conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str((child / "subA").resolve()) not in paths
    assert str((child / "subB").resolve()) not in paths


# ---------------------------------------------------------------------------
# 6. depth=1 stubs first-level only
# ---------------------------------------------------------------------------

def test_sweep_depth1_stubs_first_level_only(conn, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    child = root / "expand_me"
    child.mkdir()
    sub1 = child / "level1_a"
    sub1.mkdir()
    sub2 = child / "level1_b"
    sub2.mkdir()
    (sub1 / "level2").mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    _insert_row(conn, str(child.resolve()), depth=1)
    atlas_sweep_fs(conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str(sub1.resolve()) in paths
    assert str(sub2.resolve()) in paths
    assert str((sub1 / "level2").resolve()) not in paths


# ---------------------------------------------------------------------------
# 7. depth=2 stubs two levels
# ---------------------------------------------------------------------------

def test_sweep_depth2_stubs_two_levels(conn, tmp_path, monkeypatch):
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

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    _insert_row(conn, str(seed.resolve()), depth=2)
    atlas_sweep_fs(conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str(l1.resolve()) in paths
    assert str(l2.resolve()) in paths
    assert str(l3.resolve()) not in paths


# ---------------------------------------------------------------------------
# 8. Vanished dir -> deleted (no stale flag)
# ---------------------------------------------------------------------------

def test_sweep_deletes_vanished_dir(conn, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    seed = root / "seed_for_del"
    seed.mkdir()
    child = seed / "soon_gone"
    child.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    _insert_row(conn, str(seed.resolve()), depth=1)
    atlas_sweep_fs(conn)  # stubs child

    # Now remove child
    child.rmdir()
    atlas_sweep_fs(conn)  # should delete the stub row

    row = conn.execute(
        "SELECT path FROM atlas WHERE path=?",
        (str(child.resolve()),),
    ).fetchone()
    assert row is None


def test_sweep_deletes_only_stub_rows_not_manual(conn, tmp_path, monkeypatch):
    """Manual rows (non-empty description) are preserved even if dir vanishes."""
    root = tmp_path / "root"
    root.mkdir()
    seed = root / "seed_manual"
    seed.mkdir()
    child = seed / "manual_child"
    child.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    _insert_row(conn, str(seed.resolve()), depth=1)
    atlas_sweep_fs(conn)

    # Set manual description on the child
    conn.execute(
        "UPDATE atlas SET description='keep me' WHERE path=?",
        (str(child.resolve()),),
    )
    conn.commit()

    child.rmdir()
    atlas_sweep_fs(conn)

    row = conn.execute(
        "SELECT description FROM atlas WHERE path=?",
        (str(child.resolve()),),
    ).fetchone()
    assert row is not None
    assert row["description"] == "keep me"


# ---------------------------------------------------------------------------
# 9. reconcile preserves manual fields across path change
# ---------------------------------------------------------------------------

def test_reconcile_preserves_fields_on_path_rekey(conn, tmp_path, monkeypatch):
    """Simulated rename: old path has description; new md has new path + same."""
    root = tmp_path / "root"
    root.mkdir()
    old_dir = root / "old_name"
    old_dir.mkdir()
    new_dir = root / "new_name"
    new_dir.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    _insert_row(conn, str(old_dir.resolve()), description="precious note",
                naming_hint="kebab-case", depth=1)

    new_path = str(new_dir.resolve())
    md_file = tmp_path / "atlas.md"
    md_file.write_text(_marker_md(new_path, description="precious note",
                                  naming="kebab-case", depth=1))
    reconcile_atlas(conn, md_file)

    row = conn.execute(
        "SELECT description, naming_hint, depth FROM atlas WHERE path=?",
        (new_path,),
    ).fetchone()
    assert row is not None
    assert row["description"] == "precious note"
    assert row["naming_hint"] == "kebab-case"
    assert row["depth"] == 1

    old_row = conn.execute(
        "SELECT path FROM atlas WHERE path=?",
        (str(old_dir.resolve()),),
    ).fetchone()
    assert old_row is None


# ---------------------------------------------------------------------------
# 10. EXCLUDE_DIRS_TREE honored
# ---------------------------------------------------------------------------

def test_sweep_excludes_excluded_dirs(conn, tmp_path, monkeypatch):
    from marrow.drift_sweep import EXCLUDE_DIRS_TREE
    from marrow import drift_sweep

    root = tmp_path / "root"
    root.mkdir()
    seed = root / "seed_excl"
    seed.mkdir()

    excluded_name = next(iter(EXCLUDE_DIRS_TREE))
    (seed / excluded_name).mkdir()
    (seed / "normal_dir").mkdir()

    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    _insert_row(conn, str(seed.resolve()), depth=1)
    atlas_sweep_fs(conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str((seed / excluded_name).resolve()) not in paths
    assert str((seed / "normal_dir").resolve()) in paths


# ---------------------------------------------------------------------------
# 11. ~/.claude whitelist honored
# ---------------------------------------------------------------------------

def test_sweep_claude_whitelist_honored(conn, tmp_path, monkeypatch):
    """Dirs under ~/.claude not in CLAUDE_WHITELIST are not recursed into."""
    from marrow import atlas as _atlas_module
    from marrow import drift_sweep

    fake_claude = tmp_path / "fake_claude"
    fake_claude.mkdir()
    allowed = fake_claude / "rules"
    allowed.mkdir()
    not_allowed = fake_claude / "some_private_dir"
    not_allowed.mkdir()

    monkeypatch.setattr(_atlas_module, "CLAUDE_WHITELIST", frozenset({"rules"}))
    monkeypatch.setattr(_atlas_module, "_CLAUDE_ROOT", fake_claude.resolve())
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [fake_claude])

    _insert_row(conn, str(fake_claude.resolve()), depth=1)
    atlas_sweep_fs(conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str(allowed.resolve()) in paths
    assert str(not_allowed.resolve()) not in paths


# ---------------------------------------------------------------------------
# 12. atlas in subpages._REGISTRY
# ---------------------------------------------------------------------------

def test_atlas_registered_in_subpages():
    from marrow import subpages
    assert "atlas" in subpages._REGISTRY
    assert "atlas" in subpages._DISPLAY
    assert "atlas" in subpages._DEFAULT_BOTTOM


# ---------------------------------------------------------------------------
# 13. build_atlas_spec key and path
# ---------------------------------------------------------------------------

def test_atlas_spec_key_and_path(tmp_path):
    spec = subpage_specs.build_atlas_spec(str(tmp_path))
    assert spec.key == "atlas"
    assert str(tmp_path / "atlas.md") == spec.path


# ---------------------------------------------------------------------------
# 14. _parse_atlas_md round-trip
# ---------------------------------------------------------------------------

def test_parse_atlas_md_round_trip(tmp_path):
    """render -> parse gives back the same row dict."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "mydir"
    child.mkdir()
    roots = [root.resolve()]

    r = {
        "path": str(child.resolve()),
        "description": "round trip desc",
        "naming_hint": "kebab-case",
        "depth": 3,
    }
    rendered = _render_atlas_row(r, roots)
    parsed = _parse_atlas_md(rendered, roots)
    assert len(parsed) == 1
    p = parsed[0]
    assert p["path"] == r["path"]
    assert p["description"] == r["description"]
    assert p["naming_hint"] == r["naming_hint"]
    assert p["depth"] == r["depth"]


def test_parse_atlas_md_empty_fields(tmp_path):
    """Empty field placeholders parse back as None."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "emptydir"
    child.mkdir()
    roots = [root.resolve()]

    r = {"path": str(child.resolve()), "description": None, "naming_hint": None, "depth": 0}
    rendered = _render_atlas_row(r, roots)
    parsed = _parse_atlas_md(rendered, roots)
    assert len(parsed) == 1
    p = parsed[0]
    assert p["description"] is None
    assert p["naming_hint"] is None
    assert p["depth"] == 0


def test_parse_atlas_md_depth_field(tmp_path):
    """Parser must handle [d=N] and not revert to 0."""
    root = tmp_path / "root"
    root.mkdir()
    child = root / "depthdir"
    child.mkdir()
    roots = [root.resolve()]

    r = {"path": str(child.resolve()), "description": None, "naming_hint": None, "depth": 5}
    rendered = _render_atlas_row(r, roots)
    parsed = _parse_atlas_md(rendered, roots)
    assert parsed[0]["depth"] == 5


# ---------------------------------------------------------------------------
# 15. rekey_paths
# ---------------------------------------------------------------------------

def test_rekey_paths_migrates_description(conn, tmp_path):
    """rekey_paths moves src row to dest, preserving all fields."""
    src = str(tmp_path / "old")
    dest = str(tmp_path / "new")
    _insert_row(conn, src, description="keep me", naming_hint="x/", depth=2)

    n = rekey_paths(conn, [(src, dest)])
    assert n == 1

    dest_row = conn.execute(
        "SELECT description, naming_hint, depth FROM atlas WHERE path=?", (dest,)
    ).fetchone()
    assert dest_row is not None
    assert dest_row["description"] == "keep me"
    assert dest_row["naming_hint"] == "x/"
    assert dest_row["depth"] == 2

    src_row = conn.execute(
        "SELECT path FROM atlas WHERE path=?", (src,)
    ).fetchone()
    assert src_row is None


def test_rekey_paths_conflict_drops_src(conn, tmp_path):
    """If dest already exists in atlas, src is removed; dest is untouched."""
    src = str(tmp_path / "old")
    dest = str(tmp_path / "new")
    _insert_row(conn, src, description="src note")
    _insert_row(conn, dest, description="dest note")

    n = rekey_paths(conn, [(src, dest)])
    assert n == 0

    src_row = conn.execute("SELECT path FROM atlas WHERE path=?", (src,)).fetchone()
    assert src_row is None

    dest_row = conn.execute("SELECT description FROM atlas WHERE path=?", (dest,)).fetchone()
    assert dest_row["description"] == "dest note"


def test_rekey_paths_src_absent_is_noop(conn, tmp_path):
    """Missing src row is silently skipped."""
    n = rekey_paths(conn, [(str(tmp_path / "ghost"), str(tmp_path / "dest"))])
    assert n == 0


# ---------------------------------------------------------------------------
# 17. Bug 1 — retract walks ancestor chain (collapsed root retracts stubs)
# ---------------------------------------------------------------------------

def test_sweep_retracts_under_collapsed_root_ancestor(conn, tmp_path, monkeypatch):
    """When an AUTHORIZED_ROOT itself has depth=0, ALL stub-only descendants
    in the atlas (regardless of intermediate depth) must retract.

    Bug scenario: Study root flipped depth 2→0 to collapse. An intermediate
    sub-dir kept depth=1 from a prior pass (no manual hint fields, just a
    stale seed). Its children look "covered" by that intermediate seed and
    survive retract, even though the canonical root is collapsed.

    Fix: walk ancestor chain; any AUTHORIZED_ROOT ancestor with depth=0
    forces retract of stub-only descendants regardless of intermediate seeds.
    """
    root = tmp_path / "study_root"
    root.mkdir()
    a = root / "lvl1"
    a.mkdir()
    b = a / "lvl2"
    b.mkdir()
    c = b / "lvl3"
    c.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    # Root is collapsed (depth=0). lvl1 still has depth=1 from a prior pass
    # — a stale seed left in db with no manual fields.
    _insert_row(conn, str(root.resolve()), depth=0)
    _insert_row(conn, str(a.resolve()), depth=1)  # stale seed, stub-only
    _insert_row(conn, str(b.resolve()), depth=0)  # covered by a (depth=1)
    _insert_row(conn, str(c.resolve()), depth=0)  # NOT covered (too deep)

    atlas_sweep_fs(conn)

    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    # Root itself stays (AUTHORIZED_ROOTS always spared)
    assert str(root.resolve()) in paths
    # All stub-only descendants retract because the root ancestor is depth=0
    assert str(a.resolve()) not in paths
    assert str(b.resolve()) not in paths
    assert str(c.resolve()) not in paths


def test_sweep_retract_spares_manual_descendant_of_collapsed_root(
        conn, tmp_path, monkeypatch):
    """Stub-only retract under collapsed root must NOT touch rows with
    manual fields (note / write_hint / naming_hint)."""
    root = tmp_path / "study_root2"
    root.mkdir()
    a = root / "lvl1"
    a.mkdir()
    b = a / "lvl2"
    b.mkdir()

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    _insert_row(conn, str(root.resolve()), depth=0)
    _insert_row(conn, str(a.resolve()), depth=0)  # stub-only — retract
    _insert_row(conn, str(b.resolve()), depth=0, description="precious")  # spared

    atlas_sweep_fs(conn)
    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str(a.resolve()) not in paths
    assert str(b.resolve()) in paths


# ---------------------------------------------------------------------------
# 18. Bug 2 — atlas rows outside AUTHORIZED_ROOTS are purged / refused
# ---------------------------------------------------------------------------

def test_sweep_purges_rows_outside_authorized_roots(conn, tmp_path, monkeypatch):
    """Rows whose path is not under any AUTHORIZED_ROOT must be deleted
    by atlas_sweep_fs (one-time / ongoing cleanup)."""
    root = tmp_path / "ar_root"
    root.mkdir()
    inside = root / "child"
    inside.mkdir()
    outside = tmp_path / "stray" / "dir"
    outside.mkdir(parents=True)

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    _insert_row(conn, str(root.resolve()), depth=1)
    _insert_row(conn, str(inside.resolve()), depth=0)
    _insert_row(conn, str(outside.resolve()), depth=0, description="orphan")

    atlas_sweep_fs(conn)
    paths = {r[0] for r in conn.execute("SELECT path FROM atlas").fetchall()}
    assert str(inside.resolve()) in paths
    assert str(outside.resolve()) not in paths


def test_reconcile_refuses_rows_outside_authorized_roots(
        conn, tmp_path, monkeypatch):
    """reconcile_atlas must not insert rows whose path is not under any
    AUTHORIZED_ROOT — even when md contains a marker for them."""
    root = tmp_path / "ar_root2"
    root.mkdir()
    outside = tmp_path / "outside_root" / "dir"
    outside.mkdir(parents=True)

    from marrow import drift_sweep
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [root])

    md_file = tmp_path / "atlas.md"
    md_file.write_text(_marker_md(str(outside.resolve()), description="bad", depth=0))
    reconcile_atlas(conn, md_file)

    row = conn.execute(
        "SELECT path FROM atlas WHERE path=?", (str(outside.resolve()),)
    ).fetchone()
    assert row is None


# ---------------------------------------------------------------------------
# 19. Bug 3 — ATLAS_ROOT_ORDER constant + section_order canonical sequence
# ---------------------------------------------------------------------------

def test_atlas_root_order_constant_defined():
    """ATLAS_ROOT_ORDER must exist with 5 canonical roots in order."""
    from marrow.atlas import ATLAS_ROOT_ORDER
    expected = [
        Path.home() / "Desktop" / "NY",
        Path.home() / "Library" / "Mobile Documents" /
        "com~apple~CloudDocs" / "Study",
        Path.home() / "CC-Lab",
        Path.home() / ".claude",
        Path.home() / ".config",
    ]
    assert list(ATLAS_ROOT_ORDER) == expected


def test_section_order_uses_atlas_root_order(tmp_path, monkeypatch):
    """build_atlas_spec.section_order must emit roots in ATLAS_ROOT_ORDER
    regardless of AUTHORIZED_ROOTS iteration order."""
    from marrow import atlas as atlas_mod
    from marrow import drift_sweep

    fake_roots = [tmp_path / name for name in
                  ("NY", "Study", "CC-Lab", ".claude", ".config")]
    for r in fake_roots:
        r.mkdir()

    scrambled = [fake_roots[3], fake_roots[0], fake_roots[4],
                 fake_roots[2], fake_roots[1]]
    monkeypatch.setattr(atlas_mod, "ATLAS_ROOT_ORDER", fake_roots)
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", scrambled)

    spec = subpage_specs.build_atlas_spec(str(tmp_path))
    ordered = spec.section_order([])

    expected = [str(r.resolve()) for r in fake_roots]
    assert ordered == expected


def test_section_order_appends_extras_at_end(tmp_path, monkeypatch):
    """Labels not in ATLAS_ROOT_ORDER stick at the end, preserving canonical
    order at the front."""
    from marrow import atlas as atlas_mod
    from marrow import drift_sweep

    fake_roots = [tmp_path / "A", tmp_path / "B"]
    for r in fake_roots:
        r.mkdir()
    monkeypatch.setattr(atlas_mod, "ATLAS_ROOT_ORDER", fake_roots)
    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", fake_roots)

    spec = subpage_specs.build_atlas_spec(str(tmp_path))
    extra = str((tmp_path / "Z").resolve())
    ordered = spec.section_order([extra])
    assert ordered[-1] == extra
    assert ordered[:2] == [str(r.resolve()) for r in fake_roots]


# ---------------------------------------------------------------------------
# 20. Bug 4 — _DriftHandler dir on_moved triggers drift ref-scan
# ---------------------------------------------------------------------------

def test_drift_handler_dir_rename_queues_drift_scan(tmp_path, monkeypatch):
    """When a watched directory is renamed, _DriftHandler must:
      1. rekey atlas rows (existing behaviour), AND
      2. queue the rename in DriftWatcher batch so ref-scan runs.
    EXCLUDE_DIRS_SCAN basename rename (e.g. .git) must NOT trigger drift.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from marrow import drift_sweep as ds
    from marrow.watcher import _DriftHandler

    # Redirect drift paths to tmp_path so any pending write is contained.
    pending_dir = tmp_path / "pending"
    backup_dir = tmp_path / "backup"
    pending_dir.mkdir()
    backup_dir.mkdir()
    monkeypatch.setattr(ds, "paths", SimpleNamespace(
        drift_pending_dir=pending_dir,
        drift_backup_dir=backup_dir,
        dir_tree_md=tmp_path / "dir_tree.md",
    ))
    monkeypatch.setattr(ds, "AUTHORIZED_ROOTS", [tmp_path])

    dw = ds.DriftWatcher(roots=[tmp_path], batch_window=10.0)  # long window
    handler = _DriftHandler(dw, MagicMock())

    # Patch out atlas rekey + storage so the handler doesn't need a real db.
    rekeyed: list[tuple] = []
    monkeypatch.setattr(
        "marrow.atlas.rekey_paths",
        lambda conn, ops: rekeyed.extend(ops) or len(ops),
    )
    monkeypatch.setattr(
        "marrow.storage.connect", lambda: MagicMock(close=lambda: None)
    )

    class DirEvent:
        is_directory = True
        src_path = str(tmp_path / "old_dir")
        dest_path = str(tmp_path / "new_dir")

    handler.on_moved(DirEvent())

    # 1. atlas rekey called
    assert rekeyed == [(DirEvent.src_path, DirEvent.dest_path)]
    # 2. drift batch picked up the rename
    with dw._lock:
        batch = list(dw._batch)
    assert batch == [(DirEvent.src_path, DirEvent.dest_path)]


def test_drift_handler_dir_rename_skips_excluded_dirs(tmp_path, monkeypatch):
    """Renames inside .git / __pycache__ etc. must NOT queue drift scan."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from marrow import drift_sweep as ds
    from marrow.watcher import _DriftHandler

    pending_dir = tmp_path / "pending"
    backup_dir = tmp_path / "backup"
    pending_dir.mkdir()
    backup_dir.mkdir()
    monkeypatch.setattr(ds, "paths", SimpleNamespace(
        drift_pending_dir=pending_dir,
        drift_backup_dir=backup_dir,
        dir_tree_md=tmp_path / "dir_tree.md",
    ))
    monkeypatch.setattr(ds, "AUTHORIZED_ROOTS", [tmp_path])

    dw = ds.DriftWatcher(roots=[tmp_path], batch_window=10.0)
    handler = _DriftHandler(dw, MagicMock())

    monkeypatch.setattr("marrow.atlas.rekey_paths", lambda conn, ops: 0)
    monkeypatch.setattr(
        "marrow.storage.connect", lambda: MagicMock(close=lambda: None)
    )

    class GitEvent:
        is_directory = True
        # basename .git is in EXCLUDE_DIRS_SCAN
        src_path = str(tmp_path / "proj" / ".git")
        dest_path = str(tmp_path / "proj" / ".git2")

    handler.on_moved(GitEvent())

    with dw._lock:
        assert dw._batch == []
# 16. test_migrate_v13 — v12 -> v13 data migration
# ---------------------------------------------------------------------------

def test_migrate_v13(tmp_path):
    """Create v12 atlas with note+write_hint+stale rows, run migration, assert new schema."""
    import sqlite3
    db_path = str(tmp_path / "v12.db")

    # Create a db at exactly v12 state (skip v13 migration)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE atlas (
            path TEXT PRIMARY KEY,
            note TEXT,
            write_hint TEXT,
            naming_hint TEXT,
            depth INTEGER NOT NULL DEFAULT 0,
            stale INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX idx_atlas_stale ON atlas(stale)")
    conn.execute("PRAGMA user_version=12")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Row with both note and write_hint
    conn.execute("INSERT INTO atlas VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("/tmp/both", "My note", "docs/", "snake_case", 1, 0, now))
    # Row with only note
    conn.execute("INSERT INTO atlas VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("/tmp/note_only", "Just note", None, None, 0, 0, now))
    # Row with only write_hint
    conn.execute("INSERT INTO atlas VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("/tmp/write_only", None, "scripts/", "kebab", 0, 1, now))
    # Row with neither
    conn.execute("INSERT INTO atlas VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("/tmp/empty", None, None, None, 0, 0, now))
    conn.commit()
    conn.close()

    # Now run the full init which should apply v13
    from marrow import storage as _storage
    import sqlite_vec
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    conn2.enable_load_extension(True)
    sqlite_vec.load(conn2)
    conn2.enable_load_extension(False)
    conn2.execute("PRAGMA journal_mode=WAL")
    with conn2:
        _storage._migrate_to_v13(conn2)
        conn2.execute("PRAGMA user_version=13")

    # Verify schema
    cols = {r["name"] for r in conn2.execute("PRAGMA table_info(atlas)")}
    assert "description" in cols
    assert "note" not in cols
    assert "write_hint" not in cols
    assert "stale" not in cols

    # Verify data preservation
    row_both = conn2.execute("SELECT description, naming_hint FROM atlas WHERE path='/tmp/both'").fetchone()
    assert row_both["description"] == "My note | docs/"
    assert row_both["naming_hint"] == "snake_case"

    row_note = conn2.execute("SELECT description FROM atlas WHERE path='/tmp/note_only'").fetchone()
    assert row_note["description"] == "Just note"

    row_write = conn2.execute("SELECT description FROM atlas WHERE path='/tmp/write_only'").fetchone()
    assert row_write["description"] == "scripts/"

    row_empty = conn2.execute("SELECT description FROM atlas WHERE path='/tmp/empty'").fetchone()
    assert row_empty["description"] is None

    conn2.close()


# ---------------------------------------------------------------------------
# 17. lookup_by_prefix
# ---------------------------------------------------------------------------

def test_lookup_by_prefix(conn, tmp_path):
    """lookup_by_prefix returns exact match and descendants."""
    prefix = str(tmp_path / "root")
    child = str(tmp_path / "root" / "child")
    grandchild = str(tmp_path / "root" / "child" / "grand")
    other = str(tmp_path / "other")

    _insert_row(conn, prefix, description="root desc", depth=2)
    _insert_row(conn, child, description="child desc", depth=1)
    _insert_row(conn, grandchild, description="grand desc", depth=0)
    _insert_row(conn, other, description="other")

    results = lookup_by_prefix(conn, prefix)
    paths = [r["path"] for r in results]
    assert prefix in paths
    assert child in paths
    assert grandchild in paths
    assert other not in paths


def test_lookup_by_prefix_miss(conn, tmp_path):
    """lookup_by_prefix returns empty for non-matching prefix."""
    _insert_row(conn, str(tmp_path / "a"), description="A")
    results = lookup_by_prefix(conn, str(tmp_path / "b"))
    assert results == []


def test_lookup_by_prefix_exact(conn, tmp_path):
    """Exact match returns the single row."""
    p = str(tmp_path / "exact")
    _insert_row(conn, p, description="exact only")
    results = lookup_by_prefix(conn, p)
    assert len(results) == 1
    assert results[0]["path"] == p


# ---------------------------------------------------------------------------
# 18. resolve_naming — P-walk
# ---------------------------------------------------------------------------

def test_resolve_naming_verbatim(conn, tmp_path):
    p = str(tmp_path / "root")
    _insert_row(conn, p, naming_hint="Lec=N.n")
    from marrow import drift_sweep
    roots = [tmp_path.resolve()]
    result = resolve_naming(conn, p, roots)
    assert result == "Lec=N.n"


def test_resolve_naming_empty(conn, tmp_path):
    p = str(tmp_path / "root")
    _insert_row(conn, p)
    from marrow import drift_sweep
    roots = [tmp_path.resolve()]
    result = resolve_naming(conn, p, roots)
    assert "empty" in result.lower() or "sibling" in result.lower() or "pattern" in result.lower()


def test_resolve_naming_p_walk(conn, tmp_path):
    """Root has 'Lec=N.n', mid has 'P', deepest has 'P' -> all resolve to 'Lec=N.n'."""
    root = tmp_path / "root"
    mid = root / "mid"
    deep = mid / "deep"

    root_s = str(root.resolve())
    mid_s = str(mid.resolve())
    deep_s = str(deep.resolve())

    _insert_row(conn, root_s, naming_hint="Lec=N.n")
    _insert_row(conn, mid_s, naming_hint="P")
    _insert_row(conn, deep_s, naming_hint="P")

    from marrow import drift_sweep
    roots = [root.parent.resolve()]

    assert resolve_naming(conn, root_s, roots) == "Lec=N.n"
    assert resolve_naming(conn, mid_s, roots) == "Lec=N.n"
    assert resolve_naming(conn, deep_s, roots) == "Lec=N.n"


# ---------------------------------------------------------------------------
# 19. pretool_use hook tests
# ---------------------------------------------------------------------------

def test_pretool_use_placement(tmp_path, monkeypatch, capsys):
    """Write JSON for a file under CC-Lab -> emit [Path/Naming rules] + Atlas slice."""
    import json
    from marrow import hooks, drift_sweep, storage as _storage

    db_path = str(tmp_path / "h.db")
    conn = _storage.init_db(db_path)

    # Insert atlas row for the target dir
    root = tmp_path / "CC-Lab" / "marrow"
    rules = root / ".claude" / "rules"
    root.mkdir(parents=True)
    rules.mkdir(parents=True)

    _insert_row(conn, str(root.resolve()), description="marrow project", naming_hint="snake_case")
    conn.commit()
    conn.close()

    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [tmp_path / "CC-Lab"])
    monkeypatch.setattr("marrow.config.db_path", lambda: db_path)

    target = str(rules / "scratch_test.md")
    inp = json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": target},
    })

    import io, sys
    old_stdin = sys.stdin
    sys.stdin = io.TextIOWrapper(io.BytesIO(inp.encode()), encoding="utf-8")
    try:
        hooks.pretool_use()
    finally:
        sys.stdin = old_stdin

    captured = capsys.readouterr()
    assert "[Path/Naming rules]" in captured.out
    assert "Description:" in captured.out
    # Hook outputs resolved paths (or tilde-abbreviated if under ~)
    assert str((tmp_path / "CC-Lab").resolve()) in captured.out


def test_pretool_use_literal(tmp_path, monkeypatch, capsys):
    """Edit JSON -> emit only literal path reminder."""
    import json
    from marrow import hooks, drift_sweep

    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [tmp_path])

    inp = json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "PROGRESS.md")},
    })

    import io, sys
    old_stdin = sys.stdin
    sys.stdin = io.TextIOWrapper(io.BytesIO(inp.encode()), encoding="utf-8")
    try:
        hooks.pretool_use()
    finally:
        sys.stdin = old_stdin

    captured = capsys.readouterr()
    # PreToolUse hook ships its message inside a JSON envelope
    # ({"hookSpecificOutput": {"hookEventName": "PreToolUse",
    # "additionalContext": "..."}}) — the only stdout form cc injects into
    # assistant context. Plain-string stdout was the Phase 1 shape.
    payload = json.loads(captured.out.strip())
    assert (
        payload["hookSpecificOutput"]["additionalContext"]
        == "[Path] Use paths with /, not bare filenames."
    )
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "[Path/Naming rules]" not in captured.out


def test_pretool_use_outside_root(tmp_path, monkeypatch, capsys):
    """Write to /tmp/x.md (outside AUTHORIZED_ROOTS) -> empty stdout."""
    import json
    from marrow import hooks, drift_sweep

    monkeypatch.setattr(drift_sweep, "AUTHORIZED_ROOTS", [tmp_path / "restricted"])

    inp = json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/x.md"},
    })

    import io, sys
    old_stdin = sys.stdin
    sys.stdin = io.TextIOWrapper(io.BytesIO(inp.encode()), encoding="utf-8")
    try:
        hooks.pretool_use()
    finally:
        sys.stdin = old_stdin

    captured = capsys.readouterr()
    assert captured.out == ""
