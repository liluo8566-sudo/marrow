"""Global test fixtures.

Two autouse guards:

1. `_redirect_marrow_data_dir` (session-scope, autouse): patches
   `marrow.config.DATA_DIR` and `CONFIG_PATH` to a per-session tmp dir.
   Any caller that falls back to `config.db_path()` / `config.load()` —
   including `storage.connect(None)` and a forgotten `db=` on
   `repo.add_alert(...)` — now writes to the tmp db, never to the real
   `~/.config/marrow/marrow.db`. Belt-and-braces: individual tests that
   call `monkeypatch.setattr(config, "DATA_DIR", ...)` still win for
   their own scope; this only catches the leak path.

2. `_disable_hooks_popen_detach` (function-scope, autouse): hook tests
   invoke `hooks.main(['session_*'])`, which fires `popen_detach` (title
   summarize). The child subprocess loads REAL config (monkeypatch is
   in-process only) and would write to the real db. Neutering the
   hook-side reference keeps tests isolated. The direct popen_detach
   contract test imports from `marrow.popen_detach` and is unaffected.
"""
from __future__ import annotations

import builtins
import os
import sqlite3
from pathlib import Path

os.environ.setdefault("WATCHDOG_USE_POLLING", "1")

import pytest


# ── hard wall: no test may WRITE under the real ~/.config/marrow/ tree ─────────
_REAL_MARROW_DIR = Path(os.path.expanduser("~/.config/marrow")).resolve()
_WRITE_MODE_CHARS = set("waxr+")  # any mode that can create/append/truncate


def _is_write_mode(mode: str) -> bool:
    m = str(mode)
    if "r" in m and "+" not in m:
        return False  # pure read (allowed — e.g. db_leak_guard side-channel)
    return any(c in _WRITE_MODE_CHARS for c in m) and m != "r"


def _under_real_marrow(target) -> bool:
    try:
        p = Path(os.fspath(target)).resolve()
    except (TypeError, ValueError, OSError):
        return False
    return p == _REAL_MARROW_DIR or _REAL_MARROW_DIR in p.parents


def _sqlite_target_under_real(database, uri: bool) -> bool:
    """Resolve the on-disk path a sqlite3.connect(database, uri=...) call targets
    and report whether it lands under the real ~/.config/marrow/ tree.

    In-memory (":memory:", empty) and unresolvable targets -> False (allowed).
    For a `file:` URI the path is the part before any `?query`."""
    if database is None:
        return False
    s = os.fspath(database) if not isinstance(database, str) else database
    if s in ("", ":memory:") or s.startswith(":memory:"):
        return False
    if uri and s.startswith("file:"):
        path_part = s[len("file:"):].split("?", 1)[0]
        if not path_part or path_part.startswith(":memory:"):
            return False
        return _under_real_marrow(path_part)
    return _under_real_marrow(s)


def _sqlite_is_readonly(database, uri: bool) -> bool:
    """True iff a sqlite3.connect request is read-only: a `file:` URI carrying
    mode=ro (or immutable=1) with uri=True. Anything else (plain path, mode=rwc,
    default) is treated as writable."""
    if not uri:
        return False
    s = database if isinstance(database, str) else os.fspath(database)
    if not s.startswith("file:") or "?" not in s:
        return False
    query = s.split("?", 1)[1]
    params = query.replace(";", "&").split("&")
    for p in params:
        if p in ("mode=ro", "immutable=1"):
            return True
    return False


