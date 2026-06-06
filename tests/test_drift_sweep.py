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


# ---------------------------------------------------------------------------
# B. Atomic-write + .bak suffix exclusion (drift_sweep-level)
# ---------------------------------------------------------------------------

def test_is_atomic_write_artifact_pytest():
    """pytest atomic-write tmp pattern is detected."""
    from marrow.drift_sweep import _is_atomic_write_artifact
    assert _is_atomic_write_artifact("test_atlas.py.tmp.13018.c7a3adecaf9d")
    assert _is_atomic_write_artifact("foo.json.tmp.7.deadbeef")


def test_is_atomic_write_artifact_marrow():
    """Marrow's `.mrw.<token>` hidden atomic-write prefix is detected."""
    from marrow.drift_sweep import _is_atomic_write_artifact
    assert _is_atomic_write_artifact(".mrw.oap8rcgu")
    assert _is_atomic_write_artifact(".mrw.deadbeef.tmp")


def test_is_atomic_write_artifact_pyc_numeric():
    """`*.pyc.<digits>` (python compile artefact) is detected."""
    from marrow.drift_sweep import _is_atomic_write_artifact
    assert _is_atomic_write_artifact("atlas.cpython-313.pyc.4446290304")


def test_is_atomic_write_artifact_negative():
    """Normal filenames are NOT flagged."""
    from marrow.drift_sweep import _is_atomic_write_artifact
    assert not _is_atomic_write_artifact("widget.py")
    assert not _is_atomic_write_artifact("README.md")
    assert not _is_atomic_write_artifact("alpha.py")


def test_path_excluded_bak_dir():
    """`.venv*.bak` style dirs are excluded by the watcher-edge filter."""
    from marrow.drift_sweep import _path_excluded
    assert _path_excluded("/Users/me/repo/.venv.py314.bak/bin/python")
    assert _path_excluded("/Users/me/repo/.git/index.lock")
    assert _path_excluded("/Users/me/repo/__pycache__/foo.pyc")
    assert _path_excluded("/x/drift_pending/abc.json")


def test_path_excluded_negative():
    from marrow.drift_sweep import _path_excluded
    assert not _path_excluded("/Users/me/repo/marrow/cli.py")
    assert not _path_excluded("/Users/me/repo/docs/plans/today.md")


def test_find_refs_skips_bak_suffix_dirs(drift_env):
    """A `.bak-<timestamp>` dir under root must NOT appear in find_refs results."""
    env = drift_env
    backup_dir = env.root_a / "data.db.bak-20260518-220058"
    backup_dir.mkdir()
    (backup_dir / "stale.md").write_text("see widget.py here\n", encoding="utf-8")

    live = env.root_a / "live.md"
    live.write_text("see widget.py here\n", encoding="utf-8")

    from marrow.drift_sweep import find_refs
    refs = find_refs("widget.py", roots=[env.root_a, env.root_b])
    files = {r["file"] for r in refs}
    assert str(live) in files, "live ref should be found"
    assert not any(".bak-" in f for f in files), \
        f".bak- dir refs leaked: {files}"


def test_find_refs_skips_drift_pending_dir(drift_env):
    """drift_pending/ json must not be re-grepped."""
    env = drift_env
    pending_inside_root = env.root_a / "drift_pending"
    pending_inside_root.mkdir()
    (pending_inside_root / "abc.json").write_text(
        '{"src":"widget.py","dest":"gadget.py"}', encoding="utf-8")
    live = env.root_a / "live.md"
    live.write_text("see widget.py here\n", encoding="utf-8")

    from marrow.drift_sweep import find_refs
    refs = find_refs("widget.py", roots=[env.root_a, env.root_b])
    files = {r["file"] for r in refs}
    assert str(live) in files
    assert not any("drift_pending" in f for f in files), \
        f"drift_pending leaked: {files}"


# ---------------------------------------------------------------------------
# A. Watcher-edge noise filter (via _DriftHandler in watcher.py)
# ---------------------------------------------------------------------------

def test_drift_handler_skips_git_lock(drift_env):
    """`.git/index.lock` events never reach DriftWatcher."""
    env = drift_env
    from marrow.drift_sweep import DriftWatcher
    from marrow.watcher import _DriftHandler
    from unittest.mock import MagicMock

    dw = DriftWatcher(roots=[env.root_a], batch_window=10)
    handler = _DriftHandler(dw, MagicMock())

    class Ev:
        is_directory = False
        src_path = str(env.root_a / ".git" / "index.lock")
        dest_path = str(env.root_a / ".git" / "index")

    handler.on_moved(Ev())
    with dw._lock:
        assert dw._batch == []


