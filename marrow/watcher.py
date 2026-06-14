"""marrow watcher — keep md_index in sync with dashboard.md, db-pages/.

Boot: full_scan reconcile (covers crash gap) -> persistent watchdog.Observer
on three roots. Edits are debounced 200ms per (path, key) to dedup OS event
storms (one save = 5-7 raw events on macOS). Hash-compare via md_index; no
mute-lock timing — auto-writer + watcher race-safe by construction.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import re
import sqlite3

import sqlite_vec

from . import config, storage
from .drift_sweep import AUTHORIZED_ROOTS, EXCLUDE_DIRS_SCAN, DriftWatcher
from .md_index import MdIndex
from .sticker_ops import STICKERS_DIR, ingest_sticker, sweep_orphans, sweep_file_orphans
from .sync_loop import AtlasSweepLoop, SyncLoop, build_targets

_DEBOUNCE_S = 0.2
_LOG_NAME = "watcher.log"


def _logs_dir() -> Path:
    d = Path(config.DATA_DIR) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _setup_logger() -> logging.Logger:
    log = logging.getLogger("marrow.watcher")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    handler = logging.handlers.TimedRotatingFileHandler(
        _logs_dir() / _LOG_NAME, when="midnight", backupCount=7, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    log.addHandler(handler)
    log.propagate = False
    return log


def _resolve_roots() -> tuple[list[str], list[str]]:
    """Returns (file_roots, dir_roots) — both as absolute strings."""
    cfg = config.load()
    dash = str(Path(cfg["paths"]["dashboard"]).resolve())
    db_pages = str(Path(cfg["paths"]["db_pages"]).resolve())
    return [dash], [db_pages]


class _Debouncer:
    """Coalesce repeat events per key inside _DEBOUNCE_S window."""

    def __init__(self, delay: float, fire) -> None:
        self.delay = delay
        self.fire = fire
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def trigger(self, key: str, *args) -> None:
        with self._lock:
            t = self._timers.get(key)
            if t is not None:
                t.cancel()
            t = threading.Timer(self.delay, self._run, args=(key, args))
            t.daemon = True
            self._timers[key] = t
            t.start()

    def _run(self, key: str, args: tuple) -> None:
        with self._lock:
            self._timers.pop(key, None)
        try:
            self.fire(*args)
        except Exception:  # noqa: BLE001 — log + survive; never kill the observer
            logging.getLogger("marrow.watcher").exception(
                "debounced fire failed: key=%s args=%r", key, args)

    def flush(self) -> None:
        with self._lock:
            for t in list(self._timers.values()):
                t.cancel()
            self._timers.clear()


class _MdHandler(FileSystemEventHandler):
    """One handler instance per watcher run. Routes via debouncer.

    `watched_files` = file-mode targets (full paths). `watched_dirs` = roots
    monitored recursively. A path qualifies if it ends in .md AND either
    matches a watched file OR sits under a watched dir. This keeps the
    file-mode filter from rejecting dir-mode siblings inside the same parent.
    """

    def __init__(self, store: MdIndex, watched_files: set[str],
                 watched_dirs: set[str], debouncer: _Debouncer,
                 log: logging.Logger) -> None:
        self.store = store
        self.watched_files = watched_files
        self.watched_dirs = watched_dirs
        self.debouncer = debouncer
        self.log = log

    _DETAIL_SUFFIXES = (
        os.sep + "study" + os.sep,   # db-pages/study/<unit>.md
        os.sep + "projects" + os.sep, # db-pages/projects/<page>.md
    )
    _DETAIL_INDEX_NAMES = (
        "study.md",
        "projects.md",
        "dashboard.md",
    )

    def _is_detail_page(self, path: str) -> bool:
        """True for db-pages/{study,projects}/*.md detail pages; index pages stay watched."""
        # study/<unit>.md and projects/<name>.md (incl. pit.md) flow through
        # inserter or user edits, not md_index. study.md/projects.md/dashboard.md
        # remain candidates.
        basename = os.path.basename(path)
        if basename in self._DETAIL_INDEX_NAMES:
            return False
        for suffix in self._DETAIL_SUFFIXES:
            if suffix in path:
                return True
        return False

    def _candidate(self, path: str) -> bool:
        if not path.endswith(".md"):
            return False
        if self._is_detail_page(path):
            return False
        if path in self.watched_files:
            return True
        for d in self.watched_dirs:
            if path == d or path.startswith(d + os.sep):
                return True
        return False

    def on_modified(self, event) -> None:
        if event.is_directory:
            return
        path = str(Path(event.src_path).resolve())
        if not self._candidate(path):
            return
        self.debouncer.trigger(path, path)

    def on_created(self, event) -> None:
        self.on_modified(event)

    def on_moved(self, event) -> None:
        if event.is_directory:
            return
        src = str(Path(event.src_path).resolve())
        dst = str(Path(event.dest_path).resolve())
        if self._candidate(src):
            self.debouncer.trigger(src, src)
        if self._candidate(dst):
            self.debouncer.trigger(dst, dst)

    def on_deleted(self, event) -> None:
        if event.is_directory:
            return
        path = str(Path(event.src_path).resolve())
        if not self._candidate(path):
            return
        self.debouncer.trigger(path, path)


class _DriftHandler(FileSystemEventHandler):
    """Watchdog bridge → DriftWatcher event methods.

    First gate: pre-enqueue noise filter (drift_sweep._path_excluded) drops
    `.git/`, `__pycache__/`, `.venv*`, `node_modules/`, `drift_pending/`,
    `drift_backup/`, `logs/`, `archives/` events and atomic-write artefacts
    (`*.tmp.<N>.<hex>`, `.mrw.<token>`, `*.pyc.<N>`) BEFORE they reach
    DriftWatcher. Otherwise every git commit / pytest run / venv touch
    floods the batch and drowns real renames.
    """

    def __init__(self, drift: DriftWatcher, log: logging.Logger) -> None:
        self._drift = drift
        self._log = log

    def on_moved(self, event) -> None:
        if event.is_directory:
            try:
                from .atlas import rekey_paths
                conn = storage.connect()
                try:
                    rekey_paths(conn, [(event.src_path, event.dest_path)])
                finally:
                    conn.close()
            except Exception:
                self._log.exception("atlas rekey on dir mv failed: %s → %s",
                                    event.src_path, event.dest_path)
            # Bug 4: dir rename also feeds drift_sweep so refs to the old
            # basename (in python / configs / md) get queued + alert fires.
            # Skip renames whose basename is in EXCLUDE_DIRS_SCAN
            # (.git, __pycache__, node_modules, ...) — those never carry
            # path references worth scanning.
            src_name = os.path.basename(event.src_path.rstrip(os.sep))
            if src_name in EXCLUDE_DIRS_SCAN:
                return
            try:
                self._drift.on_moved(event.src_path, event.dest_path)
            except Exception:
                self._log.exception(
                    "drift on_moved (dir) failed: %s → %s",
                    event.src_path, event.dest_path,
                )
            return
        from .drift_sweep import _path_excluded
        if _path_excluded(event.src_path) or _path_excluded(event.dest_path):
            return
        try:
            self._drift.on_moved(event.src_path, event.dest_path)
        except Exception:
            self._log.exception("drift on_moved failed: %s → %s",
                                event.src_path, event.dest_path)

    def on_deleted(self, event) -> None:
        if event.is_directory:
            return
        from .drift_sweep import _path_excluded
        if _path_excluded(event.src_path):
            return
        try:
            self._drift.on_deleted(event.src_path)
        except Exception:
            self._log.exception("drift on_deleted failed: %s", event.src_path)

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        from .drift_sweep import _path_excluded
        if _path_excluded(event.src_path):
            return
        try:
            self._drift.on_created(event.src_path)
        except Exception:
            self._log.exception("drift on_created failed: %s", event.src_path)


_STICKER_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_STK_RE = re.compile(r"^stk_\d{3,}", re.IGNORECASE)
_STICKER_DEBOUNCE_S = 1.5


class _StickerHandler(FileSystemEventHandler):

    def __init__(self, log: logging.Logger) -> None:
        self._log = log
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _skip(self, path: str) -> bool:
        p = Path(path)
        if p.suffix.lower() not in _STICKER_EXTS:
            return True
        name = p.name
        if name.startswith("."):
            return True
        parts = p.parts
        if "_thumb" in parts:
            return True
        return False

    def _schedule(self, path: str) -> None:
        if self._skip(path):
            return
        with self._lock:
            t = self._pending.get(path)
            if t is not None:
                t.cancel()
            t = threading.Timer(_STICKER_DEBOUNCE_S, self._ingest, args=(path,))
            t.daemon = True
            self._pending[path] = t
            t.start()

    def _ingest(self, path: str) -> None:
        with self._lock:
            self._pending.pop(path, None)
        p = Path(path)
        if not p.exists():
            return
        size_a = p.stat().st_size
        time.sleep(0.5)
        if not p.exists():
            return
        size_b = p.stat().st_size
        if size_a != size_b:
            self._schedule(path)
            return
        conn = storage.connect()
        try:
            result = ingest_sticker(conn, path, desc="(pending)", source="finder")
            if result.get("duplicate"):
                self._log.info("sticker_ingest duplicate skipped: %s -> id=%s",
                               path, result.get("existing_id"))
            else:
                self._log.info("sticker_ingest new: %s -> id=%s path=%s",
                               path, result.get("id"), result.get("path"))
            if p.exists() and p.resolve() != Path(result.get("path", "")).resolve():
                p.unlink()
                self._log.info("sticker_ingest cleaned source: %s", path)
        except Exception:
            self._log.exception("sticker_ingest failed: %s", path)
        finally:
            conn.close()

    def _sweep(self) -> None:
        conn = storage.connect()
        try:
            removed = sweep_orphans(conn)
            if removed:
                self._log.info("sticker orphan sweep removed ids: %s", removed)
        except Exception:
            self._log.exception("sticker orphan sweep failed")
        finally:
            conn.close()

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        self._schedule(str(Path(event.src_path).resolve()))

    def on_deleted(self, event) -> None:
        if event.is_directory:
            return
        p = Path(event.src_path)
        if _STK_RE.match(p.name):
            threading.Timer(1.0, self._sweep).start()

    def on_moved(self, event) -> None:
        if event.is_directory:
            return
        self._schedule(str(Path(event.dest_path).resolve()))


def _warmup_imports() -> None:
    """Force-load every stdlib + marrow module the worker threads may touch.

    Mitigates a macOS 26 / Python 3.14 SIGBUS (FS pagein error 22) seen when
    multiple daemon threads concurrently trigger dyld page-in of a stdlib .so.
    Pre-importing on the main thread means worker threads only hit
    `sys.modules` lookups — no concurrent dlopen.
    """
    import grp  # noqa: F401
    import pwd  # noqa: F401
    import tempfile  # noqa: F401
    import shutil  # noqa: F401
    import subprocess  # noqa: F401
    import urllib.parse  # noqa: F401
    import hashlib  # noqa: F401
    import sqlite3  # noqa: F401

    from . import (  # noqa: F401
        atlas, candidates, config, dashboard, drift_sweep, entity_recall,
        inserter, md_index, recall, reconcile, repo, storage, subpage_specs,
        subpages, subpages_render, top_sections,
    )


class Watcher:
    """Lifecycle wrapper. Build, start, join, stop."""

    def __init__(self) -> None:
        self.log = _setup_logger()
        _warmup_imports()
        self.file_roots, self.dir_roots = _resolve_roots()
        # Schema first (default conn, main thread). Then reopen with
        # check_same_thread=False for the debouncer worker threads.
        storage.init_db().close()
        db_path = config.db_path()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            db_path, timeout=30.0, check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.store = MdIndex(self.conn)
        # File-mode and dir-mode tracking. Filled during attach().
        self.watched_files: set[str] = set()
        self.watched_dirs: set[str] = set()
        self.observer = Observer()
        self.debouncer = _Debouncer(_DEBOUNCE_S, self._fire_sync)
        self._stop = threading.Event()
        self._sync_loop: SyncLoop | None = None
        self._atlas_sweep: AtlasSweepLoop | None = None
        self.drift_watcher = DriftWatcher(roots=list(AUTHORIZED_ROOTS))

    def _fire_sync(self, path: str) -> None:
        # observe-only — keep auto-write baseline frozen so the dashboard
        # inserter can detect user edits on the next render. Hand-edit
        # debounce fires → block_id stays in md_index but content_hash
        # baseline is NOT updated → dashboard._resolve_blocks sees stored
        # != cur_hash → preserves user body.
        # Each debouncer worker thread opens its own conn to avoid sharing
        # SQLite connection state across threads.
        conn = storage.connect()
        try:
            store = MdIndex(conn)
            report = store.sync_file_observe(path)
            if report.inserted or report.updated or report.tombstoned or report.cleared:
                self.log.info(
                    "sync %s inserted=%d updated=%d tombstoned=%d cleared=%d",
                    path, report.inserted, report.updated,
                    report.tombstoned, report.cleared,
                )
        finally:
            conn.close()

    def _attach_dir(self, handler: _MdHandler, root: str) -> None:
        if not Path(root).is_dir():
            self.log.warning("dir root missing, skipped: %s", root)
            return
        self.watched_dirs.add(root)
        self.observer.schedule(handler, root, recursive=True)
        self.log.info("watching dir %s", root)

    def _attach_file(self, handler: _MdHandler, path: str) -> None:
        # watchdog can only watch directories; we watch the parent + filter.
        parent = str(Path(path).parent)
        if not Path(parent).is_dir():
            self.log.warning("file parent missing, skipped: %s", path)
            return
        self.watched_files.add(path)
        self.observer.schedule(handler, parent, recursive=False)
        self.log.info("watching file %s", path)

    def _reconcile_boot(self) -> None:
        # Boot scan = observe-only. Hand-edits made while the watcher was
        # down must not collapse the auto-write baseline; the next inserter
        # pass needs `stored != cur_hash` to recognise them as user edits.
        roots = self.file_roots + self.dir_roots
        report = self.store.full_scan(roots, observe=True)
        self.log.info(
            "boot full_scan scanned_files=%d inserted=%d updated=%d "
            "tombstoned=%d cleared=%d", report.scanned_files,
            report.inserted, report.updated, report.tombstoned, report.cleared,
        )
        if report.files_without_markers:
            # Warn once per path, not per scan tick.
            self.log.warning(
                "files without id markers (skipped): %s",
                ", ".join(report.files_without_markers[:10]),
            )

    def run(self) -> None:
        self.log.info("watcher pid=%d starting; roots=%s",
                      os.getpid(), self.file_roots + self.dir_roots)
        self._reconcile_boot()
        handler = _MdHandler(self.store, self.watched_files,
                             self.watched_dirs, self.debouncer, self.log)
        for d in self.dir_roots:
            self._attach_dir(handler, d)
        for f in self.file_roots:
            self._attach_file(handler, f)
        # Build watched_files set BEFORE we start — handler already holds the
        # reference, so additions land in time. (_MdHandler reads it on each
        # event.)
        # Attach DriftWatcher to each AUTHORIZED_ROOTS dir that exists.
        drift_handler = _DriftHandler(self.drift_watcher, self.log)
        for root in AUTHORIZED_ROOTS:
            if root.is_dir():
                self.observer.schedule(drift_handler, str(root), recursive=True)
                self.log.info("drift_watcher watching %s", root)

        stickers_dir = STICKERS_DIR.expanduser().resolve()
        stickers_dir.mkdir(parents=True, exist_ok=True)
        conn = storage.connect()
        try:
            registered = sweep_file_orphans(conn)
            if registered:
                self.log.info("sticker boot sweep registered orphans: %s", registered)
        except Exception:
            self.log.exception("sticker boot sweep failed")
        finally:
            conn.close()
        sticker_handler = _StickerHandler(self.log)
        self.observer.schedule(sticker_handler, str(stickers_dir), recursive=False)
        self.log.info("sticker_ingest watching %s", stickers_dir)

        self.observer.start()
        # Start sync loop — boot tick fires immediately (after _reconcile_boot)
        # to catch drift while watcher was down.
        try:
            cfg = config.load()
            folder = cfg["paths"]["db_pages"]
            state_dir = cfg["paths"]["db_pages_state"]
            dash = cfg["paths"]["dashboard"]
            targets = build_targets(folder, state_dir, dash)
            self._sync_loop = SyncLoop(storage.connect, targets)
            self._sync_loop.start()
            self._atlas_sweep = AtlasSweepLoop(storage.connect)
            self._atlas_sweep.start()
            self.log.info("sync_loop started targets=%d", len(targets))
        except Exception:
            self.log.exception("sync_loop failed to start; watcher continues without it")
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        finally:
            self.log.info("watcher stopping")
            if self._sync_loop is not None:
                self._sync_loop.stop()
            if self._atlas_sweep is not None:
                self._atlas_sweep.stop()
            self.debouncer.flush()
            self.observer.stop()
            self.observer.join(timeout=5)
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _on_signal(self, *_args) -> None:
        self._stop.set()


def main() -> int:
    Watcher().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
