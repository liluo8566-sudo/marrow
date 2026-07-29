"""Git auto-commit housekeep block + ~/.claude.json snapshot."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from .. import config, repo

# ── git housekeep ────────────────────────────────────────────────────────────

# Files whose deletion must never be auto-committed away silently (Part A only).
_HOUSEKEEP_PROTECTED_DEFAULT = [
    "CLAUDE.md", "settings.json", "keybindings.json", "statusline.py",
    "output-styles/ny.md",
]


def _housekeep_protected_files() -> list[str]:
    try:
        return config.load().get("hooks", {}).get(
            "housekeep_protected_files", _HOUSEKEEP_PROTECTED_DEFAULT
        )
    except Exception:
        return _HOUSEKEEP_PROTECTED_DEFAULT


# Docs/config-shaped files: committed unconditionally. Everything else waits
# until it has gone quiet for `housekeep_stale_hours` (likely another live
# session's WIP otherwise).
_HOUSEKEEP_DOCS_EXTS_DEFAULT = [".md", ".toml", ".json", ".txt"]
_HOUSEKEEP_STALE_HOURS_DEFAULT = 2.0


def _housekeep_docs_exts() -> set[str]:
    try:
        exts = config.load().get("hooks", {}).get(
            "housekeep_docs_extensions", _HOUSEKEEP_DOCS_EXTS_DEFAULT
        )
    except Exception:
        exts = _HOUSEKEEP_DOCS_EXTS_DEFAULT
    return {str(e).lower() for e in exts}


def _housekeep_stale_hours() -> float:
    try:
        return float(config.load().get("hooks", {}).get(
            "housekeep_stale_hours", _HOUSEKEEP_STALE_HOURS_DEFAULT
        ))
    except Exception:
        return _HOUSEKEEP_STALE_HOURS_DEFAULT


def _unquote_porcelain(path: str) -> str:
    """Undo git's C-style quoting (`"caf\\303\\251.md"` → `café.md`)."""
    p = path.strip()
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        try:
            return (p[1:-1].encode("ascii").decode("unicode_escape")
                    .encode("latin-1").decode("utf-8"))
        except Exception:  # noqa: BLE001
            return p[1:-1]
    return p


def _porcelain_paths(line: str) -> list[str]:
    """Pathspec(s) for one porcelain line — renames yield old + new."""
    raw = line[3:].strip()
    if " -> " in raw:
        old, new = raw.split(" -> ", 1)
        return [_unquote_porcelain(old), _unquote_porcelain(new)]
    return [_unquote_porcelain(raw)]


def _newest_mtime(target: Path) -> float:
    """Freshness stamp for a porcelain target. Git reports an untracked
    directory as one `?? dir/` line, and the directory's own mtime can be far
    older than a file just written inside it — so a directory is judged by the
    newest mtime anywhere under it."""
    newest = target.stat().st_mtime
    if not target.is_dir():
        return newest
    for p in target.rglob("*"):
        try:
            if p.is_file():
                newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


def _split_housekeep_dirty(
    repo: str, dirty: list[str], now: float | None = None
) -> tuple[list[str], list[str], list[str]]:
    """(docs, stale, fresh) porcelain lines.

    docs = extension in `housekeep_docs_extensions` → always committable.
    stale = other files whose mtime is older than `housekeep_stale_hours`,
    plus anything with no mtime (deleted/renamed away).
    fresh = the rest — left uncommitted, likely another live session's WIP.
    """
    exts = _housekeep_docs_exts()
    cutoff = _housekeep_stale_hours() * 3600
    now = time.time() if now is None else now
    docs: list[str] = []
    stale: list[str] = []
    fresh: list[str] = []
    for line in dirty:
        if not line.strip():
            continue
        target = _porcelain_paths(line)[-1]
        if Path(target).suffix.lower() in exts:
            docs.append(line)
            continue
        try:
            mtime = _newest_mtime(Path(repo) / target)
        except Exception:  # noqa: BLE001 — deleted/renamed/unreadable
            stale.append(line)
            continue
        (stale if now - mtime >= cutoff else fresh).append(line)
    return docs, stale, fresh


