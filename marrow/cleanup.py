"""Standalone sdk-cli jsonl disk reaper.

Spawned `claude -p` runs (prompt-lint haiku, diary digest) each drop a
.jsonl into ~/.claude/projects/, cluttering the CC project list. Their
event data is already firewalled (transcript.is_headless -> clean()=[]);
this is disk/UX hygiene only. Decoupled from the diary routine on purpose
(one job failing must never starve the other).

Delete a .jsonl iff transcript.is_headless()==True AND it is older than
grace_days (headless processes die in seconds; the age guard only protects
a jsonl still being written). Interactive (cli) and legacy (no entrypoint)
sessions are always kept. Idempotent: a second run deletes nothing.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from marrow import transcript

PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_GRACE_DAYS = 1
DAY = 86400.0


def scan(projects_dir, grace_days, now):
    to_delete: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for f in sorted(Path(projects_dir).rglob("*.jsonl")):
        if not transcript.is_headless(str(f)):
            skipped.append((f, "kept"))
            continue
        if now - f.stat().st_mtime < grace_days * DAY:
            skipped.append((f, "too young"))
            continue
        to_delete.append(f)
    return to_delete, skipped


def run(apply=False, projects_dir=PROJECTS_DIR, grace_days=DEFAULT_GRACE_DAYS,
        now=None):
    now = time.time() if now is None else now
    to_delete, skipped = scan(projects_dir, grace_days, now)
    freed = 0
    deleted = 0
    for f in to_delete:
        try:
            freed += f.stat().st_size
        except OSError:
            pass
        if apply:
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
    return {
        "applied": apply,
        "would_delete": len(to_delete),
        "deleted": deleted,
        "kept": len(skipped),
        "freed_bytes": freed,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="marrow.cleanup",
                                 description="Reap spawned sdk-cli .jsonl")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default: dry-run)")
    ap.add_argument("--grace-days", type=int, default=DEFAULT_GRACE_DAYS,
                    help=f"keep files newer than N days (default {DEFAULT_GRACE_DAYS})")
    a = ap.parse_args(argv)
    rep = run(apply=a.apply, grace_days=a.grace_days)
    verb = "deleted" if a.apply else "would delete"
    mb = rep["freed_bytes"] / 1e6
    n = rep["deleted"] if a.apply else rep["would_delete"]
    print(f"[cleanup] {verb} {n} sdk-cli jsonl, kept {rep['kept']}, "
          f"{mb:.1f} MB" + ("" if a.apply else " (dry-run; --apply to delete)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
