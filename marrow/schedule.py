"""Daily schedule context injection from Apple Calendar + Reminders via cadence CLI."""
from __future__ import annotations

import glob
import json as _json
import os
import re as _re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config
from ._atomic import atomic_write

_CADENCE_DEFAULT = str(Path.home() / "CC-Lab" / "cadence" / ".build" / "debug" / "cadence")
_DAILY_PATH = str(config.DATA_DIR / "daily.md")
_SNAPSHOT_DIR = config.DATA_DIR / "schedule-snapshots"
_TIMEOUT = 5
_MAX_CHARS = 8000

_DEFAULT_FLAG_NOTE = "Only follow up overdue flagged 🚩 tasks — no push on others."

_REM_GLOB_BASE = str(
    Path.home() / "Library" / "Group Containers"
    / "group.com.apple.reminders" / "Container_v1" / "Stores"
)
_CAL_DB = str(
    Path.home() / "Library" / "Group Containers"
    / "group.com.apple.calendar" / "Calendar.sqlitedb"
)

_PRIORITY_LABELS = {1: "⚡"}


def _schedule_cfg() -> dict:
    return config.load().get("schedule", {}) or {}


def is_enabled() -> bool:
    val = _schedule_cfg().get("enabled", True)
    return bool(val)


def _cadence_bin() -> str:
    return _schedule_cfg().get("cadence_bin", "") or _CADENCE_DEFAULT


def _flag_note() -> str:
    note = _schedule_cfg().get("flag_note", "")
    return note if note else _DEFAULT_FLAG_NOTE


def _cal_exclude() -> set[str]:
    val = _schedule_cfg().get("cal_exclude", [])
    if not isinstance(val, list):
        return set()
    return {str(v) for v in val}


def _cal_keep_re():
    pattern = _schedule_cfg().get("cal_keep", "")
    if not pattern:
        return None
    try:
        return _re.compile(pattern)
    except _re.error:
        return None


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


def _clean_list(name: str) -> str:
    """Strip trailing emoji/whitespace from a list or calendar name."""
    return name.strip()


def _hm(iso: str) -> str:
    """Extract HH:MM from an ISO datetime string, or '' on failure."""
    if not iso or len(iso) < 16:
        return ""
    return iso[11:16]


def _glyphs(prio: int, flagged: bool) -> str:
    g = _PRIORITY_LABELS.get(prio, "")
    if flagged:
        g += "🚩"
    return g


def _rem_line(r: dict, today_str: str) -> tuple[str, str, str]:
    """Return (bucket, sort_key, line).

    bucket: overdue | timed | untimed | done. sort_key orders within a bucket.
    Line ends with the reminder id in brackets, after any glyphs, e.g.
    '- [List] HH:MM title 🚩 [151]'.
    """
    lst = _clean_list(r.get("list", "Inbox"))
    title = r.get("title", "")
    prio = r.get("priority", 0)
    flagged = bool(r.get("flagged", False))
    glyph = _glyphs(prio, flagged)
    suffix = f" {glyph}" if glyph else ""
    rid = r.get("id")
    id_tag = f" [{rid}]" if rid is not None else ""

    if r.get("completed"):
        comp = r.get("completion_date", "")
        hm = _hm(comp)
        tag = f" [Done {hm}]" if hm else " [Done]"
        line = f"- [{lst}] {title}{tag}{suffix}{id_tag}"
        return "done", comp, line

    due = r.get("due_date", "")
    due_day = due[:10] if due else ""
    hm = _hm(due)
    timed = due and hm and hm != "00:00"

    if due_day and due_day < today_str:
        line = f"- [{lst}] {title} [Overdue]{suffix}{id_tag}"
        return "overdue", due, line
    if timed:
        line = f"- [{lst}] {hm} {title}{suffix}{id_tag}"
        return "timed", hm, line
    line = f"- [{lst}] {title}{suffix}{id_tag}"
    return "untimed", title, line