def _commit_housekeep_groups(
    repo: str, dirty: list[str], tag: str | None, label: str
) -> list[str]:
    """Commit the docs group and the stale group separately; report lines."""
    docs, stale, fresh = _split_housekeep_dirty(repo, dirty)
    out: list[str] = []
    for group, subject, suffix in (
        (docs, "docs housekeep", "docs"),
        (stale, "stale leftovers", "stale"),
    ):
        if not group:
            continue
        cats = _categorize_porcelain(group)
        paths: list[str] = []
        for line in group:
            paths.extend(_porcelain_paths(line))
        subprocess.run(
            ["git", "-C", repo, "add", "-A", "--"] + paths,
            capture_output=True, text=True, timeout=5, check=False,
        )
        cr = subprocess.run(
            ["git", "-C", repo, "commit",
             "-m", _build_housekeep_commit_msg(cats, len(group), tag, subject),
             "--"] + paths,
            capture_output=True, text=True, timeout=10, check=False,
        )
        if cr.returncode != 0:
            out.append(f"{label} {suffix}: ⚠️ commit failed ({len(group)} files)")
            continue
        out.append(_build_housekeep_report_line(f"{label} {suffix}", cats, len(group)))
    if fresh:
        out.append(
            f"{label}: skipped {len(fresh)} fresh file(s) "
            f"(<{_housekeep_stale_hours():g}h — possibly a live session)"
        )
    return out


def _categorize_porcelain(lines: list[str]) -> dict[str, list[str]]:
    """Bucket `git status --porcelain` lines into deleted/renamed/added/modified.

    XY status codes: 'R'/'C' = rename/copy (own bucket, path keeps the
    'old -> new' arrow); 'D' = deleted; '?' or 'A' = added; everything else
    (M, T, U, ...) = modified.
    """
    cats: dict[str, list[str]] = {
        "deleted": [], "renamed": [], "added": [], "modified": [],
    }
    for line in lines:
        if not line.strip():
            continue
        xy, path = line[:2], line[3:].strip()
        if "R" in xy or "C" in xy:
            cats["renamed"].append(path)
        elif "D" in xy:
            cats["deleted"].append(path)
        elif "?" in xy or "A" in xy:
            cats["added"].append(path)
        else:
            cats["modified"].append(path)
    return cats


