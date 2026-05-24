"""Pick best-of-day quote from the monthly goose reaction file, upsert to DB."""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from .llm import LLMClient, LLMError  # noqa: F401 — re-exported for patching

_LOG = logging.getLogger(__name__)
_QUOTE_DIR = Path.home() / ".config" / "marrow" / "goose_log"
_LINE_RE = re.compile(r"^- `\d{2}:\d{2}` (.+)$")

_SYSTEM_PROMPT = (
    "From the 铁锅 (goose son) reactions below, pick the ONE most memorable line. "
    "Criteria: humour/wit, captures a real Lumi-Stellan interaction moment, "
    "worth re-reading a year later. "
    "Output ONLY the selected line verbatim with no prefix, suffix, quotes, or explanation. "
    "No translation, no edits."
)


def _parse_day_block(date: str) -> list[str]:
    """Return quote texts for `date` (YYYY-MM-DD) from the monthly file."""
    monthly = _QUOTE_DIR / f"{date[:7]}.md"
    if not monthly.exists():
        return []
    text = monthly.read_text(encoding="utf-8")
    candidates: list[str] = []
    in_block = False
    for line in text.splitlines():
        s = line.strip()
        if s == f"### {date}":
            in_block = True
            continue
        if in_block:
            if s.startswith("### ") and s != f"### {date}":
                break
            m = _LINE_RE.match(s)
            if m:
                candidates.append(m.group(1))
    return candidates


def _call_haiku(candidates: list[str]) -> str | None:
    """Ask Haiku to pick the best candidate. Returns stripped text or None."""
    try:
        client = LLMClient()
        body = _SYSTEM_PROMPT + "\n\n" + "\n".join(candidates)
        result = client.call("goose_bites", body, tier="cheap")
        picked = result.strip()
        if picked in candidates:
            return picked
        _LOG.warning("goose_bites: Haiku output not in candidates, falling back to longest")
        return max(candidates, key=len)
    except Exception as e:
        _LOG.warning("goose_bites: LLM call failed: %s", e)
        return None


def _upsert(conn: sqlite3.Connection, date: str, quote: str) -> None:
    """INSERT OR REPLACE the day's best quote."""
    existing = conn.execute(
        "SELECT id FROM goose_bites WHERE date = ?", (date,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE goose_bites SET bites=?, best=1, session_id=NULL, source_hash=NULL "
            "WHERE date=?",
            (quote, date),
        )
    else:
        conn.execute(
            "INSERT INTO goose_bites (date, session_id, bites, best, source_hash)"
            " VALUES (?, NULL, ?, 1, NULL)",
            (date, quote),
        )
    conn.commit()


def select_quote_for_date(conn: sqlite3.Connection, date: str) -> str | None:
    """Pick best quote for `date` (YYYY-MM-DD), upsert to DB, return quote or None."""
    try:
        candidates = _parse_day_block(date)
    except Exception as e:
        _LOG.warning("goose_bites: failed to parse day block for %s: %s", date, e)
        return None

    if not candidates:
        _LOG.warning("goose_bites: no candidates for %s", date)
        return None

    if len(candidates) == 1:
        quote = candidates[0]
    else:
        quote = _call_haiku(candidates)
        if quote is None:
            return None

    try:
        _upsert(conn, date, quote)
    except Exception as e:
        _LOG.warning("goose_bites: DB upsert failed for %s: %s", date, e)
        return None

    return quote
