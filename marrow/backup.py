"""DESIGN net "DB never lost": daily atomic DB snapshot + iCloud offsite.

Snapshot uses sqlite `VACUUM INTO` (consistent read of the live WAL DB,
no destructive lock) to a temp path, then atomic os.replace into the
local backup dir as marrow-YYYY-MM-DD.db. The same snapshot is atomically
placed offsite (iCloud). Offsite unreachable never fails the local leg:
the local dump still succeeds and a failure alert is raised. Conservative
retention: keep the newest N daily dumps each side, prune the rest.
Retention policy beyond a flat count is DESIGN-Pending — kept minimal.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

from marrow import config, repo

DEFAULT_KEEP = 14
_NAME_RE = re.compile(r"^marrow-\d{4}-\d{2}-\d{2}\.db$")


def _snap_name(today: str) -> str:
    return f"marrow-{today}.db"


def _existing(d: Path) -> list[Path]:
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if _NAME_RE.match(p.name))


def plan(*, local_dir: str, keep: int, today: str,
         offsite_dir: str | None = None) -> dict:
    """Pure: what a run would write/prune. No filesystem mutation."""
    name = _snap_name(today)

    def _prune_for(d: Path) -> list[Path]:
        names = {p.name for p in _existing(d)}
        names.add(name)
        keep_set = sorted(names, reverse=True)[:keep]
        return [d / n for n in sorted(names) if n not in keep_set]

    out = {
        "name": name,
        "would_write": str(Path(local_dir) / name),
        "prune": _prune_for(Path(local_dir)),
    }
    if offsite_dir is not None:
        out["offsite_would_write"] = str(Path(offsite_dir) / name)
        out["offsite_prune"] = _prune_for(Path(offsite_dir))
    return out


def _snapshot(db: str, dest: Path) -> None:
    """Consistent snapshot of a live (WAL) DB, then atomic replace."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mw-bk-", suffix=".db",
                               dir=str(dest.parent))
    os.close(fd)
    os.unlink(tmp)
    src = sqlite3.connect(db, timeout=30.0)
    try:
        src.execute("VACUUM INTO ?", (tmp,))
    finally:
        src.close()
    os.replace(tmp, dest)


def _place(src_file: Path, dest: Path) -> None:
    """Atomic copy of an already-consistent snapshot to dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mw-bk-", suffix=".db",
                               dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as out, open(src_file, "rb") as inp:
            while chunk := inp.read(1 << 20):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _prune(targets: list[Path]) -> None:
    for p in targets:
        try:
            p.unlink()
        except OSError:
            pass


def run(*, apply: bool = False, db: str | None = None,
        local_dir: str | None = None, offsite_dir: str | None = None,
        keep: int = DEFAULT_KEEP, today: str | None = None,
        alert_db: str | None = None) -> dict:
    cfg = config.load()
    db = db or cfg["paths"]["db"]
    local_dir = local_dir or cfg["paths"]["backup_dir"]
    offsite_dir = offsite_dir or cfg["paths"]["offsite_backup_dir"]
    keep = keep or int(cfg.get("backup", {}).get("keep", DEFAULT_KEEP))
    today = today or date.today().isoformat()
    p = plan(local_dir=local_dir, keep=keep, today=today,
             offsite_dir=offsite_dir)
    rep = {
        "applied": apply,
        "would_write": p["would_write"],
        "local_ok": False,
        "offsite_ok": False,
        "pruned": 0,
    }
    if not apply:
        return rep

    local_file = Path(local_dir) / p["name"]
    try:
        _snapshot(db, local_file)
        rep["local_ok"] = True
    except Exception as e:
        repo.add_alert(
            "critical", "backup",
            f"local DB snapshot failed: {e}. Recover: re-run "
            f"`python -m marrow.backup --apply`; check disk/space at "
            f"{local_dir}.",
            source="backup.py", db=alert_db,
        )
        return rep

    try:
        _place(local_file, Path(offsite_dir) / p["name"])
        rep["offsite_ok"] = True
    except Exception as e:
        repo.add_alert(
            "warn", "backup",
            f"offsite backup failed: {e}. Local dump OK at {local_file}. "
            f"Recover: ensure iCloud path reachable, then re-run "
            f"`python -m marrow.backup --apply`.",
            source="backup.py", db=alert_db,
        )

    _prune(p["prune"])
    rep["pruned"] += len(p["prune"])
    if rep["offsite_ok"]:
        _prune(p["offsite_prune"])
        rep["pruned"] += len(p["offsite_prune"])
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="marrow.backup",
        description="Daily atomic DB snapshot + iCloud offsite (DB never lost)",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true",
                   help="execute (default: dry-run plan)")
    g.add_argument("--dry-run", action="store_true",
                   help="print plan only (default)")
    ap.add_argument("--keep", type=int, default=0,
                    help=f"daily dumps to retain each side "
                         f"(default {DEFAULT_KEEP})")
    a = ap.parse_args(argv)
    rep = run(apply=a.apply, keep=a.keep or 0)
    if not a.apply:
        print(f"[backup] would write {rep['would_write']} "
              f"(dry-run; --apply to execute)")
    else:
        loc = "ok" if rep["local_ok"] else "FAILED"
        off = "ok" if rep["offsite_ok"] else "FAILED"
        print(f"[backup] local={loc} offsite={off} "
              f"pruned={rep['pruned']} -> {rep['would_write']}")
    return 0 if (not a.apply or rep["local_ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
