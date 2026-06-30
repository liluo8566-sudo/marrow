"""Daily schedule context injection from Apple Calendar + Reminders via cadence CLI."""
from __future__ import annotations

import glob
import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config
from ._atomic import atomic_write

_CADENCE_DEFAULT = str(Path.home() / "CC-Lab" / "cadence" / ".build" / "debug" / "cadence")
_DAILY_PATH = str(config.DATA_DIR / "daily.md")
_SNAPSHOT_DIR = config.DATA_DIR / "schedule-snapshots"
_TIMEOUT = 5

_REM_GLOB_BASE = str(
    Path.home() / "Library" / "Group Containers"
    / "group.com.apple.reminders" / "Container_v1" / "Stores"
)
_CAL_DB = str(
    Path.home() / "Library" / "Group Containers"
    / "group.com.apple.calendar" / "Calendar.sqlitedb"
)


def _cadence_bin() -> str:
    cfg = config.load()
    return cfg.get("schedule", {}).get("cadence_bin", "") or _CADENCE_DEFAULT


def get_data_mtime() -> float:
    best = 0.0
    for pattern in (
        os.path.join(_REM_GLOB_BASE, "*.sqlite"),
        os.path.join(_REM_GLOB_BASE, "*.sqlite-wal"),
        os.path.join(_REM_GLOB_BASE, "*.sqlite-shm"),
    ):
        for p in glob.glob(pattern):
            try:
                best = max(best, os.path.getmtime(p))
            except OSError:
                pass
    for suffix in ("", "-wal", "-shm"):
        try:
            best = max(best, os.path.getmtime(_CAL_DB + suffix))
        except OSError:
            pass
    return best


def _run_cadence(args: list[str], binary: str) -> str:
    try:
        r = subprocess.run(
            [binary] + args,
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def render_daily(cadence_bin: str | None = None) -> str:
    binary = cadence_bin or _cadence_bin()
    if not os.path.isfile(binary):
        return ""

    tz = config.get_tz()
    now = datetime.now(timezone.utc).astimezone(tz)
    today = now.strftime("%Y-%m-%d")
    day_name = now.strftime("%A")
    time_str = now.strftime("%H:%M")

    cal = _run_cadence(["cal", "read", today, "--human"], binary)
    rem_today = _run_cadence(["rem", "read", "--today", "--human"], binary)
    rem_overdue = _run_cadence(["rem", "read", "--overdue", "--human"], binary)

    if not cal and not rem_today and not rem_overdue:
        return ""

    parts = [f"## Daily Schedule  {today} {day_name} | now {time_str}"]

    if cal:
        parts.append(f"### Calendar\n{cal}")
    if rem_today:
        parts.append(f"### Today's Reminders\n{rem_today}")
    if rem_overdue:
        parts.append(f"### Overdue\n{rem_overdue}")

    out = "\n\n".join(parts)
    if len(out) > 8000:
        out = out[:7900] + "\n... (truncated)"
    return out


def refresh_daily(cadence_bin: str | None = None, daily_path: str | None = None) -> tuple[str, bool]:
    daily_path = daily_path or _DAILY_PATH
    binary = cadence_bin or _cadence_bin()
    content = render_daily(binary)
    if not content:
        if os.path.isfile(daily_path):
            try:
                with open(daily_path, "r", encoding="utf-8") as f:
                    return f.read(), False
            except OSError:
                pass
        return "", False

    old = ""
    if os.path.isfile(daily_path):
        try:
            with open(daily_path, "r", encoding="utf-8") as f:
                old = f.read()
        except OSError:
            pass

    changed = content != old
    if changed:
        atomic_write(daily_path, content)
    return content, changed


def compute_diff(old_content: str, new_content: str) -> str:
    if not old_content or not new_content:
        return ""

    old_lines = set(old_content.splitlines())
    new_lines = set(new_content.splitlines())
    added = new_lines - old_lines
    removed = old_lines - new_lines

    parts: list[str] = []
    for line in sorted(removed):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("--"):
            parts.append(f"-{line[:60]}")
    for line in sorted(added):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("--"):
            parts.append(f"+{line[:60]}")

    if not parts:
        return ""
    out = " | ".join(parts[:8])
    if len(parts) > 8:
        out += f" | +{len(parts) - 8} more"
    return out[:500]


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _snapshot_dir() -> Path:
    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return _SNAPSHOT_DIR


def _mtime_path(session_id: str) -> Path:
    return _snapshot_dir() / f"{session_id}.mtime"


def _hash_path(session_id: str) -> Path:
    return _snapshot_dir() / f"{session_id}.hash"


def check_and_inject(
    session_id: str,
    cadence_bin: str | None = None,
    daily_path: str | None = None,
) -> str | None:
    daily_path = daily_path or _DAILY_PATH
    binary = cadence_bin or _cadence_bin()

    data_mt = get_data_mtime()
    mt_file = _mtime_path(session_id)
    hash_file = _hash_path(session_id)

    last_mt = 0.0
    try:
        if mt_file.exists():
            last_mt = float(mt_file.read_text().strip())
    except (ValueError, OSError):
        pass

    if last_mt and abs(data_mt - last_mt) < 0.01:
        return None

    old_content = ""
    if os.path.isfile(daily_path):
        try:
            with open(daily_path, "r", encoding="utf-8") as f:
                old_content = f.read()
        except OSError:
            pass

    content, _ = refresh_daily(binary, daily_path)
    if not content:
        return None

    try:
        mt_file.write_text(str(data_mt))
    except OSError:
        pass

    content_hash = _hash(content)

    old_hash = ""
    try:
        if hash_file.exists():
            old_hash = hash_file.read_text().strip()
    except OSError:
        pass

    try:
        hash_file.write_text(content_hash)
    except OSError:
        pass

    if not old_hash:
        return content

    if content_hash == old_hash:
        return None

    diff = compute_diff(old_content, content)
    if diff:
        return f"Schedule update: {diff}\n\n{content}"
    return content
