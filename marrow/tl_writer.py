"""tl_add / tl_update core: self-authored timeline rows.

One call -> events(channel='self') + affect(FK event_id) in a single txn.
Render/reconcile treat these rows by their tl:e:<event_id> anchor.

Format: HH:mm[-HH:mm] 【N word·n | Y word·n】body
  N = user affect, Y = assistant affect. word <=6 chars, intensity 1-5.
  body <=30 chars. V/A default-mapped from the primary word; explicit
  valence/arousal override wins.
"""
from __future__ import annotations

import datetime as _dt
import functools
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

_MELB = ZoneInfo("Australia/Melbourne")
_WORD_MAX = 6
_BODY_MAX = 30


class TlError(ValueError):
    """Validation failure surfaced to the MCP caller."""


# ── affect word -> V/A map ───────────────────────────────────────────────────

def _words_path() -> Path:
    p = (config.load().get("affect", {}) or {}).get("words_file") or ""
    if p:
        return Path(p).expanduser()
    return Path(__file__).parent / "data" / "affect_words.toml"


@functools.lru_cache(maxsize=4)
def _load_words_cached(path_str: str, mtime: float) -> dict[str, tuple[float, float]]:
    with open(path_str, "rb") as f:
        raw = tomllib.load(f)
    out: dict[str, tuple[float, float]] = {}
    for word, va in (raw.get("words") or {}).items():
        out[word] = (float(va["v"]), float(va["a"]))
    return out


def load_words() -> dict[str, tuple[float, float]]:
    path = _words_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    return _load_words_cached(str(path), mtime)


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


def _hhmm_to_utc(hhmm: str, base_date: _dt.date, now_melb: _dt.datetime) -> str:
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    local = _dt.datetime(base_date.year, base_date.month, base_date.day,
                         h, m, tzinfo=_MELB)
    return local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compose_label(n_word, n_int, y_word, y_int) -> str:
    seg = []
    if n_word:
        seg.append(f"N {n_word}·{n_int}")
    if y_word:
        seg.append(f"Y {y_word}·{y_int}")
    return " | ".join(seg)


def _map_va(primary_word: str, valence, arousal) -> tuple[float, float]:
    """Explicit override wins; else map primary word; else error."""
    words = load_words()
    v = float(valence) if valence is not None else None
    a = float(arousal) if arousal is not None else None
    if v is None or a is None:
        mapped = words.get(primary_word)
        if mapped is None:
            known = ", ".join(sorted(words)) or "(map empty)"
            raise TlError(
                f"word {primary_word!r} not in affect map and no explicit "
                f"valence/arousal given. Known words: {known}"
            )
        if v is None:
            v = mapped[0]
        if a is None:
            a = mapped[1]
    return v, a


def _next_ep(conn, date: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(ep), 0) AS m FROM affect WHERE date = ?", (date,)
    ).fetchone()
    return int(row["m"]) + 1


def _diary_date(ts_utc: str) -> str:
    d = _dt.datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    local = d.astimezone(_MELB)
    if local.hour < 6:
        local -= _dt.timedelta(days=1)
    return local.date().isoformat()


# ── write path ───────────────────────────────────────────────────────────────

def tl_add(conn, timerange: str, body: str,
           n_word: str | None = None, n_intensity: int | None = None,
           y_word: str | None = None, y_intensity: int | None = None,
           importance: int | None = None,
           valence=None, arousal=None,
           description: str | None = None, unresolved: int | None = None,
           sid: str | None = None) -> dict:
    """Insert one self timeline row (events + affect) in a single txn."""
    body = (body or "").strip()
    if not body:
        raise TlError("body required")
    if len(body) > _BODY_MAX:
        raise TlError(f"body exceeds {_BODY_MAX} chars: {len(body)}")

    n_word = _check_word(n_word, "N")
    y_word = _check_word(y_word, "Y")
    if not n_word and not y_word:
        raise TlError("at least one of n_word / y_word required")
    n_int = _clamp_1_5(n_intensity, "n_intensity", 3) if n_word else 3
    y_int = _clamp_1_5(y_intensity, "y_intensity", 3) if y_word else 3
    imp = _clamp_1_5(importance, "importance", 2)

    primary_word = n_word or y_word
    v, a = _map_va(primary_word, valence, arousal)
    label = _compose_label(n_word, n_int, y_word, y_int)

    hhmm_start, hhmm_end = _parse_timerange(timerange)
    now_melb = _dt.datetime.now(_MELB)
    base_date = now_melb.date()
    ts_start = _hhmm_to_utc(hhmm_start, base_date, now_melb)
    if ts_start > now_melb.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"):
        base_date -= _dt.timedelta(days=1)
        ts_start = _hhmm_to_utc(hhmm_start, base_date, now_melb)
    ts_end = None
    if hhmm_end is not None:
        ts_end = _hhmm_to_utc(hhmm_end, base_date, now_melb)
        if ts_end < ts_start:  # range crosses midnight
            ts_end = _hhmm_to_utc(hhmm_end, base_date + _dt.timedelta(days=1), now_melb)

    if not sid:
        from .timeline import _query_current_sid
        sid = _query_current_sid(conn)
    if not sid:
        import secrets
        sid = "self:" + secrets.token_hex(4)

    date = _diary_date(ts_start)
    unresolved_i = 1 if unresolved else 0
    desc = (description or "").strip() or None

    with conn:
        cur = conn.execute(
            "INSERT INTO events (session_id, timestamp, role, content, channel,"
            " ts_start, ts_end) VALUES (?, ?, 'assistant', ?, 'self', ?, ?)",
            (sid, ts_start, body, ts_start, ts_end),
        )
        event_id = cur.lastrowid
        ep = _next_ep(conn, date)
        cur2 = conn.execute(
            "INSERT INTO affect (date, ep, event_id, valence, arousal,"
            " importance, label, description, source, unresolved)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'tl_add', ?)",
            (date, ep, event_id, v, a, imp, label, desc, unresolved_i),
        )
        affect_id = cur2.lastrowid
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'tl_add', ?)",
            (sid, f"event_id={event_id} label={label!r}"),
        )
    return {"ok": True, "event_id": event_id, "affect_id": affect_id,
            "line": render_line(hhmm_start, hhmm_end, label, body)}


