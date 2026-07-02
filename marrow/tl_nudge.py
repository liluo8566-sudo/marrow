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


def turns_since_last_tl_add(conn, sid: str) -> int:
    """Assistant turns recorded for sid since its last tl_add (all if none)."""
    last = conn.execute(
        "SELECT MAX(occurred_at) AS t FROM audit_log"
        " WHERE action='tl_add' AND target_id=?",
        (sid,),
    ).fetchone()
    since = last["t"] if last else None
    if since:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM events"
            " WHERE session_id=? AND role='assistant' AND timestamp > ?",
            (sid, since),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM events"
            " WHERE session_id=? AND role='assistant'",
            (sid,),
        ).fetchone()
    return int(row["n"]) if row else 0


def should_nudge(turns_since_add: int) -> bool:
    """Fire once every `threshold` turns without a tl_add, when enabled."""
    if not enabled():
        return False
    t = threshold()
    return turns_since_add >= t and turns_since_add % t == 0


def maybe_nudge(conn, sid: str) -> str | None:
    """Return injection text when the sid is due a nudge, else None."""
    if not enabled() or not sid:
        return None
    if should_nudge(turns_since_last_tl_add(conn, sid)):
        return nudge_text() or None
    return None