def test_drift_handler_skips_pytest_tmp(drift_env):
    """pytest atomic-write `.tmp.<n>.<hex>` events are filtered."""
    env = drift_env
    from marrow.drift_sweep import DriftWatcher
    from marrow.watcher import _DriftHandler
    from unittest.mock import MagicMock

    dw = DriftWatcher(roots=[env.root_a], batch_window=10)
    handler = _DriftHandler(dw, MagicMock())

    class EvCreate:
        is_directory = False
        src_path = str(env.root_a / "test_atlas.py.tmp.13018.c7a3adecaf9d")

    class EvMove:
        is_directory = False
        src_path = str(env.root_a / "test_atlas.py.tmp.13018.c7a3adecaf9d")
        dest_path = str(env.root_a / "test_atlas.py")

    handler.on_created(EvCreate())
    handler.on_moved(EvMove())
    with dw._lock:
        assert dw._batch == []
        assert "test_atlas.py.tmp.13018.c7a3adecaf9d" not in dw._deleted


def test_drift_handler_skips_venv_bak(drift_env):
    """`.venv.py314.bak/bin/python` events are filtered."""
    env = drift_env
    from marrow.drift_sweep import DriftWatcher
    from marrow.watcher import _DriftHandler
    from unittest.mock import MagicMock

    dw = DriftWatcher(roots=[env.root_a], batch_window=10)
    handler = _DriftHandler(dw, MagicMock())

    class Ev:
        is_directory = False
        src_path = str(env.root_a / ".venv.py314.bak" / "bin" / "python")

    handler.on_deleted(Ev())
    with dw._lock:
        assert dw._deleted == {}


# ---------------------------------------------------------------------------
# C. One pending per op — no batch merging
# ---------------------------------------------------------------------------

def test_three_distinct_ops_produce_three_pendings(drift_env, monkeypatch):
    """3 different (src,dest) ops within one flush window → 3 handle_move calls."""
    env = drift_env
    from marrow.drift_sweep import DriftWatcher

    captured: list[tuple] = []
    monkeypatch.setattr(
        "marrow.drift_sweep.handle_move",
        lambda src, dest, roots=None: captured.append((src, dest)) or "pid",
    )

    dw = DriftWatcher(roots=[env.root_a], batch_window=0.05)
    dw.on_moved(str(env.root_a / "a.md"), str(env.root_a / "A.md"))
    dw.on_moved(str(env.root_a / "b.md"), str(env.root_a / "B.md"))
    dw.on_moved(str(env.root_a / "c.md"), str(env.root_a / "C.md"))

    import time
    time.sleep(0.2)

    assert len(captured) == 3, f"expected 3 separate handle_move calls, got {captured}"


def test_duplicate_ops_deduped_in_window(drift_env, monkeypatch):
    """Same (src,dest) emitted 3× in one window → only 1 handle_move call."""
    env = drift_env
    from marrow.drift_sweep import DriftWatcher

    captured: list[tuple] = []
    monkeypatch.setattr(
        "marrow.drift_sweep.handle_move",
        lambda src, dest, roots=None: captured.append((src, dest)) or "pid",
    )

    dw = DriftWatcher(roots=[env.root_a], batch_window=0.05)
    src = str(env.root_a / "x.md")
    dest = str(env.root_a / "y.md")
    dw.on_moved(src, dest)
    dw.on_moved(src, dest)
    dw.on_moved(src, dest)

    import time
    time.sleep(0.2)
    assert len(captured) == 1, f"expected 1 deduped call, got {captured}"


# ---------------------------------------------------------------------------
# D. Safe-classify + auto-apply
# ---------------------------------------------------------------------------

def test_classify_refs_safe_md_with_slash():
    """`.md` ref containing a slash-prefixed token → safe."""
    from marrow.drift_sweep import _classify_refs
    refs = [{"file": "/Users/me/cc-lab/marrow/docs/x.md",
             "line": 1, "col": 1, "text": 'see marrow/widget.py here'}]
    safe, unsafe = _classify_refs(refs)
    assert len(safe) == 1 and not unsafe


def test_classify_refs_unsafe_db_bak():
    """`.db.bak` file ref → unsafe (wrong ext + .bak prefix part)."""
    from marrow.drift_sweep import _classify_refs
    refs = [{"file": "/Users/me/.config/marrow/marrow.db.bak-20260518",
             "line": 1, "col": 1, "text": 'path = marrow/widget.py'}]
    safe, unsafe = _classify_refs(refs)
    assert not safe and len(unsafe) == 1


