"""Day-paragraph writer — one 100-150 char overview per day.

Composes the day's self-timeline (role='tl' events, verbatim 【label】body [imp])
plus optional calendar into a single mid-tier LLM call, parses PARA:/TONE:
markers, and fills the diary.overview column WITHOUT touching diary.content.
Column-UPDATE on an existing diary row; INSERT a stub row otherwise. Never
DELETE.

Also hosts the diary free-write prompt loader (load_diary_prompt) — groundwork
for the cortex free-write slot. No diary writer here; daily.py keeps its inline
DIARY_PROMPT for now.

CLI: python -m marrow.day_para --day YYYY-MM-DD [--force]

DB timestamps are UTC; local dates/times resolved via config.get_tz().
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sys
from pathlib import Path

from . import config, repo, schedule, storage
from .llm import LLMClient, LLMError


class _Keep(dict):
    """format_map dict that leaves unknown {placeholders} literally intact,
    so persona/config fill can run in one stage and leave runtime slots
    ({date}/{timeline}/{calendar} · {date}/{digest}) for a later fill."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


# ── template loaders ─────────────────────────────────────────────────────────

def _load_template(path_cfg: str | None, default_name: str) -> str:
    """Config override path or packaged default (tl_nudge mechanism)."""
    p = (path_cfg or "").strip()
    path = (Path(p).expanduser() if p
            else Path(__file__).parent / "data" / default_name)
    return path.read_text(encoding="utf-8")


def render_day_para_prompt(cfg: dict | None = None) -> str:
    """Load prompt_day_para.txt, fill persona + chars_min/max; leave the
    runtime slots {date}/{timeline}/{calendar} intact for write_day_para."""
    cfg = cfg if cfg is not None else config.load()
    dp = cfg.get("day_para", {}) or {}
    p = config.persona()
    template = _load_template(dp.get("prompt_file"), "prompt_day_para.txt")
    return template.format_map(_Keep(
        user_name=p["user_name"],
        assistant_name=p["assistant_name"],
        chars_min=dp.get("chars_min", 100),
        chars_max=dp.get("chars_max", 150),
    ))


def load_diary_prompt(cfg: dict | None = None) -> str:
    """Load prompt_diary.txt, fill persona + length_range/style; leave the
    runtime slots {date}/{digest} intact. Loader only — no writer wired."""
    cfg = cfg if cfg is not None else config.load()
    d = cfg.get("diary", {}) or {}
    p = config.persona()
    template = _load_template(d.get("prompt_file"), "prompt_diary.txt")
    return template.format_map(_Keep(
        user_name=p["user_name"],
        assistant_name=p["assistant_name"],
        length_range=d.get("length_range", "300-800"),
        style=d.get("style", ""),
    ))


# ── timeline + calendar readers ──────────────────────────────────────────────

def _hhmm(utc_iso: str, tz) -> str:
    s = (utc_iso or "").strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return "??:??"
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(tz).strftime("%H:%M")


def _day_bounds_utc(date: str, tz) -> tuple[str, str]:
    """Local-calendar-day [00:00, next 00:00) → UTC ISO-Z bounds.
    Each midnight localized independently so DST transitions stay correct."""
    d = _dt.date.fromisoformat(date)
    start = _dt.datetime.combine(d, _dt.time(0, 0), tzinfo=tz)
    end = _dt.datetime.combine(d + _dt.timedelta(days=1), _dt.time(0, 0),
                               tzinfo=tz)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (start.astimezone(_dt.timezone.utc).strftime(fmt),
            end.astimezone(_dt.timezone.utc).strftime(fmt))


def _read_tl_lines(conn, date: str) -> list[str]:
    """role='tl' rows whose local ts_start date == `date`, time-ordered,
    rendered `HH:MM[-HH:MM] content` (content already holds 【label】body [imp])."""
    tz = config.get_tz()
    lo, hi = _day_bounds_utc(date, tz)
    rows = conn.execute(
        "SELECT ts_start, ts_end, timestamp, content FROM events"
        " WHERE role='tl'"
        " AND COALESCE(ts_start, timestamp) >= ?"
        " AND COALESCE(ts_start, timestamp) < ?"
        " ORDER BY COALESCE(ts_start, timestamp) ASC",
        (lo, hi),
    ).fetchall()
    out: list[str] = []
    for r in rows:
        ts_start = r["ts_start"] or r["timestamp"]
        hhmm = _hhmm(ts_start, tz)
        end = r["ts_end"]
        rng = f"{hhmm}-{_hhmm(end, tz)}" if end else hhmm
        body = (r["content"] or "").strip()
        out.append(f"{rng} {body}".rstrip())
    return out


