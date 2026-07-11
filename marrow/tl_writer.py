"""tl_add / tl_update core: self-authored timeline rows.

One call -> a single events row (role='tl', channel=platform). No affect table
write: the affect phrase lives verbatim inside content, importance lives in
events.imp. Render/reconcile treat these rows by their tl:e:<event_id> anchor.

Format: HH:mm[-HH:mm] 【N word♡Y word】body [i]
  N = user affect, Y = assistant affect. word <=8 chars.
  Single-side rows: just 【N word】 or 【Y word】.
  i = composite 1-5 (events.imp), one value for the whole row, not per side,
  rendered at the end as " [i]".
  body <=50 chars (config: tl.body_max).
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path

from . import config as _config

_TZ = _config.get_tz()
_WORD_MAX = 8


def _body_max() -> int:
    return int(_config.load().get("tl", {}).get("body_max", 50))
_LABEL_RE = re.compile(r"^\s*(【[^】]*】)?(.*)$", re.DOTALL)
_TRAIL_IMP_RE = re.compile(r"\s*\[\d\]\s*$")


class TlError(ValueError):
    """Validation failure surfaced to the MCP caller."""


# ── validation helpers ───────────────────────────────────────────────────────

def _clamp_1_5(x, name: str, default: int) -> int:
    if x is None:
        return default
    try:
        v = int(x)
    except (TypeError, ValueError):
        raise TlError(f"{name} must be an integer 1-5")
    if not 1 <= v <= 5:
        raise TlError(f"{name}={v} out of range 1-5")
    return v


def _check_word(word: str | None, side: str) -> str | None:
    if not word:
        return None
    word = word.strip()
    if len(word) > _WORD_MAX:
        raise TlError(f"{side} word {word!r} exceeds {_WORD_MAX} chars")
    return word


def _parse_timerange(timerange: str) -> tuple[str, str | None]:
    tr = (timerange or "").strip()
    if not tr:
        raise TlError("timerange required (HH:mm-HH:mm or HH:mm)")
    parts = tr.split("-")
    if len(parts) == 1:
        return _norm_hhmm(parts[0]), None
    if len(parts) == 2:
        return _norm_hhmm(parts[0]), _norm_hhmm(parts[1])
    raise TlError(f"bad timerange {timerange!r}")


def _norm_hhmm(s: str) -> str:
    s = s.strip()
    try:
        h, m = int(s[:2]), int(s[3:5])
        if s[2] != ":" or not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
    except (ValueError, IndexError):
        raise TlError(f"bad time {s!r} (expected HH:mm)")
    return f"{h:02d}:{m:02d}"


def _hhmm_to_utc(hhmm: str, base_date: _dt.date, now_local: _dt.datetime) -> str:
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    local = _dt.datetime(base_date.year, base_date.month, base_date.day,
                         h, m, tzinfo=_TZ)
    return local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compose_label(n_word, y_word) -> str:
    seg = []
    if n_word:
        seg.append(f"N{n_word}")
    if y_word:
        seg.append(f"Y{y_word}")
    return "♡".join(seg)


def _split_content(content: str) -> tuple[str, str]:
    """Split stored content into (label_bracket, body). body has the trailing
    ' [i]' composite marker stripped (imp lives in events.imp, not content)."""
    m = _LABEL_RE.match(content or "")
    if not m:
        return "", _TRAIL_IMP_RE.sub("", (content or "")).strip()
    label = m.group(1) or ""
    body = _TRAIL_IMP_RE.sub("", (m.group(2) or "")).strip()
    return label, body


def _platform() -> str:
    return (os.environ.get("MARROW_CHANNEL") or "").strip() or "cli"


# ── write path ───────────────────────────────────────────────────────────────

def tl_add(conn, timerange: str, body: str,
           n_word: str | None = None,
           y_word: str | None = None,
           importance: int | None = None,
           sid: str | None = None) -> dict:
    """Insert one self timeline row (events only) in a single txn."""
    body = (body or "").strip()
    if not body:
        raise TlError("body required")
    body_max = _body_max()
    if len(body) > body_max:
        raise TlError(f"body exceeds {body_max} chars: {len(body)}")

    n_word = _check_word(n_word, "N")
    y_word = _check_word(y_word, "Y")
    if not n_word and not y_word:
        raise TlError("at least one of n_word / y_word required")
    imp = _clamp_1_5(importance, "importance", 3)

    label = _compose_label(n_word, y_word)
    content = f"【{label}】{body} [{imp}]" if label else f"{body} [{imp}]"

    hhmm_start, hhmm_end = _parse_timerange(timerange)
    now_local = _dt.datetime.now(_TZ)
    base_date = now_local.date()
    ts_start = _hhmm_to_utc(hhmm_start, base_date, now_local)
    now_utc = now_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if ts_start > now_utc:
        base_date -= _dt.timedelta(days=1)
        ts_start = _hhmm_to_utc(hhmm_start, base_date, now_local)
    ts_end = None
    if hhmm_end is not None:
        ts_end = _hhmm_to_utc(hhmm_end, base_date, now_local)
        if ts_end < ts_start:  # range crosses midnight
            ts_end = _hhmm_to_utc(hhmm_end, base_date + _dt.timedelta(days=1), now_local)

    if not sid:
        from .timeline import _query_current_sid
        sid = _query_current_sid(conn)
    if not sid:
        import secrets
        sid = "self:" + secrets.token_hex(4)

    from . import tl_sync
    prev_hhmm = tl_sync.last_tl_hhmm(conn, sid)
    prev_hint = (f" (previous tl this session: {prev_hhmm})"
                 if prev_hhmm != "n/a" else " (first tl this session)")

    with conn:
        cur = conn.execute(
            "INSERT INTO events (session_id, timestamp, role, content, channel,"
            " ts_start, ts_end, imp) VALUES (?, ?, 'tl', ?, ?, ?, ?, ?)",
            (sid, ts_start, content, _platform(), ts_start, ts_end, imp),
        )
        event_id = cur.lastrowid
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'tl_add', ?)",
            (sid, f"event_id={event_id} label={label!r}"),
        )
    if sid:
        from . import tl_nudge
        tl_nudge.reset(sid)
    return {"ok": True, "event_id": event_id,
            "line": render_line(hhmm_start, hhmm_end, content) + prev_hint}


def tl_update(conn, event_id: int, timerange: str | None = None,
              body: str | None = None,
              n_word: str | None = None,
              y_word: str | None = None,
              importance: int | None = None) -> dict:
    """Update an existing self row in place. Only provided fields change."""
    ev = conn.execute(
        "SELECT session_id, timestamp, ts_start, ts_end, content, role, imp"
        " FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    if ev is None:
        raise TlError(f"event_id {event_id} not found")
    if ev["role"] != "tl":
        raise TlError(f"event_id {event_id} is not a tl row (role={ev['role']!r})")

    now_local = _dt.datetime.now(_TZ)
    ts_start = ev["ts_start"] or ev["timestamp"]
    ts_end = ev["ts_end"]
    if timerange is not None:
        hhmm_start, hhmm_end = _parse_timerange(timerange)
        base_date = now_local.date()
        ts_start = _hhmm_to_utc(hhmm_start, base_date, now_local)
        ts_end = _hhmm_to_utc(hhmm_end, base_date, now_local) if hhmm_end else None
        if ts_end and ts_end < ts_start:
            ts_end = _hhmm_to_utc(hhmm_end, base_date + _dt.timedelta(days=1), now_local)

    label_part, body_part = _split_content(ev["content"])
    if body is not None:
        body_part = body.strip()
        if not body_part:
            raise TlError("body cannot be empty")
        body_max = _body_max()
        if len(body_part) > body_max:
            raise TlError(f"body exceeds {body_max} chars")
    n_word = _check_word(n_word, "N")
    y_word = _check_word(y_word, "Y")
    if n_word or y_word:
        label_part = f"【{_compose_label(n_word, y_word)}】"
    imp = _clamp_1_5(importance, "importance", ev["imp"] or 3)
    new_content = f"{label_part}{body_part} [{imp}]"

    with conn:
        conn.execute(
            "UPDATE events SET content=?, ts_start=?, ts_end=?, timestamp=?, imp=?"
            " WHERE id=?",
            (new_content, ts_start, ts_end, ts_start, imp, event_id),
        )
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'tl_update', ?)",
            (ev["session_id"], f"event_id={event_id}"),
        )
    from .timeline import _hhmm_local
    hhmm_start = _hhmm_local(ts_start)
    hhmm_end = _hhmm_local(ts_end) if ts_end else None
    _sync_dashboard_line(event_id, hhmm_start, hhmm_end, new_content)
    return {"ok": True, "event_id": event_id}


# ── render helper (shared with timeline) ─────────────────────────────────────

def render_line(hhmm_start: str, hhmm_end: str | None, content: str) -> str:
    rng = f"{hhmm_start}-{hhmm_end}" if hhmm_end else hhmm_start
    return f"{rng} {content}"


# ── dashboard sync (md must mirror DB or reconcile reverts the edit) ─────────

def _dashboard_path() -> Path:
    return Path.home() / "Desktop" / "NY" / "dashboard.md"


def _sync_dashboard_line(event_id: int, hhmm_start: str, hhmm_end: str | None,
                          content: str) -> bool:
    """Rewrite the dashboard.md line anchored `<!-- tl:e:<event_id> -->` so it
    matches the just-written DB content. Without this, the resident md->DB
    reconcile (_reconcile_self_edit) treats the stale md line as a user edit
    and reverts the update within seconds. No-op if the row isn't rendered
    yet (anchor absent) — the next render will pick up the DB content."""
    dash = _dashboard_path()
    if not dash.exists():
        return False
    anchor = f"<!-- tl:e:{event_id} -->"
    new_line = f"{render_line(hhmm_start, hhmm_end, content)} {anchor}"
    lines = dash.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.rstrip("\n").endswith(anchor):
            eol = "\n" if line.endswith("\n") else ""
            lines[i] = new_line + eol
            dash.write_text("".join(lines), encoding="utf-8")
            return True
    return False
