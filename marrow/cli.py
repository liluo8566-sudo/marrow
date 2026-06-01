"""`mw` — deterministic point-edit/remove of one record by id. No LLM.

Thin shell over storage.py. CLI is the public surface; main(argv) returns
an exit code. Hooks/daemon never call here — they use repo.py.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

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
    return _shortcut(
        args, "alerts",
        "UPDATE alerts SET resolved=1, "
        "resolved_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
        "resolved via mw",
    )


def cmd_add_alert(args) -> int:
    sev = args.severity
    if sev not in {"warn", "critical"}:
        return _fail(f"severity must be warn|critical (got {sev})")
    msg = (args.message or "").strip()
    if not msg:
        return _fail("message required")
    aid = repo.add_alert(sev, args.type, msg, args.source, db=args.db)
    print(f"alert #{aid} [{sev}/{args.type}] {msg[:80]}")
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

_MEL_TZ = ZoneInfo("Australia/Melbourne")


def cmd_goose_bites(args) -> int:
    from .goose_bites import select_quote_for_date
    if args.date:
        date = args.date
    else:
        today = datetime.datetime.now(_MEL_TZ).date()
        date = (today - datetime.timedelta(days=1)).isoformat()
    with _conn(args.db) as conn:
        quote = select_quote_for_date(conn, date)
    if quote:
        print(f"selected: {quote}")
    else:
        print(f"no quote for {date}")
    return 0


def cmd_handover(args) -> int:
    """Manually fire sessionend_async for a sid — re-renders handover.md.
    Fire-and-forget popen; logs land in DATA_DIR/logs/sessionend_async_<sid>.log."""
    if not args.sid:
        return _fail("mw handover requires --sid <session_id>")
    from .popen_detach import popen_detach
    log = config.DATA_DIR / "logs" / f"sessionend_async_{args.sid}.log"
    popen_detach(
        [sys.executable, "-m", "marrow.sessionend_async", "--sid", args.sid],
        log_path=log,
    )
    print(f"handover async fired for sid={args.sid} (log: {log})")
    return 0


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
        [sys.executable, "-m", "marrow.sessionend_async", "--sid", target_sid],
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

    Always: dashboard.md + handover.md. With include_subpages: db-pages.
    """
    from .md_index import MdIndex
    idx = MdIndex(conn)
    file_roots = [config.dashboard_path(),
                  str(config.DATA_DIR / "handover.md")]
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
        print(msg)
        return 0
    finally:
        conn.close()


_WATCHER_LABEL = "com.marrow.watcher"
_WATCHER_PLIST_SRC = Path(__file__).resolve().parents[1] / "deploy" / "mw-watcher.plist"
_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"


def _watcher_plist_target() -> Path:
    return _LAUNCH_AGENTS / "com.marrow.watcher.plist"


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
                        help="insert one alert row (idempotent on dup)")
    aa.add_argument("severity", choices=["warn", "critical"])
    aa.add_argument("type")
    aa.add_argument("message")
    aa.add_argument("--source", default=None)
    aa.set_defaults(fn=cmd_add_alert)

    dn = sub.add_parser("done", parents=[common])
    dn.add_argument("id")
    dn.set_defaults(fn=cmd_done)

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

    gs = sub.add_parser("goose-bites", parents=[common])
    gs.add_argument("--date", default=None, metavar="YYYY-MM-DD")
    gs.set_defaults(fn=cmd_goose_bites)

    rf = sub.add_parser("refresh", parents=[common])
    rf.add_argument("--all", action="store_true")
    rf.set_defaults(fn=cmd_refresh)

    wt = sub.add_parser("watcher", parents=[common])
    wt.add_argument("action", choices=["start", "stop", "status"])
    wt.set_defaults(fn=cmd_watcher)

    ho = sub.add_parser("handover", parents=[common])
    ho.add_argument("--sid", required=True,
                    help="session id to re-extract")
    ho.set_defaults(fn=cmd_handover)

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

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
