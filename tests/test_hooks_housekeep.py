"""Git auto-commit housekeep: commit subject, docs/stale split,
porcelain parsing, session tag."""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone


from marrow import hooks, storage
from _hooks_shared import (  # noqa: F401 — fixtures resolved by name
    _hook_out,
    _iso,
    _no_real_git,
    _out,
    _pretool,
    _seed_session,
    _stdin,
    env,
)

# -- housekeep commit subject stamp -------------------------------------------

def _cats(**kw):
    base = {"deleted": [], "renamed": [], "added": [], "modified": []}
    base.update(kw)
    return base


def test_housekeep_subject_carries_session_tag(env):
    msg = hooks._build_housekeep_commit_msg(_cats(modified=["a.py"]), 6, "cli·ab3a")
    assert msg.splitlines()[0] == "auto: session-start housekeep (6 files) [cli·ab3a]"
    assert msg.splitlines()[2] == "modified: a.py"


def test_housekeep_subject_unchanged_without_tag(env):
    msg = hooks._build_housekeep_commit_msg(_cats(modified=["a.py"]), 6, None)
    assert msg.splitlines()[0] == "auto: session-start housekeep (6 files)"


def test_housekeep_subject_kind_override(env):
    msg = hooks._build_housekeep_commit_msg(
        _cats(modified=["a.md"]), 2, "cli·ab3a", "docs housekeep")
    assert msg.splitlines()[0] == "auto: docs housekeep (2 files) [cli·ab3a]"


# -- housekeep docs/stale split -----------------------------------------------

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


def _mk_repo(tmp_path):
    repo = tmp_path / "hk"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _porcelain(repo):
    return [ln for ln in _git(repo, "status", "--porcelain").stdout.splitlines()
            if ln.strip()]


def test_split_housekeep_dirty_buckets(env, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks.housekeep, "_housekeep_stale_hours", lambda: 2.0)
    monkeypatch.setattr(hooks.housekeep, "_housekeep_docs_exts",
                        lambda: {".md", ".toml", ".json", ".txt"})
    repo = _mk_repo(tmp_path)
    (repo / "notes.md").write_text("fresh doc\n")
    (repo / "fresh.py").write_text("x = 1\n")
    (repo / "old.py").write_text("y = 2\n")
    old_ts = time.time() - 3 * 3600
    os.utime(repo / "old.py", (old_ts, old_ts))
    (repo / "seed.txt").unlink()

    docs, stale, fresh = hooks._split_housekeep_dirty(str(repo), _porcelain(repo))
    assert sorted(ln[3:].strip() for ln in docs) == ["notes.md", "seed.txt"]
    assert [ln[3:].strip() for ln in stale] == ["old.py"]
    assert [ln[3:].strip() for ln in fresh] == ["fresh.py"]


def test_split_treats_missing_mtime_as_stale(env, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks.housekeep, "_housekeep_stale_hours", lambda: 2.0)
    monkeypatch.setattr(hooks.housekeep, "_housekeep_docs_exts", lambda: {".md"})
    repo = _mk_repo(tmp_path)
    (repo / "gone.py").write_text("z = 3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add gone")
    (repo / "gone.py").unlink()
    docs, stale, fresh = hooks._split_housekeep_dirty(str(repo), _porcelain(repo))
    assert (docs, [ln[3:].strip() for ln in stale], fresh) == ([], ["gone.py"], [])


def test_split_untracked_dir_judged_by_newest_file_inside(
        env, tmp_path, monkeypatch):
    """`?? dir/` — an old directory holding a just-written file stays fresh."""
    monkeypatch.setattr(hooks.housekeep, "_housekeep_stale_hours", lambda: 2.0)
    monkeypatch.setattr(hooks.housekeep, "_housekeep_docs_exts", lambda: {".md"})
    repo = _mk_repo(tmp_path)
    d = repo / "wip"
    (d / "deep").mkdir(parents=True)
    (d / "deep" / "new.py").write_text("x = 1\n")
    old_ts = time.time() - 9 * 3600
    for p in (d, d / "deep"):
        os.utime(p, (old_ts, old_ts))

    docs, stale, fresh = hooks._split_housekeep_dirty(str(repo), _porcelain(repo))
    assert (docs, stale) == ([], [])
    assert [ln[3:].strip() for ln in fresh] == ["wip/"]

    os.utime(d / "deep" / "new.py", (old_ts, old_ts))
    docs, stale, fresh = hooks._split_housekeep_dirty(str(repo), _porcelain(repo))
    assert [ln[3:].strip() for ln in stale] == ["wip/"] and fresh == []


def test_commit_housekeep_groups_two_commits_and_leaves_fresh(
        env, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks.housekeep, "_housekeep_stale_hours", lambda: 2.0)
    monkeypatch.setattr(hooks.housekeep, "_housekeep_docs_exts", lambda: {".md"})
    repo = _mk_repo(tmp_path)
    (repo / "notes.md").write_text("doc\n")
    (repo / "fresh.py").write_text("x = 1\n")
    (repo / "old.py").write_text("y = 2\n")
    old_ts = time.time() - 5 * 3600
    os.utime(repo / "old.py", (old_ts, old_ts))

    out = hooks._commit_housekeep_groups(str(repo), _porcelain(repo),
                                         "cli·ab3a", "cwd")

    subjects = _git(repo, "log", "--format=%s", "-3").stdout.splitlines()
    assert subjects[1] == "auto: docs housekeep (1 files) [cli·ab3a]"
    assert subjects[0] == "auto: stale leftovers (1 files) [cli·ab3a]"
    # fresh.py untouched: still the only dirty entry
    assert [ln[3:].strip() for ln in _porcelain(repo)] == ["fresh.py"]
    assert any("skipped 1 fresh" in ln for ln in out)
    assert any(ln.startswith("cwd docs: committed 1 files") for ln in out)
    assert any(ln.startswith("cwd stale: committed 1 files") for ln in out)


def test_commit_housekeep_groups_docs_only_single_commit(
        env, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks.housekeep, "_housekeep_stale_hours", lambda: 2.0)
    monkeypatch.setattr(hooks.housekeep, "_housekeep_docs_exts", lambda: {".md"})
    repo = _mk_repo(tmp_path)
    (repo / "a.md").write_text("a\n")
    (repo / "b.md").write_text("b\n")
    hooks._commit_housekeep_groups(str(repo), _porcelain(repo), None, "cwd")
    subjects = _git(repo, "log", "--format=%s", "-2").stdout.splitlines()
    assert subjects[0] == "auto: docs housekeep (2 files)"
    assert subjects[1] == "seed"
    assert _porcelain(repo) == []


def test_porcelain_paths_rename_and_quoting():
    assert hooks._porcelain_paths("R  old.py -> new.py") == ["old.py", "new.py"]
    assert hooks._porcelain_paths(' M "caf\\303\\251.md"') == ["café.md"]


def test_session_tag_resolution(env):
    db, _, _ = env
    now = datetime.now(timezone.utc)
    _seed_session(db, "ab3ac0de-1111", "cli", "/repo", _iso(now), _iso(now))
    _seed_session(db, "nochan-2222", "", "/repo", _iso(now), _iso(now))
    conn = storage.init_db(db)
    assert hooks._session_tag("ab3ac0de-1111", conn) == "cli·ab3a"
    assert hooks._session_tag("nochan-2222", conn) is None   # blank channel
    assert hooks._session_tag("missing", conn) is None       # no row
    assert hooks._session_tag(None, conn) is None
    conn.close()