def _session_tag(sid: str | None, conn: sqlite3.Connection) -> str | None:
    """`<channel>·<sid[:4]>` for the sessions row, or None when sid/channel
    is unavailable — callers then emit their subject unchanged."""
    if not sid:
        return None
    try:
        row = conn.execute(
            "SELECT channel FROM sessions WHERE sid = ?", (sid,)
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    channel = (row[0] if row is not None else None) or ""  # Row or tuple
    if not str(channel).strip():
        return None
    return f"{channel}·{sid[:4]}"


def _build_housekeep_commit_msg(cats: dict[str, list[str]], total: int,
                                tag: str | None = None,
                                kind: str = "session-start housekeep") -> str:
    """Subject `auto: <kind> (N files)[ [tag]]` — the tag stamps which
    session's window auto-committed the work. Body lists files by category,
    deleted first and never truncated. Body caps at ~2000 chars overall
    (added/modified may truncate, deleted never does).
    """
    subject = f"auto: {kind} ({total} files)"
    if tag:
        subject += f" [{tag}]"
    body_lines: list[str] = []
    running = 0
    if cats["deleted"]:
        line = "deleted: " + ", ".join(cats["deleted"])
        body_lines.append(line)
        running += len(line)
    for key in ("renamed", "added", "modified"):
        items = cats[key]
        if not items:
            continue
        line = f"{key}: " + ", ".join(items)
        if running + len(line) > 2000:
            remaining = 2000 - running
            if remaining <= len(key) + 2:
                continue
            line = line[:remaining - 1] + "…"
        body_lines.append(line)
        running += len(line)
    if not body_lines:
        return subject
    return subject + "\n\n" + "\n".join(body_lines)


def _build_housekeep_report_line(label: str, cats: dict[str, list[str]], total: int) -> str:
    line = f"{label}: committed {total} files"
    deleted = cats["deleted"]
    if not deleted:
        return line
    joined = ", ".join(deleted)
    if len(joined) > 120:
        joined = joined[:117] + "…"
        return f"{line} ⚠️ {len(deleted)} deleted: {joined}"
    return f"{line} ⚠️ deleted: {joined}"


def _git_housekeep_block(
    cwd: str | None, current_sid: str | None, conn: sqlite3.Connection
) -> str | None:
    """Auto-commit leftover diffs from prior sessions at session start.

    Three parts joined with ' · '. Returns None if nothing to report.
    Entire function is fail-soft — never blocks session_start.
    """
    try:
        lines: list[str] = []
        tag = _session_tag(current_sid, conn)

        # Part A: ~/.claude auto-commit
        try:
            claude_dir = Path("~/.claude").expanduser()
            if (claude_dir / ".git").is_dir():
                r = subprocess.run(
                    ["git", "-C", str(claude_dir), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                dirty = [l for l in r.stdout.splitlines() if l.strip()]
                if dirty:
                    cats = _categorize_porcelain(dirty)
                    protected = _housekeep_protected_files()
                    blocked = [p for p in cats["deleted"] if p in protected]
                    if blocked:
                        repo.add_alert(
                            "warn", "git_housekeep_protected_delete",
                            f"claude:{','.join(sorted(blocked))}",
                            source="hooks.py",
                            message=(
                                "~/.claude session-start housekeep would delete "
                                f"protected file(s): {', '.join(blocked)} — "
                                "commit skipped, working tree left dirty"
                            ),
                            db=config.db_path(),
                        )
                        lines.append(
                            f"~/.claude: ⚠️ SKIPPED — protected file(s) "
                            f"deleted: {', '.join(blocked)} (resolve manually)"
                        )
                    else:
                        lines.extend(_commit_housekeep_groups(
                            str(claude_dir), dirty, tag, "~/.claude"))
        except Exception:
            pass

        # Part B: project cwd — commit submodules first, then top-level
        try:
            if cwd and Path(cwd).is_dir():
                # B1: recurse into nested git repos and commit dirty ones
                cwd_p = Path(cwd)
                nested = [d for d in cwd_p.iterdir()
                          if d.is_dir() and (d / ".git").exists()]
                for sm_abs_p in nested:
                    sm_path = sm_abs_p.name
                    sm_abs = str(sm_abs_p)
                    sr = subprocess.run(
                        ["git", "-C", sm_abs, "status", "--porcelain"],
                        capture_output=True, text=True, timeout=5, check=False,
                    )
                    sm_dirty = [l for l in sr.stdout.splitlines() if l.strip()]
                    if sm_dirty:
                        lines.extend(_commit_housekeep_groups(
                            sm_abs, sm_dirty, tag, sm_path))

                # B2: top-level commit (picks up updated submodule pointers + own files)
                r = subprocess.run(
                    ["git", "-C", cwd, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                dirty = [l for l in r.stdout.splitlines() if l.strip()]
                if dirty:
                    lines.extend(_commit_housekeep_groups(cwd, dirty, tag, "cwd"))
        except Exception:
            pass

        # Part C: stale worktree detection + cleanup
        try:
            if cwd and Path(cwd).is_dir():
                r = subprocess.run(
                    ["git", "-C", cwd, "worktree", "list", "--porcelain"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                wt_paths = [
                    l.split(" ", 1)[1].strip()
                    for l in r.stdout.splitlines()
                    if l.startswith("worktree ")
                ]
                secondary = wt_paths[1:]
                if secondary:
                    now = time.time()
                    stale, fresh = [], []
                    for p in secondary:
                        pp = Path(p)
                        if not pp.is_dir():
                            continue
                        age_h = (now - pp.stat().st_mtime) / 3600
                        name = pp.name
                        if age_h >= 24:
                            has_changes = bool(subprocess.run(
                                ["git", "-C", p, "status", "--porcelain"],
                                capture_output=True, text=True, timeout=5, check=False,
                            ).stdout.strip())
                            if has_changes:
                                stale.append(f"{name} ({age_h:.0f}h, has uncommitted changes)")
                            else:
                                branch = subprocess.run(
                                    ["git", "-C", p, "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True, timeout=5, check=False,
                                ).stdout.strip()
                                subprocess.run(
                                    ["git", "-C", cwd, "worktree", "remove", p],
                                    capture_output=True, text=True, timeout=10, check=False,
                                )
                                if branch and branch != "HEAD":
                                    subprocess.run(
                                        ["git", "-C", cwd, "branch", "-d", branch],
                                        capture_output=True, text=True, timeout=5, check=False,
                                    )
                                stale.append(f"{name} ({age_h:.0f}h, clean — removed)")
                        else:
                            fresh.append(name)
                    parts = []
                    if stale:
                        parts.append("stale wt: " + "; ".join(stale))
                    if fresh:
                        parts.append(f"{len(fresh)} active wt: " + ", ".join(fresh))
                    if parts:
                        lines.append(" · ".join(parts))
        except Exception:
            pass

        return " · ".join(lines) if lines else None
    except Exception:
        return None


# ── ~/.claude.json mcpServers snapshot ───────────────────────────────────────

_CLAUDE_JSON_SNAPSHOT_KEEP_DEFAULT = 10


def _claude_json_snapshot_keep() -> int:
    try:
        return int(config.load().get("hooks", {}).get(
            "claude_json_snapshot_keep", _CLAUDE_JSON_SNAPSHOT_KEEP_DEFAULT
        ))
    except Exception:
        return _CLAUDE_JSON_SNAPSHOT_KEEP_DEFAULT


def _claude_json_snapshot_block() -> str | None:
    """Fail-soft rolling backup of ~/.claude.json's mcpServers block. Never
    blocks session_start; on parse failure raises an alert instead of
    snapshotting the corrupt file.
    """
    try:
        src = Path.home() / ".claude.json"
        if not src.exists():
            return None

        try:
            raw = src.read_text()
        except Exception:
            return None

        try:
            data = json.loads(raw)
        except Exception:
            repo.add_alert(
                "warn", "claude_json_corrupt", "claude_json_corrupt",
                source="hooks.py",
                message="~/.claude.json failed to parse as JSON — snapshot skipped",
                db=config.db_path(),
            )
            return "claude.json: ⚠️ corrupt JSON, snapshot skipped"

        mcp_hash = hashlib.sha256(
            json.dumps(data.get("mcpServers", {}), sort_keys=True).encode()
        ).hexdigest()

        snap_dir = Path(config.DATA_DIR) / "backup" / "claude-json"
        snap_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(snap_dir.glob("claude-json-*.json"))

        if existing:
            newest = existing[-1]
            try:
                newest_data = json.loads(newest.read_text())
                newest_hash = hashlib.sha256(
                    json.dumps(newest_data.get("mcpServers", {}), sort_keys=True).encode()
                ).hexdigest()
            except Exception:
                newest_hash = None
            if newest_hash == mcp_hash:
                return None

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = snap_dir / f"claude-json-{stamp}.json"
        shutil.copy2(src, dest)

        keep = _claude_json_snapshot_keep()
        all_snaps = sorted(snap_dir.glob("claude-json-*.json"))
        if keep > 0:
            for stale in all_snaps[:-keep]:
                try:
                    stale.unlink()
                except Exception:
                    pass

        return "claude.json: snapshot saved (mcpServers changed)"
    except Exception:
        return None