def _render_reminders(rem_json: str, done_json: str, today_str: str) -> list[str]:
    overdue: list[tuple[str, str]] = []
    timed: list[tuple[str, str]] = []
    untimed: list[tuple[str, str]] = []
    done: list[tuple[str, str]] = []

    try:
        items = _json.loads(rem_json) if rem_json else []
    except (ValueError, TypeError):
        items = []
    for r in items:
        if r.get("completed"):
            continue
        due = r.get("due_date", "")
        due_day = due[:10] if due else ""
        # only dated reminders: overdue (past) + today; skip future + no-due
        if not due_day or due_day > today_str:
            continue
        bucket, key, line = _rem_line(r, today_str)
        if bucket == "overdue":
            overdue.append((key, line))
        elif bucket == "timed":
            timed.append((key, line))
        else:
            untimed.append((key, line))

    try:
        done_items = _json.loads(done_json) if done_json else []
    except (ValueError, TypeError):
        done_items = []
    for r in done_items:
        comp = r.get("completion_date", "")
        if not comp or comp[:10] != today_str:
            continue
        _b, key, line = _rem_line(r, today_str)
        done.append((key, line))

    lines: list[str] = []
    for key, line in sorted(overdue, key=lambda x: x[0]):
        lines.append(line)
    for key, line in sorted(timed, key=lambda x: x[0]):
        lines.append(line)
    for key, line in sorted(untimed, key=lambda x: x[0]):
        lines.append(line)
    for key, line in sorted(done, key=lambda x: x[0]):
        lines.append(line)
    return lines


def _render_calendar(cal_json: str, today_str: str) -> list[str]:
    try:
        events = _json.loads(cal_json) if cal_json else []
    except (ValueError, TypeError):
        events = []
    exclude = _cal_exclude()
    keep_re = _cal_keep_re()
    rows: list[tuple[str, str]] = []
    for e in events:
        cal = _clean_list(e.get("calendar", ""))
        title = e.get("title", "")
        if cal in exclude:
            if not (keep_re and keep_re.search(title)):
                continue
        if e.get("all_day"):
            # skip all-day Scheduled Reminders duplicates; keep other all-day
            if cal == "Scheduled Reminders":
                continue
            rows.append(("", f"- [{cal}] {title} [all-day]"))
            continue
        start = e.get("start", "")
        end = e.get("end", "")
        if start[:10] != today_str:
            continue
        s_hm = _hm(start)
        e_hm = _hm(end)
        rows.append((s_hm, f"- [{cal}] {s_hm}-{e_hm} {title}"))
    return [line for _k, line in sorted(rows, key=lambda x: x[0])]


def render_daily(cadence_bin: str | None = None) -> str:
    binary = cadence_bin or _cadence_bin()
    if not os.path.isfile(binary):
        return ""

    tz = config.get_tz()
    now = datetime.now(timezone.utc).astimezone(tz)
    today = now.strftime("%Y-%m-%d")
    day_name = now.strftime("%A")
    time_str = now.strftime("%H:%M")

    cal_json = _run_cadence(["cal", "read", today, "--json"], binary)
    rem_json = _run_cadence(["rem", "read", "--all"], binary)
    done_json = _run_cadence(["rem", "read", "--done"], binary)

    rem_lines = _render_reminders(rem_json, done_json, today)
    cal_lines = _render_calendar(cal_json, today)

    if not rem_lines and not cal_lines:
        return ""

    parts = [
        f"## Daily Schedule  {today} {day_name} | now {time_str}",
        _flag_note(),
    ]
    body: list[str] = []
    body.extend(rem_lines)
    body.append("---")
    body.extend(cal_lines)
    parts.append("\n".join(body))

    out = "\n".join(parts)
    if len(out) > _MAX_CHARS:
        out = out[: _MAX_CHARS - 100] + "\n... (truncated)"
        try:
            from . import repo
            repo.add_alert("info", "schedule", "render_truncated",
                           message=f"render_daily exceeded {_MAX_CHARS} chars",
                           source="schedule.py", db=config.db_path())
        except Exception:
            pass
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


# --- structured diff -------------------------------------------------------

def _split_sections(content: str) -> tuple[list[str], list[str]]:
    """Return (rem_lines, cal_lines) from rendered daily content."""
    lines = content.splitlines()
    rem: list[str] = []
    cal: list[str] = []
    seen_sep = False
    for ln in lines:
        if ln.startswith("## ") or ln == _flag_note() or not ln.strip():
            continue
        if ln.strip() == "---":
            seen_sep = True
            continue
        if not ln.startswith("- "):
            continue
        (cal if seen_sep else rem).append(ln)
    return rem, cal


_TRAILING_ID_RE = _re.compile(r"\s*\[(\d+)\]\s*$")