def _read_calendar(date: str, cfg: dict | None = None) -> str:
    """cadence `cal read <date> --human`. "" when include_calendar is off or
    on any failure (missing binary, timeout, non-zero exit)."""
    cfg = cfg if cfg is not None else config.load()
    dp = cfg.get("day_para", {}) or {}
    if not dp.get("include_calendar", True):
        return ""
    binary = schedule._cadence_bin()
    if not os.path.isfile(binary):
        return ""
    return schedule._run_cadence(["cal", "read", date, "--human"], binary)


# ── marker parse ─────────────────────────────────────────────────────────────

_PARA_RE = re.compile(r"PARA[：:]\s*(.+)", re.IGNORECASE)
_TONE_RE = re.compile(r"TONE[：:]\s*(.+)", re.IGNORECASE)


def _parse_para_tone(raw: str) -> tuple[str | None, str | None]:
    """Bottom-up scan for PARA/TONE markers. Missing TONE tolerated;
    missing PARA → (None, tone)."""
    para: str | None = None
    tone: str | None = None
    for ln in reversed((raw or "").splitlines()):
        t_match = _TONE_RE.search(ln)
        p_match = _PARA_RE.search(ln)
        if tone is None and t_match:
            tone = t_match.group(1).strip() or None
        elif para is None and p_match:
            para = p_match.group(1).strip() or None
    return para, tone


# ── writer ───────────────────────────────────────────────────────────────────

_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def write_day_para(conn, date: str, llm, *, db: str | None = None,
                   force: bool = False) -> bool:
    """Compose + persist the day paragraph into diary.overview. Returns True
    on write. No tl rows → False (no write). LLM failure / missing PARA →
    alert + False. Existing overview → skip unless force. Never DELETE."""
    tl_lines = _read_tl_lines(conn, date)
    if not tl_lines:
        return False

    # Skip early (before spending an LLM call) when the row already carries
    # an overview and we are not forcing.
    existing = conn.execute(
        "SELECT overview FROM diary WHERE date=?", (date,)).fetchone()
    if (existing is not None and (existing["overview"] or "").strip()
            and not force):
        return False

    cfg = config.load()
    cal = _read_calendar(date, cfg)
    prompt = render_day_para_prompt(cfg).format_map(_Keep(
        date=date,
        timeline="\n".join(tl_lines),
        calendar=cal or "(无)",
    ))

    try:
        raw = llm.call("day_para", prompt, tier="mid")
    except LLMError as e:
        repo.add_alert("warn", "routine", f"day_para_failed:{date}",
                       source="day_para.py", db=db,
                       message=f"day_para {date} llm call failed: {e}")
        return False

    para, tone = _parse_para_tone(raw or "")
    if not para:
        repo.add_alert("warn", "routine", f"day_para_noparse:{date}",
                       source="day_para.py", db=db,
                       message=f"day_para {date} produced no PARA marker")
        return False

    if existing is not None:
        with conn:
            conn.execute(
                "UPDATE diary SET overview=?, tone=COALESCE(?, tone),"
                f" updated_at={_NOW} WHERE date=?",
                (para, tone, date))
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('diary', ?, 'day_para', ?)",
                (date, f"day_para overview updated for {date} "
                       f"(tone={'ok' if tone else 'missing'})"))
    else:
        with conn:
            conn.execute(
                "INSERT INTO diary (date, content, overview, tone,"
                f" session_ids, updated_at) VALUES (?, '—', ?, ?, '', {_NOW})",
                (date, para, tone))
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('diary', ?, 'day_para', ?)",
                (date, f"day_para stub inserted for {date} "
                       f"(tone={'ok' if tone else 'missing'})"))
    return True


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    force = "--force" in args
    day = None
    if "--day" in args:
        i = args.index("--day")
        if i + 1 < len(args):
            day = args[i + 1]
    if not day:
        print("usage: python -m marrow.day_para --day YYYY-MM-DD [--force]",
              flush=True)
        return 2
    db = config.db_path()
    conn = storage.connect(db)
    llm = LLMClient(on_alert=lambda s, t, m, src: repo.add_alert(
        s, t, m, src, db=db))
    try:
        wrote = write_day_para(conn, day, llm, db=db, force=force)
        print(f"day_para {day}: {'wrote' if wrote else 'skip'}", flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
