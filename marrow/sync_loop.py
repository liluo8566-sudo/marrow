"""Sync loop — periodic md↔db reconcile/render for subpages + dashboard.

5s tick: for each target (subpage + dashboard.md), compare md mtime vs
db mtime. md newer → reconcile; db newer → render. Race防御: re-check md
mtime after reconcile; if it advanced, skip render this tick.

Runs in its own thread (SQLite WAL handles concurrent reads). Boot tick
fires immediately after _reconcile_boot to catch drift while watcher was
down. Clean shutdown via threading.Event.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from . import config, repo

_SYNC_TICK_S = float(os.environ.get("MARROW_SYNC_TICK_S", "5.0"))
_MTIME_EPSILON_S = 1.0  # jitter guard
# If md was touched within this window, skip render this tick — protects
# user keystrokes from inserter force_sort_consistency bootstrap rewriting
# the file under the cursor (atlas.md "modified externally" toast).
USER_ACTIVE_WINDOW_S = 3.0

log = logging.getLogger("marrow.watcher")


# ---------------------------------------------------------------------------
# db_mtime helpers — one per subpage key + dashboard
# ---------------------------------------------------------------------------

def _max_updated(conn: sqlite3.Connection, table: str,
                 col: str = "updated_at") -> float | None:
    """Return max(col) from table as POSIX float, or None if table empty/absent."""
    try:
        row = conn.execute(
            f"SELECT max({col}) FROM {table}"  # noqa: S608 — internal table names
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    ts = row[0]
    # ISO 8601 UTC: "2026-05-27T21:17:00Z" or without Z
    try:
        import datetime
        ts_clean = ts.rstrip("Z").replace("T", " ")
        dt = datetime.datetime.fromisoformat(ts_clean)
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except (ValueError, AttributeError):
        return None


def _max_any(conn: sqlite3.Connection,
             specs: list[tuple[str, str]]) -> float | None:
    """Return max mtime across multiple (table, col) pairs. None if all miss."""
    vals = [_max_updated(conn, t, c) for t, c in specs]
    valid = [v for v in vals if v is not None]
    return max(valid) if valid else None


# Mapping from subpage key → (table, updated_at_col) pairs.
# Only tables with a reliable timestamp column included.
_SUBPAGE_DB_SOURCES: dict[str, list[tuple[str, str]]] = {
    "profile":   [("entities", "created_at")],
    "milestone": [("milestones", "updated_at")],
    "diary":     [("diary", "updated_at")],
    "memes":     [("memes", "created_at")],
    "stickers":  [("stickers", "updated_at")],
    "wallet":    [],  # no table yet
    "study":     [("tasks", "updated_at")],
    "projects":  [("tasks", "updated_at")],
    "cheatsheet": [],  # disk is SoT, skip
    "atlas":     [("atlas", "updated_at")],
}

# Atlas-sweep tick: independent of the 5s md/db sync. Runs atlas_sweep_fs
# every N seconds to pick up new dirs and mark stale ones.
# S2b sync_loop integration wires this after merge; scaffold lives here.
_ATLAS_SWEEP_TICK_S = float(os.environ.get("MARROW_ATLAS_SWEEP_TICK_S", "60.0"))

# Tables that feed dashboard.md. No separate milestone_candidate or monitor
# table exists in current schema; milestones covers candidates.
_DASHBOARD_DB_SOURCES: list[tuple[str, str]] = [
    ("affect",          "created_at"),
    ("tasks",           "updated_at"),
    ("milestones",      "updated_at"),
    ("alerts",          "created_at"),
    ("session_digests", "ts"),
    ("diary",           "updated_at"),
]

# Timeline-only subset feeding daybrief.md. Restricted to what
# reconcile_timeline / render_timeline actually touch: session_digests (ts —
# always populated, unlike the mostly-NULL updated_at), diary, events
# (created_at catches new tl lines; in-place tl edits ride collect_tick's
# 30min render), affect (open-episode rows the plan calls "episodes").
# Usage / rate-limit kv is EXCLUDED on purpose — Status-zone freshness rides
# collect_tick, not the 5s loop, so its per-render churn cannot defeat the gate.
_DAYBRIEF_DB_SOURCES: list[tuple[str, str]] = [
    ("session_digests", "ts"),
    ("diary",           "updated_at"),
    ("events",          "created_at"),
    ("affect",          "updated_at"),
]


def last_db_mtime_subpage(conn: sqlite3.Connection, key: str) -> float | None:
    """Return max db timestamp for a named subpage key, or None if unknown/empty."""
    sources = _SUBPAGE_DB_SOURCES.get(key)
    if not sources:
        return None
    return _max_any(conn, sources)


def last_db_mtime_dashboard(conn: sqlite3.Connection) -> float | None:
    """Return max db timestamp across all dashboard-feeding tables."""
    return _max_any(conn, _DASHBOARD_DB_SOURCES)


def last_db_mtime_daybrief(conn: sqlite3.Connection) -> float | None:
    """Return max db timestamp across the timeline-only daybrief sources."""
    return _max_any(conn, _DAYBRIEF_DB_SOURCES)


# ---------------------------------------------------------------------------
# Per-target descriptor
# ---------------------------------------------------------------------------

class SyncTarget:
    """One sync target: a subpage or dashboard.md."""

    def __init__(
        self,
        name: str,
        md_path: str,
        db_mtime_fn: Callable[[sqlite3.Connection], float | None],
        render_fn: Callable[[sqlite3.Connection], None],
        has_md_to_db: bool = True,
    ) -> None:
        self.name = name
        self.md_path = md_path
        self.db_mtime_fn = db_mtime_fn
        self.render_fn = render_fn
        self.has_md_to_db = has_md_to_db


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

class SyncLoop:
    """Periodic md↔db sync loop. Call start() once, stop() on shutdown."""

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection],
                 targets: list[SyncTarget],
                 tick_s: float = _SYNC_TICK_S) -> None:
        self._conn_factory = conn_factory
        self._conn: sqlite3.Connection | None = None
        self._targets = targets
        self._tick_s = tick_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_fails: dict[str, int] = {}

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="marrow-sync-loop", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        self._conn = self._conn_factory()
        try:
            # Boot tick fires immediately (catch drift while watcher was down).
            self._tick()
            while not self._stop.wait(self._tick_s):
                self._tick()
        finally:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def _tick(self) -> None:
        for target in self._targets:
            try:
                self._process(target)
                self._consecutive_fails[target.name] = 0
            except Exception:  # noqa: BLE001
                log.exception("sync_loop error processing %s", target.name)
                fails = self._consecutive_fails.get(target.name, 0) + 1
                if fails >= 3:
                    repo.add_alert(
                        "warn", "sync_loop",
                        f"sync_loop_tick_failed:{target.name}",
                        source="watcher.py",
                        message=(f"3+ consecutive tick failures for "
                                 f"{target.name}"),
                        db=config.db_path(),
                    )
                    fails = 0
                self._consecutive_fails[target.name] = fails

    def _process(self, target: SyncTarget) -> None:
        md_path = target.md_path
        conn = self._conn

        # Step 1-2: md path + mtime
        try:
            md_mtime = Path(md_path).stat().st_mtime
        except FileNotFoundError:
            return  # file missing — skip this tick

        # Step 3: db mtime
        db_mtime = target.db_mtime_fn(conn)
        if db_mtime is None:
            return  # no table / empty — skip

        log.debug("sync_loop %s md=%.3f db=%.3f", target.name, md_mtime, db_mtime)

        now = time.time()
        user_active = (now - md_mtime) < USER_ACTIVE_WINDOW_S

        if md_mtime > db_mtime + _MTIME_EPSILON_S:
            # Step 4: md newer → render (write_subpage owns reconcile internally).
            # has_md_to_db=False signals this target has no md→db direction; skip.
            if not target.has_md_to_db:
                return
            # User-active guard: skip if md touched within USER_ACTIVE_WINDOW_S.
            # Prevents inserter force_sort_consistency bootstrap from
            # rewriting the file under the user's cursor.
            if user_active:
                log.debug("sync_loop %s skip md→db: md touched %.2fs ago (user typing)",
                          target.name, now - md_mtime)
                return
            target.render_fn(conn)
            # Race防御: re-check md mtime after render
            try:
                md_mtime_after = Path(md_path).stat().st_mtime
            except FileNotFoundError:
                return
            if md_mtime_after > md_mtime + 0.01:
                log.debug("sync_loop %s md edited during render; next tick absorbs it",
                          target.name)

        elif db_mtime > md_mtime:
            # Step 5: db newer → render. No epsilon here — any db change
            # must reflect in md within the next tick. The content-equality
            # guard inside atomic_write swallows no-op renders so this
            # cannot loop on "db_mtime tiny bit ahead but content identical".
            # write_subpage's internal reconcile handles any md edit mid-tick.
            # Same user-active guard: db→md render also goes through inserter
            # which can bootstrap-rewrite on sort drift.
            if user_active:
                log.debug("sync_loop %s skip db→md: md touched %.2fs ago (user typing)",
                          target.name, now - md_mtime)
                return
            target.render_fn(conn)

        # else: md_mtime ≥ db_mtime within md→db epsilon — skip


# ---------------------------------------------------------------------------
# Atlas sweep scaffold — wired to watcher boot + 60s tick (S2b integrates)
# ---------------------------------------------------------------------------

class AtlasSweepLoop:
    """Runs atlas_sweep_fs on a periodic tick, owning its own connection."""

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection],
                 tick_s: float = _ATLAS_SWEEP_TICK_S) -> None:
        self._conn_factory = conn_factory
        self._conn: sqlite3.Connection | None = None
        self._tick_s = tick_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="marrow-atlas-sweep", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def sweep_once(self) -> dict[str, int]:
        """Run one sweep pass and return counts (uses current thread conn)."""
        from .atlas import atlas_sweep_fs
        return atlas_sweep_fs(self._conn)

    def _run(self) -> None:
        self._conn = self._conn_factory()
        try:
            # Boot sweep fires immediately
            self._safe_sweep()
            while not self._stop.wait(self._tick_s):
                self._safe_sweep()
        finally:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def _safe_sweep(self) -> None:
        try:
            self.sweep_once()
        except Exception:  # noqa: BLE001
            log.exception("atlas sweep error")


# ---------------------------------------------------------------------------
# Usage snapshot tick — carrier for marrow.usage_snapshot (5h/7d/cdx/today).
# Runs inside the watcher (always alive) so the numbers stay fresh regardless
# of whether cortex is running; cortex's own tick may also call it — the
# write is an upsert, so a duplicate call from either side is harmless.
# ---------------------------------------------------------------------------

def _usage_snapshot_tick_s() -> float:
    from . import config as _config
    return float((_config.load().get("cortex_usage", {}) or {}).get(
        "snapshot_tick_s", 300) or 300)


class UsageSnapshotLoop:
    """Runs usage_snapshot.fetch_and_write on a periodic tick. Owns no DB
    connection itself (fetch_and_write opens its own) — tick_s only."""

    def __init__(self, tick_s: float | None = None) -> None:
        self._tick_s = tick_s if tick_s is not None else _usage_snapshot_tick_s()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="marrow-usage-snapshot", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # Boot tick fires immediately so a watcher restart refreshes right away.
        self._safe_snapshot()
        while not self._stop.wait(self._tick_s):
            self._safe_snapshot()

    def _safe_snapshot(self) -> None:
        from . import usage_snapshot
        try:
            usage_snapshot.fetch_and_write()
        except usage_snapshot.UsageSnapshotError as e:
            log.debug("usage_snapshot tick: %s", e)
        except Exception:  # noqa: BLE001
            log.exception("usage_snapshot tick error")


# ---------------------------------------------------------------------------
# Factory: build targets from _REGISTRY + dashboard
# ---------------------------------------------------------------------------

def build_targets(folder: str,
                  state_dir: str,
                  dashboard_path: str) -> list[SyncTarget]:
    """Build SyncTarget list from subpages._REGISTRY + dashboard."""
    from . import config as _config
    from . import storage, subpages
    from .dashboard import write_dashboard

    targets: list[SyncTarget] = []

    # Subpage targets — use a short-lived conn just for config building
    conn = storage.connect()
    try:
        cfgs = subpages.build_all_configs(conn, folder=folder, state_dir=state_dir)
    except Exception:
        log.exception("sync_loop: build_all_configs failed")
        cfgs = []
    finally:
        conn.close()

    for cfg in cfgs:
        key = cfg.key
        md_path = cfg.path

        def _make_db_mtime_fn(k: str):
            def _fn(c: sqlite3.Connection) -> float | None:
                return last_db_mtime_subpage(c, k)
            return _fn

        def _make_render_fn(c_cfg):
            def _fn(c: sqlite3.Connection) -> None:
                from .subpages import write_subpage
                write_subpage(c_cfg, c)
            return _fn

        targets.append(SyncTarget(
            name=f"subpage:{key}",
            md_path=md_path,
            db_mtime_fn=_make_db_mtime_fn(key),
            render_fn=_make_render_fn(cfg),
            has_md_to_db=cfg.reconcile is not None,
        ))

    # Dashboard target
    _sd = state_dir

    def _dash_db_mtime(c: sqlite3.Connection) -> float | None:
        return last_db_mtime_dashboard(c)

    def _dash_render(c: sqlite3.Connection) -> None:
        write_dashboard(dashboard_path, c, state_dir=_sd)

    targets.append(SyncTarget(
        name="dashboard",
        md_path=dashboard_path,
        db_mtime_fn=_dash_db_mtime,
        render_fn=_dash_render,
        has_md_to_db=True,
    ))

    # Daybrief target — timeline zone is bidirectional. render_fn is
    # daybrief.update, which reconciles md hand-edits BEFORE rendering (P2),
    # so the loop must NOT reconcile again. db_mtime_fn is the timeline-only
    # subset. Missing file (fresh install) is skipped gracefully: _process
    # returns on stat() FileNotFoundError, and update() guards os.path.exists.
    try:
        daybrief_path = (_config.daybrief_path() or "").strip()
    except KeyError:
        daybrief_path = ""
    if daybrief_path:
        def _daybrief_db_mtime(c: sqlite3.Connection) -> float | None:
            return last_db_mtime_daybrief(c)

        def _daybrief_render(c: sqlite3.Connection) -> None:
            from . import daybrief
            daybrief.update(c)

        targets.append(SyncTarget(
            name="daybrief",
            md_path=daybrief_path,
            db_mtime_fn=_daybrief_db_mtime,
            render_fn=_daybrief_render,
            has_md_to_db=True,
        ))

    return targets
