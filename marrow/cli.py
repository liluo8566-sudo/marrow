"""`mw` — deterministic point-edit/remove of one record by id. No LLM.

Thin shell over storage.py. CLI is the public surface; main(argv) returns
an exit code. Hooks/daemon never call here — they use repo.py.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from contextlib import contextmanager

from . import config, storage


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


def cmd_done(args) -> int:
    return _shortcut(
        args, "threads",
        "UPDATE threads SET status='done' WHERE id=?", "status=done",
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

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