def _rem_identity(line: str) -> str:
    """Stable identity of a rem line.

    Prefers the trailing reminder id `[id]`; rows without one (shouldn't
    happen for real cadence data, kept for robustness) fall back to
    list + title, ignoring time/tag/status decorations.
    """
    m = _TRAILING_ID_RE.search(line)
    if m:
        return f"id:{m.group(1)}"

    s = line
    # strip leading '- '
    s = s[2:] if s.startswith("- ") else s
    # drop trailing glyphs and status tags
    s = _re.sub(r"\s*[❗⚡🚩]+\s*$", "", s)
    s = _re.sub(r"\s*\[(Done[^\]]*|Overdue|all-day)\]\s*", " ", s)
    # drop a leading HH:MM after the list bracket
    s = _re.sub(r"(\]\s*)\d{2}:\d{2}\s+", r"\1", s)
    return s.strip()


def compute_diff(old_content: str, new_content: str) -> str:
    """Structured diff keyed on reminder identity + calendar line-set.

    Reminders: done / new / changed. Calendar: added / removed lines.
    """
    if not old_content or not new_content:
        return ""

    old_rem, old_cal = _split_sections(old_content)
    new_rem, new_cal = _split_sections(new_content)

    old_map = {_rem_identity(l): l for l in old_rem}
    new_map = {_rem_identity(l): l for l in new_rem}

    parts: list[str] = []

    for ident, line in new_map.items():
        old_line = old_map.get(ident)
        is_done = "[Done" in line
        if old_line is None:
            parts.append(f"+{line[2:]}" if line.startswith("- ") else f"+{line}")
        elif old_line != line:
            tag = "✓" if is_done else "~"
            parts.append(f"{tag}{line[2:]}" if line.startswith("- ") else f"{tag}{line}")

    for ident, line in old_map.items():
        if ident not in new_map:
            parts.append(f"-{line[2:]}" if line.startswith("- ") else f"-{line}")

    old_cal_set = set(old_cal)
    new_cal_set = set(new_cal)
    for line in new_cal:
        if line not in old_cal_set:
            parts.append(f"+{line[2:]}" if line.startswith("- ") else f"+{line}")
    for line in old_cal:
        if line not in new_cal_set:
            parts.append(f"-{line[2:]}" if line.startswith("- ") else f"-{line}")

    if not parts:
        return ""
    header = "Schedule update:"
    body = "\n".join(parts[:20])
    if len(parts) > 20:
        body += f"\n… +{len(parts) - 20} more"
    return f"{header}\n{body}"


def _snapshot_dir() -> Path:
    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return _SNAPSHOT_DIR


def _mtime_path(session_id: str) -> Path:
    return _snapshot_dir() / f"{session_id}.mtime"


def _content_path(session_id: str) -> Path:
    return _snapshot_dir() / f"{session_id}.content"


def _date_path(session_id: str) -> Path:
    return _snapshot_dir() / f"{session_id}.date"


def check_and_inject(
    session_id: str,
    cadence_bin: str | None = None,
    daily_path: str | None = None,
) -> str | None:
    if not is_enabled():
        return None

    daily_path = daily_path or _DAILY_PATH
    binary = cadence_bin or _cadence_bin()

    tz = config.get_tz()
    today = datetime.now(timezone.utc).astimezone(tz).strftime("%Y-%m-%d")

    mt_file = _mtime_path(session_id)
    content_file = _content_path(session_id)
    date_file = _date_path(session_id)

    prev_date = ""
    try:
        if date_file.exists():
            prev_date = date_file.read_text().strip()
    except OSError:
        pass
    date_rolled = bool(prev_date) and prev_date != today

    data_mt = get_data_mtime()
    last_mt = 0.0
    try:
        if mt_file.exists():
            last_mt = float(mt_file.read_text().strip())
    except (ValueError, OSError):
        pass

    # Date rollover forces a full re-render BEFORE the mtime early-exit.
    if not date_rolled and last_mt and abs(data_mt - last_mt) < 0.01:
        return None

    old_content = ""
    try:
        if content_file.exists():
            old_content = content_file.read_text()
    except OSError:
        pass

    content, _ = refresh_daily(binary, daily_path)
    if not content:
        return None

    for f, val in ((mt_file, str(data_mt)), (content_file, content), (date_file, today)):
        try:
            f.write_text(val)
        except OSError:
            pass

    # First injection this session, or forced full re-render on date change.
    if not old_content or date_rolled:
        return content

    return compute_diff(old_content, content) or None
