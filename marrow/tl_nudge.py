"""tl_add nudge — hint a session that has gone N turns without a tl_add.

DEFAULT OFF ([tl_nudge].enabled=false). Pure counting + text plumbing; the
caller (a per-turn hook) decides when to inject. Injection text is data
(marrow/data/tl_nudge.txt), pending user review.
"""
from __future__ import annotations

from pathlib import Path

from . import config


def _cfg() -> dict:
    return config.load().get("tl_nudge", {}) or {}


# ── session-silent state (/tl-) ──────────────────────────────────────────────

def _silent_dir() -> Path:
    return config.DATA_DIR / "state" / "tl_silent"


def set_silent(sid: str) -> None:
    """Mark a session silent: mutes the nudge, no self writes. Dies with sid."""
    if not sid:
        return
    d = _silent_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / sid).write_text("1", encoding="utf-8")


def is_silent(sid: str) -> bool:
    return bool(sid) and (_silent_dir() / sid).exists()


def enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def threshold() -> int:
    try:
        return max(1, int(_cfg().get("threshold", 5)))
    except (TypeError, ValueError):
        return 5


def nudge_text() -> str:
    p = _cfg().get("text_file") or ""
    path = Path(p).expanduser() if p else Path(__file__).parent / "data" / "tl_nudge.txt"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    # Drop the leading "pending review" header (everything up to the --- fence).
    if "\n---\n" in raw:
        raw = raw.split("\n---\n", 1)[1]
    return raw.strip()


# ── per-turn counter state (state/tl_nudge/<sid>) ────────────────────────────

def _counter_dir() -> Path:
    return config.DATA_DIR / "state" / "tl_nudge"


def _load_count(sid: str) -> int:
    try:
        return int((_counter_dir() / sid).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _save_count(sid: str, count: int) -> None:
    d = _counter_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / sid).write_text(str(count), encoding="utf-8")


def reset(sid: str) -> None:
    """Zero sid's turn counter (called after a successful tl_add)."""
    if not sid:
        return
    try:
        (_counter_dir() / sid).unlink(missing_ok=True)
    except OSError:
        pass


def maybe_nudge(conn, sid: str) -> str | None:
    """Increment sid's per-turn counter; return injection text at threshold.

    conn is unused (the counter is a state file, not a DB query) but kept in
    the signature since hooks.py already passes it in.
    """
    if not enabled() or not sid or is_silent(sid):
        return None
    count = _load_count(sid) + 1
    if count >= threshold():
        _save_count(sid, 0)
        text = nudge_text()
        if not text:
            return None
        if "{last_tl}" in text:
            from . import tl_sync
            text = text.replace("{last_tl}", tl_sync.last_tl_hhmm(conn, sid))
        return text
    _save_count(sid, count)
    return None
