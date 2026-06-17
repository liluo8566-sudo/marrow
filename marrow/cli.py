"""`mw` — deterministic point-edit/remove of one record by id. No LLM.

Thin shell over storage.py. CLI is the public surface; main(argv) returns
an exit code. Hooks/daemon never call here — they use repo.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import shlex
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from . import config, dashboard, repo, storage, subpages


PROTECTED = {"id", "created_at", "updated_at", "source_hash", "occurred_at"}
# diary is keyed by its TEXT date column, not an integer id.
_KEY = {"diary": "date"}


def _key(table: str) -> str:
    return _KEY.get(table, "id")


@contextmanager
def _conn(db: str | None):
    conn = storage.connect(db or config.db_path())
    try:
        yield conn
    finally:
        conn.close()


def _columns(conn, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


def _fail(msg: str) -> int:
    print(f"mw: {msg}", file=sys.stderr)
    return 1


def _audit(conn, table: str, tid: str, action: str, summary: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (target_table, target_id, action, summary) "
        "VALUES (?, ?, ?, ?)",
        (table, tid, action, summary),
    )


def _write(conn, table: str, key_id: str, sql: str, params: tuple,
           action: str, summary: str) -> int:
    with conn:
        cur = conn.execute(sql, params)
        if cur.rowcount == 0:
            conn.rollback()
            return _fail(f"no {table} row with id {key_id}")
        _audit(conn, table, key_id, action, summary)
    return 0


def cmd_set(args) -> int:
    with _conn(args.db) as conn:
        cols = _columns(conn, args.table)
        if not cols:
            return _fail(f"unknown table: {args.table}")
        kc = _key(args.table)
        if args.field in PROTECTED or args.field == kc \
                or args.field not in cols:
            return _fail(f"field not editable: {args.table}.{args.field}")
        return _write(
            conn, args.table, args.id,
            f"UPDATE {args.table} SET {args.field} = ? WHERE {kc} = ?",
            (args.value, args.id), "update",
            f"{args.field}={args.value[:80]}",
        )


def cmd_rm(args) -> int:
    with _conn(args.db) as conn:
        if not _columns(conn, args.table):
            return _fail(f"unknown table: {args.table}")
        return _write(
            conn, args.table, args.id,
            f"DELETE FROM {args.table} WHERE {_key(args.table)} = ?",
            (args.id,), "delete", "removed via mw",
        )


def _shortcut(args, table: str, sql: str, summary: str) -> int:
    with _conn(args.db) as conn:
        return _write(conn, table, args.id, sql, (args.id,),
                      "update", summary)


def cmd_resolve(args) -> int:
    rc = _shortcut(
        args, "alerts",
        "UPDATE alerts SET resolved=1, "
        "resolved_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
        "resolved via mw",
    )
    if rc == 0:
        args.all = False
        cmd_refresh(args)
    return rc


def _pin_toggle(args, val: int, summary: str) -> int:
    with _conn(args.db) as conn:
        cols = _columns(conn, args.table)
        if not cols:
            return _fail(f"unknown table: {args.table}")
        if "pinned" not in cols:
            return _fail(f"{args.table} has no pinned column")
        kc = _key(args.table)
        return _write(
            conn, args.table, args.id,
            f"UPDATE {args.table} SET pinned=? WHERE {kc}=?",
            (val, args.id), "update", summary,
        )


def cmd_pin(args) -> int:
    return _pin_toggle(args, 1, "pinned via mw")


def cmd_unpin(args) -> int:
    return _pin_toggle(args, 0, "unpinned via mw")


def cmd_alerts_clear(args) -> int:
    """Bulk-resolve every unresolved alert. One audit_log row per id.

    The single-id `mw resolve <id>` path stays as-is for surgical use; this
    is the "wipe the board" shortcut so a stale legacy backlog doesn't
    require N keystrokes.
    """
    with _conn(args.db) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM alerts WHERE resolved=0"
        ).fetchall()]
        if not ids:
            print("mw: no unresolved alerts")
            return 0
        with conn:
            conn.execute(
                "UPDATE alerts SET resolved=1, "
                "resolved_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE resolved=0"
            )
            for aid in ids:
                _audit(conn, "alerts", str(aid), "update",
                       "resolved via mw alerts clear")
        print(f"cleared {len(ids)} alert(s): {','.join(str(i) for i in ids)}")
        return 0


def cmd_add_session(args) -> int:
    """B1: upsert one row in `sessions` so /resume reads the model back."""
    sid = (args.sid or "").strip()
    if not sid:
        return _fail("--sid required")
    repo.upsert_session(
        sid,
        args.model or None,
        args.channel or None,
        args.title or "",
        args.effort or "",
        last_active=getattr(args, "last_active", None) or None,
        db=args.db,
    )
    print(f"session {sid} model={args.model or '-'} channel={args.channel or '-'}")
    return 0


def cmd_get_session_model(args) -> int:
    """B1: print the persisted model for sid (empty when absent)."""
    sid = (args.sid or "").strip()
    if not sid:
        return _fail("--sid required")
    row = repo.get_session(sid, db=args.db)
    print((row or {}).get("model") or "")
    return 0


def cmd_get_session_cwd(args) -> int:
    """Print the persisted cwd for sid (empty when absent)."""
    sid = (args.sid or "").strip()
    if not sid:
        return _fail("--sid required")
    row = repo.get_session(sid, db=args.db)
    print((row or {}).get("cwd") or "")
    return 0


def cmd_get_session_created(args) -> int:
    sid = (args.sid or "").strip()
    if not sid:
        return _fail("--sid required")
    row = repo.get_session(sid, db=args.db)
    print((row or {}).get("created_at") or "")
    return 0


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def cmd_list_recent_sessions(args) -> int:
    """B6: print the N most-recent sessions, one per line."""
    include = _split_csv(getattr(args, "channels", None))
    exclude = _split_csv(getattr(args, "exclude_channels", None))
    if include and exclude:
        return _fail("--channels and --exclude-channels are mutually exclusive")
    rows = repo.list_recent_sessions(
        limit=max(1, args.limit),
        channels=include or None,
        exclude_channels=exclude or None,
        require_user_events=bool(getattr(args, "require_user_events", False)),
        db=args.db,
    )
    for r in rows:
        sid = r.get("sid") or ""
        model = r.get("model") or "-"
        channel = r.get("channel") or "-"
        cwd = r.get("cwd") or ""
        last = r.get("last_active") or "-"
        title = r.get("title") or ""
        effort = r.get("effort") or ""
        print(f"{sid}\t{model}\t{channel}\t{cwd}\t{last}\t{title}\t{effort}")
    return 0


def cmd_add_alert(args) -> int:
    sev = args.severity
    if sev not in {"warn", "critical"}:
        return _fail(f"severity must be warn|critical (got {sev})")
    fp = (args.fingerprint or "").strip()
    if not fp:
        return _fail("fingerprint required")
    msg = (args.message or "").strip() or None
    aid = repo.add_alert(
        sev, args.type, fp, args.source, message=msg, db=args.db,
    )
    detail = msg if msg is not None else fp
    print(f"alert #{aid} [{sev}/{args.type}] {detail[:80]}")
    return 0


def cmd_done(args) -> int:
    return _shortcut(
        args, "tasks",
        "UPDATE tasks SET status='done' WHERE id=?", "status=done",
    )


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _add_milestone(conn, args) -> int:
    if args.scope not in {"us", "me"}:
        return _fail(f"invalid scope: {args.scope} (us|me)")
    if not _DATE_RE.match(args.date):
        return _fail(f"invalid date: {args.date} (YYYY-MM-DD)")
    title = (args.title or "").strip()
    if not title:
        return _fail("title required")
    desc = args.description
    theme = args.theme
    pinned = 1 if args.pinned else 0
    src = "\x1f".join([args.scope, args.date, title, desc or ""])
    h = hashlib.sha256(src.encode()).hexdigest()
    with conn:
        cur = conn.execute(
            "INSERT INTO milestones "
            "(scope, date, title, description, theme, pinned, source_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (args.scope, args.date, title, desc, theme, pinned, h),
        )
        mid = cur.lastrowid
        _audit(conn, "milestones", str(mid), "insert",
               f"{args.scope}/{args.date}/{title[:60]}")
    print(f"milestone #{mid} [{args.scope}] {args.date} {title}")
    return 0


_ADD_TABLES = {"milestone": _add_milestone}


def cmd_sessionend(args) -> int:
    """mw sessionend rerun <sid> — force rerun sessionend_async, overwriting done marker."""
    if getattr(args, "sessionend_action", None) != "rerun":
        return _fail("usage: mw sessionend rerun <sid>")
    target_sid = args.sid
    if not target_sid:
        return _fail("mw sessionend rerun requires a sid argument")
    from .popen_detach import popen_detach
    with _conn(args.db) as conn:
        with conn:
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('events', ?, 'sessionend_extract', 'reset:mm_plus')",
                (target_sid,),
            )
    log = config.DATA_DIR / "logs" / f"sessionend_async_{target_sid}.log"
    popen_detach(
        [sys.executable, "-m", "marrow.sessionend_async", "--sid", target_sid,
         "--log-path", str(log)],
        log_path=log,
    )
    print(f"sessionend rerun queued for sid={target_sid} (log: {log})")
    return 0


def cmd_export_pit(args) -> int:
    """mw export-pit [--out PATH] — write pit table rows to a markdown file.

    Idempotent — overwrites the output file. Run once before dropping the pit
    table to preserve the content in a hand-managed markdown file.
    """
    from pathlib import Path as _Path
    from . import subpages_render as _sr
    default_out = str(_Path(config.sub_pages_path()) / "projects" / "pit.md")
    out_path = getattr(args, "out", None) or default_out
    with _conn(args.db) as conn:
        block = _sr.render_pit(conn)
    _Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    _Path(out_path).write_text(block + "\n", encoding="utf-8")
    print(f"pit exported to {out_path}")
    return 0


def cmd_atlas(args) -> int:
    """mw atlas <prefix> — query atlas rows by path prefix."""
    from . import atlas as _atlas_mod
    with _conn(args.db) as conn:
        rows = _atlas_mod.lookup_by_prefix(conn, args.prefix)
    if not rows:
        return 1
    for r in rows:
        print(f"{r['path']}  description={r.get('description') or ''}  "
              f"naming={r.get('naming_hint') or ''}  d={r.get('depth', 0)}")
    return 0


def cmd_doctor(args) -> int:
    from .install import _ALL_PLISTS, _MARROW_HOOKS, _SETTINGS, _VENV_PYTHON, _hook_command

    def ok(msg: str) -> None: print(f"  ✓ {msg}")

    def bad(msg: str) -> None:
        nonlocal issues
        issues += 1
        print(f"  ✗ {msg}")

    def hook_commands(settings: dict):
        for event, groups in settings.get("hooks", {}).items():
            if not isinstance(groups, list): continue
            for group in groups:
                if not isinstance(group, dict): continue
                matcher = group.get("matcher", "")
                for hook in group.get("hooks", []):
                    if isinstance(hook, dict) and hook.get("command"):
                        yield event, matcher, str(hook["command"])

    print("mw doctor")
    issues = 0

    if _VENV_PYTHON.is_file() and os.access(_VENV_PYTHON, os.X_OK):
        ok("venv: .venv/bin/python exists")
    else:
        bad("venv: .venv/bin/python missing or not executable")

    try:
        with sqlite3.connect(config.db_path()) as conn:
            conn.execute("SELECT 1").fetchone()
        ok("db: SELECT 1 ok")
    except Exception as e:
        bad(f"db: {e}")

    try:
        settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        settings = {}
        bad(f"settings missing: {_SETTINGS}")
    except json.JSONDecodeError as e:
        settings = {}
        bad(f"settings invalid JSON: {e}")

    actual = list(hook_commands(settings))
    for event, entries in _MARROW_HOOKS.items():
        for entry in entries:
            matcher = entry["matcher"]
            expected = _hook_command(entry["command"])
            group_cmds = [
                cmd for ev, ma, cmd in actual if ev == event and ma == matcher
            ]
            if expected in group_cmds:
                continue
            stale = next((cmd for cmd in group_cmds if "marrow.hooks" in cmd), None)
            label = f"{event}[{matcher}]" if matcher else event
            if stale:
                bad(f"hook stale: {label} → {stale}")
            else:
                bad(f"hook missing: {label} → {expected}")

    missing_execs = 0
    for _event, _matcher, command in actual:
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = []
        exe_str = parts[0] if parts else None
        exe = Path(exe_str).expanduser() if exe_str else None
        if exe is None or not (exe.is_file() or shutil.which(exe_str)):
            missing_execs += 1
            bad(f"hook executable missing: {command}")
    if missing_execs == 0:
        ok("all hook executables found")

    launch_agents = Path.home() / "Library" / "LaunchAgents"
    loaded = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    loaded_text = loaded.stdout if loaded.returncode == 0 else ""
    loaded_count = 0
    for _fname, label in _ALL_PLISTS:
        plist = launch_agents / f"{label}.plist"
        exists = plist.exists()
        is_loaded = label in loaded_text
        if exists and is_loaded: loaded_count += 1
        elif not exists:
            bad(f"plist missing: {label}")
        else:
            bad(f"plist not loaded: {label}")
    total = len(_ALL_PLISTS)
    if loaded_count == total:
        ok(f"{loaded_count}/{total} plists loaded")
    else:
        print(f"  ✗ {loaded_count}/{total} plists loaded")

    print()
    print(f"{issues} issue{'s' if issues != 1 else ''} found")
    return 0 if issues == 0 else 1


def cmd_drift_scan(args) -> int:
    """mw drift scan <old> <new> — manual one-shot trigger."""
    from .drift_sweep import handle_move
    pid = handle_move(args.old, args.new_path)
    if pid:
        print(f"drift queued: {pid}")
    else:
        print("drift: no refs found — nothing queued")
    return 0


def cmd_drift_apply(args) -> int:
    from .drift_sweep import apply_confirm
    result = apply_confirm(args.id)
    if not result["ok"]:
        return _fail(result.get("error", "apply failed"))
    changed = result.get("changed", [])
    print(f"drift apply {args.id}: {len(changed)} files updated")
    for f in changed:
        print(f"  {f}")
    return 0


def cmd_drift_reject(args) -> int:
    from .drift_sweep import apply_reject
    result = apply_reject(args.id)
    if not result["ok"]:
        return _fail(result.get("error", "reject failed"))
    print(f"drift reject {args.id}: pending discarded")
    return 0


def _refresh_scan(conn, *, include_subpages: bool) -> None:
    """Walk watched md files and OBSERVE each into md_index before re-render.

    Observe-only: brand-new block_ids get a first-sight baseline so the
    inserter knows they exist; existing block_ids whose body changed
    leave their content_hash baseline UNTOUCHED — that is the signal the
    dashboard inserter uses to recognise a user edit and preserve it.

    Always: dashboard.md. With include_subpages: db-pages.
    """
    from .md_index import MdIndex
    idx = MdIndex(conn)
    file_roots = [config.dashboard_path()]
    dir_roots = [config.sub_pages_path()] if include_subpages else []
    for f in file_roots:
        if Path(f).exists():
            idx.sync_file_observe(f)
    if dir_roots:
        idx.full_scan(dir_roots, observe=True)


def cmd_refresh(args) -> int:
    db = args.db or config.db_path()
    conn = storage.init_db(db)
    try:
        _refresh_scan(conn, include_subpages=args.all)
        dashboard.write_dashboard(
            config.dashboard_path(), conn,
            state_dir=str(config.DATA_DIR / "state"), db=db,
        )
        msg = "dashboard refreshed"
        if args.all:
            try:
                subpages.write_all_subpages(
                    conn, folder=config.sub_pages_path(),
                    state_dir=config.sub_pages_state_path(), db=db,
                )
                msg += " + subpages"
            except Exception as e:
                print(f"mw: subpages refresh failed: {e}", file=sys.stderr)
        _maybe_restart_watcher()
        print(msg)
        return 0
    finally:
        conn.close()


def _maybe_restart_watcher() -> None:
    """Restart watcher if any marrow .py is newer than its PID start time."""
    import subprocess, time as _time
    pkg_dir = Path(__file__).resolve().parent
    try:
        info = subprocess.run(
            ["launchctl", "list", _WATCHER_LABEL],
            capture_output=True, text=True,
        )
        if info.returncode != 0:
            return
        pid = None
        for line in info.stdout.splitlines():
            if '"PID"' in line:
                pid = int("".join(c for c in line if c.isdigit()))
                break
        if not pid:
            return
        ps = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True, text=True,
        )
        if ps.returncode != 0:
            return
        parts = ps.stdout.strip().replace("-", ":").split(":")
        secs = sum(int(x) * m for x, m in zip(reversed(parts), [1, 60, 3600, 86400]))
        proc_start = _time.time() - secs
    except Exception:
        return
    newest_py = max(
        (f.stat().st_mtime for f in pkg_dir.rglob("*.py")), default=0
    )
    if newest_py > proc_start:
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{_WATCHER_LABEL}"],
            capture_output=True,
        )


_WATCHER_LABEL = "com.marrow.watcher"
_DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
_WATCHER_PLIST_SRC = _DEPLOY_DIR / "mw-watcher.plist"
_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"

# All plist templates shipped in deploy/ with their launchd labels.
_ALL_PLISTS: list[tuple[str, str]] = [
    ("mw-aging.plist",         "com.marrow.aging"),
    ("mw-daily-catchup.plist", "com.marrow.daily-catchup"),
    ("mw-daily-routine.plist", "com.marrow.daily-routine"),
    ("mw-dashboard-tick.plist","com.marrow.dashboard-tick"),
    ("mw-db-backup.plist",     "com.marrow.db-backup"),
    ("mw-goose-bites.plist",   "com.marrow.goose-bites"),
    ("mw-watcher.plist",       "com.marrow.watcher"),
]


def _watcher_plist_target() -> Path:
    return _LAUNCH_AGENTS / "com.marrow.watcher.plist"


def _resolve_plist(template: str) -> str:
    """Substitute __TOKENS__ in a plist template string with live paths."""
    project_dir = _DEPLOY_DIR.parent
    venv_python = project_dir / ".venv" / "bin" / "python"
    venv_bin = str(venv_python.parent)
    log_dir = Path.home() / "Library" / "Logs"
    data_dir = config.DATA_DIR
    path_env = f"{Path.home() / '.local' / 'bin'}:{venv_bin}:/usr/bin:/bin"

    return (
        template
        .replace("__VENV_PYTHON__", str(venv_python))
        .replace("__PROJECT_DIR__", str(project_dir))
        .replace("__LOG_DIR__", str(log_dir))
        .replace("__DATA_DIR__", str(data_dir))
        .replace("__PATH_ENV__", path_env)
    )


def cmd_install_launchd(args) -> int:
    """Install all 7 marrow launchd agents from deploy/ templates."""
    _LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    domain = f"gui/{uid}"

    project_dir = _DEPLOY_DIR.parent
    venv_python = project_dir / ".venv" / "bin" / "python"
    log_dir = Path.home() / "Library" / "Logs"
    data_dir = config.DATA_DIR

    print(f"project dir : {project_dir}")
    print(f"venv python : {venv_python}")
    print(f"log dir     : {log_dir}")
    print(f"data dir    : {data_dir}")
    print()

    errors = 0
    for fname, label in _ALL_PLISTS:
        src = _DEPLOY_DIR / fname
        if not src.exists():
            print(f"  [skip] {fname} not found in deploy/")
            errors += 1
            continue
        resolved = _resolve_plist(src.read_text(encoding="utf-8"))
        tgt = _LAUNCH_AGENTS / fname.replace("mw-", "com.marrow.").replace(".plist", "").replace(".", "-") + ".plist"
        # Target name matches label: com.marrow.<service>.plist
        tgt = _LAUNCH_AGENTS / f"{label}.plist"
        tgt.write_text(resolved, encoding="utf-8")
        # Idempotent: bootout (tolerated), then bootstrap.
        _launchctl("bootout", domain, str(tgt))
        rc, msg = _launchctl("bootstrap", domain, str(tgt))
        status = "ok" if rc == 0 else f"FAILED: {msg}"
        print(f"  {label}: {status}")
        if rc != 0:
            errors += 1

    print()
    print(f"installed {len(_ALL_PLISTS) - errors}/{len(_ALL_PLISTS)} agents")
    return 0 if errors == 0 else 1


def cmd_uninstall_launchd(args) -> int:
    """Bootout and remove all 7 marrow launchd agents from LaunchAgents."""
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    domain = f"gui/{uid}"

    errors = 0
    for _fname, label in _ALL_PLISTS:
        tgt = _LAUNCH_AGENTS / f"{label}.plist"
        if not tgt.exists():
            print(f"  [skip] {tgt.name} not installed")
            continue
        rc, msg = _launchctl("bootout", domain, str(tgt))
        tgt.unlink(missing_ok=True)
        status = "removed" if rc == 0 else f"bootout failed ({msg}), file removed anyway"
        print(f"  {label}: {status}")

    print()
    print("uninstall complete")
    return 0


def _launchctl(*args: str) -> tuple[int, str]:
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _watcher_bootstrap() -> str:
    tgt = _watcher_plist_target()
    _LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_WATCHER_PLIST_SRC, tgt)
    # Idempotent: bootout first, ignore not-loaded; then bootstrap.
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    domain = f"gui/{uid}"
    _launchctl("bootout", domain, str(tgt))  # tolerated failure
    rc, msg = _launchctl("bootstrap", domain, str(tgt))
    if rc != 0:
        return f"bootstrap failed: {msg}"
    return f"bootstrapped {domain}/{_WATCHER_LABEL}"


def _watcher_kickstart() -> str:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    rc, msg = _launchctl("kickstart", "-k", f"gui/{uid}/{_WATCHER_LABEL}")
    return msg if rc != 0 else f"kickstarted {_WATCHER_LABEL}"


def _watcher_unload() -> str:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    rc, msg = _launchctl("bootout", f"gui/{uid}", str(_watcher_plist_target()))
    return msg if rc != 0 else f"unloaded {_WATCHER_LABEL}"


def _watcher_state() -> str:
    rc, msg = _launchctl("print", f"gui/{subprocess.run(['id','-u'], capture_output=True, text=True).stdout.strip()}/{_WATCHER_LABEL}")
    if rc != 0:
        return "not loaded"
    state = "unknown"
    pid = "-"
    for line in msg.splitlines():
        s = line.strip()
        if s.startswith("state ="):
            state = s.split("=", 1)[1].strip()
        elif s.startswith("pid ="):
            pid = s.split("=", 1)[1].strip()
    return f"state={state} pid={pid}"


def cmd_watcher(args) -> int:
    if args.action == "start":
        print(_watcher_bootstrap())
        print(_watcher_kickstart())
        return 0
    if args.action == "stop":
        print(_watcher_unload())
        return 0
    if args.action == "status":
        print(_watcher_state())
        log_path = config.DATA_DIR / "logs" / "watcher.log"
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
            print("--- last 5 log lines ---")
            for line in tail:
                print(line)
        else:
            print(f"(no log yet at {log_path})")
        return 0
    return _fail(f"unknown watcher action: {args.action}")


def cmd_add(args) -> int:
    fn = _ADD_TABLES.get(args.table)
    if fn is None:
        return _fail(f"add target not supported: {args.table}")
    with _conn(args.db) as conn:
        return fn(conn, args)


def cmd_show(args) -> int:
    with _conn(args.db) as conn:
        if not _columns(conn, args.table):
            return _fail(f"unknown table: {args.table}")
        row = conn.execute(
            f"SELECT * FROM {args.table} WHERE {_key(args.table)} = ?",
            (args.id,),
        ).fetchone()
        if row is None:
            return _fail(f"no {args.table} row with id {args.id}")
        for k in row.keys():
            print(f"{k}: {row[k]}")
        return 0


def cmd_ls(args) -> int:
    with _conn(args.db) as conn:
        cols = _columns(conn, args.table)
        if not cols:
            return _fail(f"unknown table: {args.table}")
        sql = f"SELECT * FROM {args.table}"
        params: list = []
        if args.status:
            if "status" not in cols:
                return _fail(f"{args.table} has no status column")
            sql += " WHERE status = ?"
            params.append(args.status)
        sql += f" LIMIT {int(args.limit)}"
        for row in conn.execute(sql, params).fetchall():
            head = "id" if "id" in cols else cols[0]
            label = next(
                (row[c] for c in ("title", "key", "message",
                                  "date", "content") if c in cols), ""
            )
            print(f"[{row[head]}] {label}")
        return 0


def cmd_install(args) -> int:
    from .install import run_install, run_uninstall
    if args.uninstall:
        return run_uninstall()
    return run_install(update=args.update)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mw")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("set", parents=[common])
    s.add_argument("table")
    s.add_argument("id")
    s.add_argument("field")
    s.add_argument("value")
    s.set_defaults(fn=cmd_set)

    r = sub.add_parser("rm", parents=[common])
    r.add_argument("table")
    r.add_argument("id")
    r.set_defaults(fn=cmd_rm)

    rs = sub.add_parser("resolve", parents=[common])
    rs.add_argument("id")
    rs.set_defaults(fn=cmd_resolve)

    aa = sub.add_parser("add-alert", parents=[common],
                        help="insert one alert row (dedup by type+fingerprint)")
    aa.add_argument("severity", choices=["warn", "critical"])
    aa.add_argument("type")
    aa.add_argument("fingerprint",
                    help="stable dedup key; repeats bump hit_count")
    aa.add_argument("--message", default=None,
                    help="human-readable detail (defaults to fingerprint)")
    aa.add_argument("--source", default=None)
    aa.set_defaults(fn=cmd_add_alert)

    # B1 sessions table — bridge writes via these CLI subprocesses.
    asn = sub.add_parser("add-session", parents=[common],
                         help="upsert (sid, model, channel) in sessions")
    asn.add_argument("--sid", required=True)
    asn.add_argument("--model", default="")
    asn.add_argument("--channel", default="")
    asn.add_argument("--title", default="")
    asn.add_argument("--effort", default="")
    asn.add_argument("--last-active", default="",
                     help="ISO8601 timestamp (default: now). Used by backfill "
                          "to preserve historical jsonl mtimes.")
    asn.set_defaults(fn=cmd_add_session)

    gsm = sub.add_parser("get-session-model", parents=[common],
                         help="print the persisted model for sid (or empty)")
    gsm.add_argument("--sid", required=True)
    gsm.set_defaults(fn=cmd_get_session_model)

    gsc = sub.add_parser("get-session-cwd", parents=[common],
                         help="print the persisted cwd for sid (or empty)")
    gsc.add_argument("--sid", required=True)
    gsc.set_defaults(fn=cmd_get_session_cwd)

    gscr = sub.add_parser("get-session-created", parents=[common],
                          help="Print created_at for sid")
    gscr.add_argument("--sid", required=True)
    gscr.set_defaults(fn=cmd_get_session_created)

    # B6 recent sessions — /resume picker.
    lrs = sub.add_parser("list-recent-sessions", parents=[common],
                         help="print N most-recent sessions, tab-sep")
    lrs.add_argument("--limit", type=int, default=5)
    lrs.add_argument("--channels", default="",
                     help="comma-separated channel allow-list (e.g. wx,tg)")
    lrs.add_argument("--exclude-channels", default="",
                     help="comma-separated channel deny-list (e.g. cli)")
    lrs.add_argument("--require-user-events", action="store_true",
                     help="drop sessions with no real user prompt in events")
    lrs.set_defaults(fn=cmd_list_recent_sessions)

    dn = sub.add_parser("done", parents=[common])
    dn.add_argument("id")
    dn.set_defaults(fn=cmd_done)

    pn = sub.add_parser("pin", parents=[common],
                        help="set pinned=1 on a memes/milestones row")
    pn.add_argument("table")
    pn.add_argument("id")
    pn.set_defaults(fn=cmd_pin)

    upn = sub.add_parser("unpin", parents=[common],
                         help="set pinned=0 on a memes/milestones row")
    upn.add_argument("table")
    upn.add_argument("id")
    upn.set_defaults(fn=cmd_unpin)

    sh = sub.add_parser("show", parents=[common])
    sh.add_argument("table")
    sh.add_argument("id")
    sh.set_defaults(fn=cmd_show)

    ls = sub.add_parser("ls", parents=[common])
    ls.add_argument("table")
    ls.add_argument("--status", default=None)
    ls.add_argument("--limit", type=int, default=50)
    ls.set_defaults(fn=cmd_ls)

    ad = sub.add_parser("add", parents=[common])
    ad.add_argument("table", choices=list(_ADD_TABLES.keys()))
    ad.add_argument("--scope", required=True)
    ad.add_argument("--date", required=True)
    ad.add_argument("--title", required=True)
    ad.add_argument("--description", default=None)
    ad.add_argument("--theme", default=None)
    ad.add_argument("--pinned", action="store_true")
    ad.set_defaults(fn=cmd_add)

    rf = sub.add_parser("refresh", parents=[common])
    rf.add_argument("--all", action="store_true")
    rf.set_defaults(fn=cmd_refresh)

    wt = sub.add_parser("watcher", parents=[common])
    wt.add_argument("action", choices=["start", "stop", "status"])
    wt.set_defaults(fn=cmd_watcher)

    il = sub.add_parser("install-launchd", parents=[common],
                        help="install all 7 marrow launchd agents from deploy/ templates")
    il.set_defaults(fn=cmd_install_launchd)

    ul = sub.add_parser("uninstall-launchd", parents=[common],
                        help="bootout and remove all 7 marrow launchd agents")
    ul.set_defaults(fn=cmd_uninstall_launchd)

    se = sub.add_parser("sessionend", parents=[common])
    se_sub = se.add_subparsers(dest="sessionend_action", required=True)
    se_rerun = se_sub.add_parser("rerun")
    se_rerun.add_argument("sid", help="session id to rerun")
    se_rerun.set_defaults(fn=cmd_sessionend)

    ep = sub.add_parser("export-pit", parents=[common],
                        help="export pit table rows to markdown (run before dropping table)")
    ep.add_argument("--out", default=None,
                    metavar="PATH",
                    help="output path (default: db-pages/projects/pit.md)")
    ep.set_defaults(fn=cmd_export_pit)

    at = sub.add_parser("atlas", parents=[common],
                        help="query atlas rows by path prefix")
    at.add_argument("prefix")
    at.set_defaults(fn=cmd_atlas)

    doc = sub.add_parser("doctor", help="check hook and system health")
    doc.set_defaults(fn=cmd_doctor)

    dr = sub.add_parser("drift", parents=[common],
                        help="drift_sweep: queue or apply file-move ref updates")
    dr_sub = dr.add_subparsers(dest="drift_action", required=True)

    # mw drift scan <old> <new>  — manual one-shot
    dr_scan = dr_sub.add_parser("scan", parents=[common],
                                help="scan refs for an old→new rename")
    dr_scan.add_argument("old", help="old file path")
    dr_scan.add_argument("new_path", help="new file path")
    dr_scan.set_defaults(fn=cmd_drift_scan)

    # mw drift apply <id>
    dr_apply = dr_sub.add_parser("apply", parents=[common],
                                 help="apply a queued drift pending by id")
    dr_apply.add_argument("id")
    dr_apply.set_defaults(fn=cmd_drift_apply)

    # mw drift reject <id>
    dr_reject = dr_sub.add_parser("reject", parents=[common],
                                  help="reject (discard) a queued drift pending")
    dr_reject.add_argument("id")
    dr_reject.set_defaults(fn=cmd_drift_reject)

    al = sub.add_parser("alerts", parents=[common],
                        help="bulk operations on the alerts table")
    al_sub = al.add_subparsers(dest="alerts_action", required=True)
    al_clear = al_sub.add_parser("clear", parents=[common],
                                 help="resolve every unresolved alert")
    al_clear.set_defaults(fn=cmd_alerts_clear)

    ins = sub.add_parser("install", help="Set up marrow globally")
    ins.add_argument("--update", action="store_true",
                     help="Re-sync hooks/commands/plists (skip venv/config)")
    ins.add_argument("--uninstall", action="store_true",
                     help="Remove all global registrations")
    ins.set_defaults(fn=cmd_install)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
