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
   invoke `hooks.main(['session_*'])`, which fires `popen_detach`. The
   child subprocess loads REAL config (monkeypatch is in-process only)
   and would write to the real db. Neutering the hook-side reference
   keeps tests isolated. The direct popen_detach contract test imports
   from `marrow.popen_detach` and is unaffected.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _redirect_marrow_data_dir(tmp_path_factory):
    """Redirect marrow.config DATA_DIR/CONFIG_PATH to a session tmp dir.

    Patches the module object directly so `from marrow import config;
    config.db_path()` returns the test path. Survives reimports because
    the attribute lives on the module singleton.
    """
    from marrow import config

    tmp = tmp_path_factory.mktemp("marrow-data")
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DATA_DIR", tmp)
    mp.setattr(config, "CONFIG_PATH", tmp / "config.toml")
    yield tmp
    mp.undo()


@pytest.fixture(autouse=True)
def _disable_hooks_popen_detach(monkeypatch, request):
    if "no_popen_patch" in request.keywords:
        return
    try:
        from marrow import hooks
        monkeypatch.setattr(hooks, "popen_detach", lambda *a, **kw: None)
    except ImportError:
        pass
    try:
        from marrow import sessionstart_catchup
        monkeypatch.setattr(sessionstart_catchup, "popen_detach", lambda *a, **kw: None)
    except ImportError:
        pass
