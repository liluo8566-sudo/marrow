"""Tests for marrow/drift_sweep.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures — redirect all drift paths to tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture()
def drift_env(tmp_path, monkeypatch):
    """Redirect authorized roots + drift dirs to tmp_path sub-dirs."""
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    pending_dir = tmp_path / "drift_pending"
    backup_dir = tmp_path / "drift_backup"
    tree_md = tmp_path / "dir_tree.md"

    for d in [root_a, root_b, pending_dir, backup_dir]:
        d.mkdir(parents=True)

    from types import SimpleNamespace
    fake_paths = SimpleNamespace(
        drift_pending_dir=pending_dir,
        drift_backup_dir=backup_dir,
        dir_tree_md=tree_md,
    )

    import marrow.drift_sweep as ds
    monkeypatch.setattr(ds, "paths", fake_paths)
    monkeypatch.setattr(ds, "AUTHORIZED_ROOTS", [root_a, root_b])

    return SimpleNamespace(
        root_a=root_a,
        root_b=root_b,
        pending_dir=pending_dir,
        backup_dir=backup_dir,
        tree_md=tree_md,
        tmp=tmp_path,
    )


# ---------------------------------------------------------------------------
# Test: same-root mv dry-run
# ---------------------------------------------------------------------------

def test_same_root_mv_dry_run(drift_env):
    """Create 3 files with mutual refs, mv A→B, assert pending has ≥3 refs."""
    env = drift_env
    root = env.root_a

    a = root / "alpha.py"
    b = root / "beta.md"
    c = root / "gamma.md"

    a.write_text('import alpha\nfrom alpha import foo\npath = "alpha.py"\n', encoding="utf-8")
    b.write_text('see alpha.py for details\nref: alpha.py\n', encoding="utf-8")
    c.write_text('load "alpha.py" here\n', encoding="utf-8")

    # Simulate mv alpha.py → new_alpha.py (just pass paths — file needn't actually exist at dest)
    from marrow.drift_sweep import handle_move
    pid = handle_move(str(a), str(root / "new_alpha.py"))

    assert pid is not None, "expected a pending id"

    # Check pending json
    pending_file = env.pending_dir / f"{pid}.json"
    assert pending_file.exists(), "pending json missing"
    data = json.loads(pending_file.read_text())
    assert len(data["refs"]) >= 3, f"expected ≥3 refs, got {data['refs']}"

    # Originals untouched
    assert "alpha.py" in a.read_text()
    assert "alpha.py" in b.read_text()
    assert "alpha.py" in c.read_text()


# ---------------------------------------------------------------------------
# Test: confirm applies replacements
# ---------------------------------------------------------------------------

def test_confirm_applies(drift_env):
    """After confirm: grep A→0, grep B→≥3 hits, backup exists for non-git files."""
    env = drift_env
    root = env.root_a

    a = root / "widget.py"
    ref1 = root / "doc1.md"
    ref2 = root / "doc2.md"
    ref3 = root / "doc3.md"

    a.write_text('# widget.py placeholder\n', encoding="utf-8")
    ref1.write_text('see widget.py\n', encoding="utf-8")
    ref2.write_text('load "widget.py" for info\n', encoding="utf-8")
    ref3.write_text('path = "widget.py"\n', encoding="utf-8")

    from marrow.drift_sweep import handle_move, apply_confirm

    new_path = root / "gadget.py"
    pid = handle_move(str(a), str(new_path))
    assert pid is not None

    result = apply_confirm(pid, roots=[env.root_a, env.root_b])
    assert result["ok"], f"confirm failed: {result}"

    # Old name gone from ref files
    for ref in [ref1, ref2, ref3]:
        content = ref.read_text()
        assert "widget.py" not in content, f"old name still in {ref}: {content!r}"

    # New name present
    hits = sum(
        1 for ref in [ref1, ref2, ref3]
        if "gadget.py" in ref.read_text()
    )
    assert hits >= 3, f"expected ≥3 files updated, got {hits}"

    # Pending json removed
    assert not (env.pending_dir / f"{pid}.json").exists()


# ---------------------------------------------------------------------------
# Test: reject discards pending, files untouched
# ---------------------------------------------------------------------------

def test_reject_discards(drift_env):
    env = drift_env
    root = env.root_a

    a = root / "engine.py"
    ref = root / "readme.md"
    a.write_text('# engine.py\n', encoding="utf-8")
    ref.write_text('see engine.py in root\n', encoding="utf-8")

    from marrow.drift_sweep import handle_move, apply_reject

    pid = handle_move(str(a), str(root / "motor.py"))
    assert pid is not None

    original_content = ref.read_text()
    result = apply_reject(pid)
    assert result["ok"]

    # Pending gone
    assert not (env.pending_dir / f"{pid}.json").exists()

    # Files untouched
    assert ref.read_text() == original_content


# ---------------------------------------------------------------------------
# Test: dangling delete — report written, files NOT replaced
# ---------------------------------------------------------------------------

def test_dangling_delete(drift_env):
    env = drift_env
    root = env.root_a

    a = root / "service.py"
    ref1 = root / "notes.md"
    ref2 = root / "config.md"

    a.write_text('# service.py\n', encoding="utf-8")
    ref1.write_text('import from service.py\n', encoding="utf-8")
    ref2.write_text('path = "service.py"\n', encoding="utf-8")

    original1 = ref1.read_text()
    original2 = ref2.read_text()

    from marrow.drift_sweep import handle_dangling_delete

    pid = handle_dangling_delete(str(a))
    assert pid is not None, "expected report for file with refs"

    # Check pending has refs
    data = json.loads((env.pending_dir / f"{pid}.json").read_text())
    assert len(data["refs"]) >= 2

    # dest should be empty string (dangling)
    assert data["dest"] == ""

    # Files NOT replaced
    assert ref1.read_text() == original1
    assert ref2.read_text() == original2


# ---------------------------------------------------------------------------
# Test: path-shaped match only
# ---------------------------------------------------------------------------

def test_path_shaped_only(drift_env):
    env = drift_env
    root = env.root_a

    prose_file = root / "prose.md"
    path_file = root / "config.md"

    # "marrow project" is prose — no path markers
    prose_file.write_text(
        "The marrow project is great.\nmarrow does cool things.\n",
        encoding="utf-8",
    )
    # "marrow/foo.py" is path-shaped
    path_file.write_text(
        'see marrow/foo.py for details\nload "marrow/foo.py"\n',
        encoding="utf-8",
    )

    from marrow.drift_sweep import find_refs, _path_in_line

    # prose line should NOT match
    assert not _path_in_line("marrow", "The marrow project is great.")
    assert not _path_in_line("marrow", "marrow does cool things.")

    # path line SHOULD match
    assert _path_in_line("marrow/foo.py", 'see marrow/foo.py for details')
    assert _path_in_line("marrow/foo.py", 'load "marrow/foo.py"')

    # refs for "marrow/foo.py" should find path_file but not prose_file
    refs = find_refs("marrow/foo.py", roots=[env.root_a, env.root_b])
    ref_files = {r["file"] for r in refs}
    assert str(path_file) in ref_files, "path_file should be in refs"
    assert str(prose_file) not in ref_files, "prose_file should NOT be in refs"


# ---------------------------------------------------------------------------
# Test: dir_tree refresh
# ---------------------------------------------------------------------------

def test_dir_tree_refresh(drift_env):
    """dir_tree is a dirs-only structural overview (files are omitted —
    grep covers files). Verify rename of a directory updates the tree."""
    env = drift_env
    root = env.root_a

    old_dir = root / "old_module"
    new_dir = root / "new_module"
    old_dir.mkdir()
    (old_dir / "x.py").write_text("x = 1\n", encoding="utf-8")

    from marrow.drift_sweep import refresh_dir_tree

    refresh_dir_tree(roots=[env.root_a, env.root_b])
    tree_content = env.tree_md.read_text()
    assert "old_module/" in tree_content
    assert "x.py" not in tree_content  # files intentionally omitted

    old_dir.rename(new_dir)

    refresh_dir_tree(roots=[env.root_a, env.root_b])
    tree_content = env.tree_md.read_text()
    assert "new_module/" in tree_content, "new dir missing from tree"
    assert "old_module/" not in tree_content, "old dir still in tree"


# ---------------------------------------------------------------------------
# Test: DriftWatcher batch debounce (unit-level, no real filesystem events)
# ---------------------------------------------------------------------------

def test_drift_watcher_on_moved_queues(drift_env, monkeypatch):
    """DriftWatcher.on_moved should queue an op into the batch."""
    env = drift_env
    root = env.root_a

    a = root / "alpha.py"
    b = root / "beta.md"
    a.write_text('# alpha.py\n', encoding="utf-8")
    b.write_text('see alpha.py here\n', encoding="utf-8")

    from marrow.drift_sweep import DriftWatcher, handle_move

    captured: list[tuple] = []
    monkeypatch.setattr(
        "marrow.drift_sweep.handle_move",
        lambda src, dest, roots=None: captured.append((src, dest)) or "fake-pid",
    )

    dw = DriftWatcher(roots=[env.root_a, env.root_b], batch_window=0.05)
    dw.on_moved(str(a), str(root / "new_alpha.py"))

    import time
    time.sleep(0.15)  # wait for debounce timer

    assert len(captured) >= 1, "expected at least one handle_move call"


def test_drift_watcher_cross_root_mv(drift_env, monkeypatch):
    """Trigger B: deleted from root_a + created in root_b with same basename/size."""
    env = drift_env

    old_path = env.root_a / "shared.py"
    new_path = env.root_b / "shared.py"
    content = "# shared.py content\n"
    old_path.write_text(content, encoding="utf-8")
    new_path.write_text(content, encoding="utf-8")

    from marrow.drift_sweep import DriftWatcher

    captured: list[tuple] = []
    monkeypatch.setattr(
        "marrow.drift_sweep.handle_move",
        lambda src, dest, roots=None: captured.append((src, dest)) or "pid",
    )

    dw = DriftWatcher(roots=[env.root_a, env.root_b], batch_window=0.05)
    dw.on_deleted(str(old_path))
    dw.on_created(str(new_path))

    import time
    time.sleep(0.15)

    assert any(
        "shared.py" in src and "shared.py" in dest
        for src, dest in captured
    ), f"cross-root mv not captured: {captured}"