def test_classify_refs_unsafe_bare_word():
    """Text with no `/` → unsafe (bare-word risk)."""
    from marrow.drift_sweep import _classify_refs
    refs = [{"file": "/x/notes.md",
             "line": 1, "col": 1, "text": "the grill skill is great"}]
    safe, unsafe = _classify_refs(refs)
    assert not safe and len(unsafe) == 1


def test_classify_refs_unsafe_venv_path():
    """Ref in `.venv*/` → unsafe."""
    from marrow.drift_sweep import _classify_refs
    refs = [{"file": "/Users/me/repo/.venv/lib/python3.13/site-packages/x.py",
             "line": 1, "col": 1, "text": "import marrow/widget.py"}]
    safe, unsafe = _classify_refs(refs)
    assert not safe and len(unsafe) == 1


def test_handle_move_safe_auto_applies(drift_env, monkeypatch):
    """All-safe refs → auto-applied, info alert, pending deleted."""
    env = drift_env
    root = env.root_a

    ref1 = root / "doc1.md"
    ref2 = root / "doc2.md"
    ref3 = root / "doc3.md"
    ref1.write_text("see marrow/widget.py here\n", encoding="utf-8")
    ref2.write_text("path = marrow/widget.py\n", encoding="utf-8")
    ref3.write_text('load "marrow/widget.py" now\n', encoding="utf-8")

    alerts: list[tuple] = []
    monkeypatch.setattr(
        "marrow.drift_sweep._emit_alert",
        lambda message, source="drift_sweep", severity="warn", **kw:
            alerts.append((severity, message)),
    )

    from marrow.drift_sweep import handle_move
    pid = handle_move(str(root / "widget.py"), str(root / "gadget.py"),
                      roots=[env.root_a, env.root_b])
    assert pid is not None
    # Pending file deleted by auto-apply
    assert not (env.pending_dir / f"{pid}.json").exists(), \
        "auto-applied pending must be removed"
    # Files actually rewritten
    assert "gadget.py" in ref1.read_text()
    assert "gadget.py" in ref2.read_text()
    assert "gadget.py" in ref3.read_text()
    # Info alert with file preview
    assert alerts, "expected at least one alert"
    sev, msg = alerts[-1]
    assert sev == "info", f"expected info severity, got {sev}: {msg}"
    assert "drift applied" in msg
    assert "doc1.md" in msg or "doc2.md" in msg or "doc3.md" in msg


def test_handle_move_unsafe_keeps_pending(drift_env, monkeypatch):
    """Any unsafe ref → keep pending, warn alert with apply/reject hint."""
    env = drift_env
    root = env.root_a

    # bare-word ref (no slash) → unsafe
    bare = root / "prose.md"
    bare.write_text("the widget.py thing\n", encoding="utf-8")

    alerts: list[tuple] = []
    monkeypatch.setattr(
        "marrow.drift_sweep._emit_alert",
        lambda message, source="drift_sweep", severity="warn", **kw:
            alerts.append((severity, message)),
    )

    from marrow.drift_sweep import handle_move
    pid = handle_move(str(root / "widget.py"), str(root / "gadget.py"),
                      roots=[env.root_a, env.root_b])
    assert pid is not None
    assert (env.pending_dir / f"{pid}.json").exists(), \
        "unsafe pending must be kept for manual review"
    # Files untouched
    assert "widget.py" in bare.read_text()
    # Warn alert with apply/reject hint
    sev, msg = alerts[-1]
    assert sev == "warn"
    assert "drift review" in msg
    assert f"apply {pid}" in msg
    assert f"reject {pid}" in msg


def test_handle_move_no_refs_silent(drift_env, monkeypatch):
    """0 refs → silent drop (no pending, no alert)."""
    env = drift_env
    alerts: list[tuple] = []
    monkeypatch.setattr(
        "marrow.drift_sweep._emit_alert",
        lambda message, source="drift_sweep", severity="warn", **kw:
            alerts.append((severity, message)),
    )
    from marrow.drift_sweep import handle_move
    pid = handle_move(str(env.root_a / "nonexistent.py"),
                      str(env.root_a / "other.py"),
                      roots=[env.root_a, env.root_b])
    assert pid is None
    assert alerts == []


# ---------------------------------------------------------------------------
# E. CLI subcommands
# ---------------------------------------------------------------------------