def tl_update(conn, event_id: int, timerange: str | None = None,
              body: str | None = None,
              n_word: str | None = None, n_intensity: int | None = None,
              y_word: str | None = None, y_intensity: int | None = None,
              importance: int | None = None,
              valence=None, arousal=None,
              description: str | None = None,
              unresolved: int | None = None) -> dict:
    """Update an existing self row (events + its affect row). Only provided
    fields change; timerange/body/affect words each optional."""
    ev = conn.execute(
        "SELECT session_id, timestamp, ts_start, ts_end, content, channel"
        " FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    if ev is None:
        raise TlError(f"event_id {event_id} not found")
    if ev["channel"] != "self":
        raise TlError(f"event_id {event_id} is not a self row (channel={ev['channel']!r})")
    af = conn.execute(
        "SELECT id, valence, arousal, importance, label, description, unresolved"
        " FROM affect WHERE event_id = ? ORDER BY id DESC LIMIT 1", (event_id,)
    ).fetchone()

    now_melb = _dt.datetime.now(_MELB)
    ts_start = ev["ts_start"] or ev["timestamp"]
    ts_end = ev["ts_end"]
    if timerange is not None:
        hhmm_start, hhmm_end = _parse_timerange(timerange)
        base_date = now_melb.date()
        ts_start = _hhmm_to_utc(hhmm_start, base_date, now_melb)
        ts_end = _hhmm_to_utc(hhmm_end, base_date, now_melb) if hhmm_end else None
        if ts_end and ts_end < ts_start:
            ts_end = _hhmm_to_utc(hhmm_end, base_date + _dt.timedelta(days=1), now_melb)

    new_body = ev["content"]
    if body is not None:
        new_body = body.strip()
        if not new_body:
            raise TlError("body cannot be empty")
        if len(new_body) > _BODY_MAX:
            raise TlError(f"body exceeds {_BODY_MAX} chars")

    # Affect side updates
    cur_label = af["label"] if af else ""
    n_word = _check_word(n_word, "N")
    y_word = _check_word(y_word, "Y")
    now_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with conn:
        conn.execute(
            "UPDATE events SET content=?, ts_start=?, ts_end=?, timestamp=?"
            " WHERE id=?",
            (new_body, ts_start, ts_end, ts_start, event_id),
        )
        if af is not None:
            new_label = cur_label
            v = af["valence"]
            a = af["arousal"]
            if n_word or y_word:
                n_int = _clamp_1_5(n_intensity, "n_intensity", 3)
                y_int = _clamp_1_5(y_intensity, "y_intensity", 3)
                new_label = _compose_label(n_word, n_int, y_word, y_int)
                primary = n_word or y_word
                v, a = _map_va(primary, valence, arousal)
            else:
                if valence is not None:
                    v = float(valence)
                if arousal is not None:
                    a = float(arousal)
            imp = _clamp_1_5(importance, "importance", af["importance"])
            desc = af["description"]
            if description is not None:
                desc = description.strip() or None
            unres = af["unresolved"]
            if unresolved is not None:
                unres = 1 if unresolved else 0
            conn.execute(
                "UPDATE affect SET valence=?, arousal=?, importance=?, label=?,"
                " description=?, unresolved=?, updated_at=? WHERE id=?",
                (v, a, imp, new_label, desc, unres, now_iso, af["id"]),
            )
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'tl_update', ?)",
            (ev["session_id"], f"event_id={event_id}"),
        )
    return {"ok": True, "event_id": event_id}


# ── render helper (shared with timeline) ─────────────────────────────────────

def render_line(hhmm_start: str, hhmm_end: str | None,
                label: str, body: str) -> str:
    rng = f"{hhmm_start}-{hhmm_end}" if hhmm_end else hhmm_start
    return f"{rng} 【{label}】{body}"