@pytest.fixture(scope="session", autouse=True)
def _forbid_real_marrow_writes():
    """FAIL LOUDLY if any test opens a real ~/.config/marrow/ path for writing.

    A non-isolated test (one that forgot to route its config/cortex paths into
    tmp) would otherwise silently pollute the live wake_signal.log / wake_audit.log
    / marrow.db — the 07-14 incident. This barrier turns that leak into an
    immediate AssertionError instead of a silent pass. Reads are allowed."""
    _real_open = builtins.open
    _real_path_open = Path.open

    def _guard(target):
        if _under_real_marrow(target):
            raise AssertionError(
                f"test attempted a WRITE under the real ~/.config/marrow/ tree: "
                f"{target!r} — this path must be isolated to tmp (see conftest "
                f"_redirect_marrow_data_dir).")

    def _open(file, mode="r", *a, **kw):
        if _is_write_mode(mode):
            _guard(file)
        return _real_open(file, mode, *a, **kw)

    def _path_open(self, mode="r", *a, **kw):
        if _is_write_mode(mode):
            _guard(self)
        return _real_path_open(self, mode, *a, **kw)

    # atomic_write (tempfile.mkstemp + os.replace) and the cortex flock lockfile
    # (os.open O_CREAT) bypass builtins.open — guard the low-level calls too.
    _real_os_open = os.open
    _real_os_replace = os.replace

    def _os_open(path, flags, *a, **kw):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND):
            _guard(path)
        return _real_os_open(path, flags, *a, **kw)

    def _os_replace(src, dst, *a, **kw):
        _guard(dst)
        return _real_os_replace(src, dst, *a, **kw)

    # sqlite3.connect() does its file I/O in the C extension, bypassing every
    # open() patch above — a test connecting to the real marrow.db could still
    # INSERT/migrate silently. Allow real-tree connections ONLY read-only
    # (file: URI with mode=ro / immutable=1); anything writable raises.
    _real_sqlite_connect = sqlite3.connect

    # sqlite3.connect positional order after `database`:
    #   timeout, detect_types, isolation_level, check_same_thread,
    #   factory, cached_statements, uri
    # -> `uri` is positional index 6 in *a (the 8th argument overall). On
    # Python 3.12/3.13 a caller may pass it positionally
    # (connect(db, 5.0, 0, None, True, Connection, 128, True)), which a
    # kw-only read would miss — letting a writable real-tree connect slip past.
    _URI_POS = 6

    def _sqlite_connect(database=":memory:", *a, **kw):
        if "uri" in kw:
            uri = bool(kw["uri"])
        elif len(a) > _URI_POS:
            uri = bool(a[_URI_POS])
        else:
            uri = False
        if _sqlite_target_under_real(database, uri) and not _sqlite_is_readonly(database, uri):
            raise AssertionError(
                f"test attempted a WRITABLE sqlite3.connect under the real "
                f"~/.config/marrow/ tree: {database!r} — connect read-only via "
                f"a `file:...?mode=ro` URI (uri=True), or route to a tmp db "
                f"(see conftest _redirect_marrow_data_dir).")
        return _real_sqlite_connect(database, *a, **kw)

    mp = pytest.MonkeyPatch()
    mp.setattr(builtins, "open", _open)
    mp.setattr(Path, "open", _path_open)
    mp.setattr(os, "open", _os_open)
    mp.setattr(os, "replace", _os_replace)
    mp.setattr(sqlite3, "connect", _sqlite_connect)
    yield
    mp.undo()