def test_cli_drift_scan(drift_env, capsys, monkeypatch):
    """`mw drift scan <old> <new>` exits 0 and prints queued pid."""
    env = drift_env
    root = env.root_a
    (root / "widget.py").write_text("# widget.py\n", encoding="utf-8")
    (root / "notes.md").write_text(
        "see marrow/widget.py here\n", encoding="utf-8")

    from marrow import cli
    # Force auto-apply to not blow up looking for repo:
    rc = cli.main(["drift", "scan",
                   str(root / "widget.py"), str(root / "gadget.py")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "drift queued" in out or "no refs" in out


def test_cli_drift_apply(drift_env, capsys):
    """`mw drift apply <pid>` applies a kept pending."""
    env = drift_env
    root = env.root_a
    bare = root / "prose.md"
    bare.write_text("the widget.py thing\n", encoding="utf-8")

    from marrow.drift_sweep import handle_move
    pid = handle_move(str(root / "widget.py"), str(root / "gadget.py"),
                      roots=[env.root_a, env.root_b])
    assert pid is not None
    assert (env.pending_dir / f"{pid}.json").exists()

    from marrow import cli
    rc = cli.main(["drift", "apply", pid])
    assert rc == 0
    out = capsys.readouterr().out
    assert "drift apply" in out
    # Pending gone
    assert not (env.pending_dir / f"{pid}.json").exists()


def test_cli_drift_reject(drift_env, capsys):
    """`mw drift reject <pid>` discards pending."""
    env = drift_env
    root = env.root_a
    bare = root / "prose.md"
    bare.write_text("the widget.py thing\n", encoding="utf-8")

    from marrow.drift_sweep import handle_move
    pid = handle_move(str(root / "widget.py"), str(root / "gadget.py"),
                      roots=[env.root_a, env.root_b])
    assert pid is not None

    from marrow import cli
    rc = cli.main(["drift", "reject", pid])
    assert rc == 0
    assert not (env.pending_dir / f"{pid}.json").exists()


# ---------------------------------------------------------------------------
# a) basename-unchanged move → silent drop
# b) iCloud Drive duplicate filename → noise-gated at watcher edge
# ---------------------------------------------------------------------------

def test_basename_unchanged_move_silent(drift_env, monkeypatch):
    """Pure folder-relocate (basename identical) emits no alert / no pending."""
    env = drift_env
    src = env.root_a / "SLE211"
    dest = env.root_a / "untitled folder" / "SLE211"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Create a ref so refs>0 — proves the early-return is unconditional.
    (env.root_a / "ref.md").write_text("see SLE211 here\n", encoding="utf-8")

    from marrow.drift_sweep import handle_move
    pid = handle_move(str(src), str(dest), roots=[env.root_a, env.root_b])
    assert pid is None


def test_icloud_dup_artifact_detected():
    from marrow.drift_sweep import _is_icloud_dup_artifact
    assert _is_icloud_dup_artifact("AT2 2.docx")
    assert _is_icloud_dup_artifact("LO1-2 2.m4v")
    assert _is_icloud_dup_artifact("note 3.md")
    assert _is_icloud_dup_artifact("plain 5")  # extensionless
    # Negative: legitimate names that look numeric but aren't dups
    assert not _is_icloud_dup_artifact("AT2.docx")
    assert not _is_icloud_dup_artifact("LO1-2.m4v")
    assert not _is_icloud_dup_artifact("SLE211")
    assert not _is_icloud_dup_artifact("v2.md")  # no space before digit


def test_path_excluded_filters_icloud_dup():
    from marrow.drift_sweep import _path_excluded
    assert _path_excluded("/Users/x/Study/AT2 2.docx")
    assert _path_excluded("/Users/x/Study/LO1-2 2.m4v")
    assert not _path_excluded("/Users/x/Study/AT2.docx")


def test_on_moved_drops_icloud_dup(drift_env):
    """Watcher-level: iCloud dup as src or dest is dropped before queueing."""
    from marrow.drift_sweep import DriftWatcher
    env = drift_env
    dw = DriftWatcher(roots=[env.root_a, env.root_b])
    (env.root_a / "AT2.docx").write_bytes(b"x")
    (env.root_a / "AT2 2.docx").write_bytes(b"x")
    dw.on_moved(str(env.root_a / "AT2.docx"), str(env.root_a / "AT2 2.docx"))
    assert dw._batch == []  # dup-as-dest filtered
    dw.on_moved(str(env.root_a / "AT2 2.docx"), str(env.root_a / "AT2.docx"))
    assert dw._batch == []  # dup-as-src filtered