@pytest.fixture(scope="session", autouse=True)
def _redirect_marrow_data_dir(tmp_path_factory):
    """Redirect marrow.config DATA_DIR/CONFIG_PATH to a session tmp dir.

    Patches the module object directly so `from marrow import config;
    config.db_path()` returns the test path. Survives reimports because
    the attribute lives on the module singleton.

    Also redirects db_pages_path / sub_pages_path —
    these fall back to `~/Desktop/NY/...` (NOT DATA_DIR), so a test that
    forgets to patch them would write into the real Obsidian vault. This
    autouse guard makes that leak impossible.
    """
    from marrow import config

    tmp = tmp_path_factory.mktemp("marrow-data")
    vault = tmp / "vault"
    (vault / "db-pages").mkdir(parents=True, exist_ok=True)
    cortex_home = tmp / "cortex"
    cortex_home.mkdir(parents=True, exist_ok=True)
    (cortex_home / "state").mkdir(exist_ok=True)
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DATA_DIR", tmp)
    mp.setattr(config, "CONFIG_PATH", tmp / "config.toml")
    mp.setattr(config, "db_pages_path",
               lambda: str(vault / "db-pages"))
    mp.setattr(config, "sub_pages_path",
               lambda: str(vault / "db-pages"))

    # cortex writes (wake_signal.log / wake_audit.log / wake_state.json /
    # wishlist.md) resolve from [cortex].home — a config VALUE defaulting to the
    # REAL ~/.config/marrow/cortex, NOT derived from DATA_DIR. Without this a
    # test-rendered free-round note leaked into the live ear channel (07-14
    # incident). Wrap config.load so every cortex file lands under tmp.
    _real_load = config.load

    def _load_isolated():
        cfg = _real_load()
        cx = dict(cfg.get("cortex", {}) or {})
        cx["home"] = str(cortex_home)
        cx["wishlist_path"] = ""  # derive from the tmp home
        cfg["cortex"] = cx
        return cfg

    mp.setattr(config, "load", _load_isolated)

    # marrow.paths singleton + hooks session-claim file also hardcode the real
    # tree; redirect them so migrate/drift never leak.
    from marrow import paths as _paths_mod
    for _f in ("marrow_db", "drift_pending_dir", "drift_backup_dir",
               "dir_tree_md", "logs_dir", "state_dir"):
        setattr(_paths_mod.paths, _f, tmp / Path(getattr(_paths_mod.paths, _f)).name)
    try:
        from marrow import hooks as _hooks_mod
        mp.setattr(_hooks_mod, "_SESSION_CLAIMS_PATH",
                   tmp / "session_claims.json")
    except ImportError:
        pass

    # Module-level path/db constants captured at IMPORT time (before this
    # fixture ran) still point at the real tree — patching config.DATA_DIR /
    # the paths singleton comes too late for them. A schedule snapshot
    # mkdir()s a real dir. Repoint each one at tmp.
    try:
        from marrow import schedule as _sched_mod
        mp.setattr(_sched_mod, "_SNAPSHOT_DIR", tmp / "schedule-snapshots")
        mp.setattr(_sched_mod, "_FAIL_LOG", tmp / "logs" / "cadence_fail.log")
        mp.setattr(_sched_mod, "_DAILY_PATH", str(tmp / "daily.md"))
    except ImportError:
        pass
    # _DB constants = config.db_path() frozen at import (real marrow.db). The
    # sqlite3 barrier now blocks writable real-tree connects, but repoint them
    # at the tmp db so any code reading the module constant hits the isolated db.
    _tmp_db = str(tmp / "marrow.db")
    for _mod_name in ("cortex_bridge", "daemon"):
        try:
            _m = __import__(f"marrow.{_mod_name}", fromlist=["_DB"])
            if hasattr(_m, "_DB"):
                mp.setattr(_m, "_DB", _tmp_db)
        except ImportError:
            pass

    yield tmp
    mp.undo()


@pytest.fixture(autouse=True)
def _persona_markers(monkeypatch):
    """Pin timeline persona markers to N/Y so existing 【N…♡Y…】 fixtures
    stay valid. The redirected tmp config has no config.toml, so persona()
    would otherwise return the code defaults (U/A)."""
    from marrow import config

    real = config.persona
    monkeypatch.setattr(config, "persona",
                        lambda: {**real(),
                                 "user_marker": "N", "assistant_marker": "Y"})


@pytest.fixture(autouse=True)
def _disable_hooks_popen_detach(monkeypatch, request):
    if "no_popen_patch" in request.keywords:
        return
    try:
        from marrow import hooks
        monkeypatch.setattr(hooks, "popen_detach", lambda *a, **kw: None)
    except ImportError:
        pass
