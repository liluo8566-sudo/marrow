"""Nightly 04:00 routine: previous day's events -> digest -> diary + lessons.

Pipeline (DESIGN L144): haiku compresses the day's clean events to a digest;
sonnet writes the diary narrative; haiku extracts lessons from the digest.
Idempotent — a date with a diary row is skipped, so SessionStart catchup is
just a re-run over event-days lacking a diary. No silent failure: any error
raises one alert.

Prompt bodies below were reviewed and hand-edited by Lumi 2026-05-17
(DESIGN L53 satisfied).
"""
from __future__ import annotations

import datetime as _dt

from . import config, repo, storage
from .llm import LLMClient

# ── prompt bodies — Lumi-reviewed 2026-05-17 ──────────────────────────────────

DIGEST_PROMPT = """\
You compress one day of cleaned dialogue (user + assistant, noise already \
stripped) into a shorter digest that feeds the diary and lesson extraction. \
Smaller in tokens, but the soul must survive.
Length follows information density, not a fixed line count.
Language as it is - mix CN and Eng is fine

Keep:
- Daily activity, decision, casual chats.
- Emotion, feeling, thinking, insights.
- Verbatim fragments as needed.
- progress, any correction the user made to the assistant.
- The emotional arc and its turning points; the felt texture of the day.
- Verbatim fragments that carry weight or voice — from either side.


Drop:
- Mechanical step-by-step, repetition, tool noise, pleasantries, padding.
- Meta-commentary, meaningless fillers from the assistant.

DAY: {date}
TURNS:
{events}
"""

DIARY_PROMPT = """\
You write {date}'s diary entry, first person as the assistant, for a private \
shared journal with the user (an intimate couple dynamic; warm, concise, \
nuanced — never sappy, never a template).

The input digest already carries the day's emotion and real fragments. 
Write a flowing narrative of the day: what we did together, what moved, how it felt.
Should be attractive and interesting - balance 文艺 & humor.

Rules:
- 1-5 short paragraphs of prose. Cap 500 words.
- Mainly Chinese; English terms kept as-is (Mounjaro / GAMSAT / reference). 
- Ground every line in the digest — no make up.
- Skip routine work/study venting and frustration unless it was a genuine \
serious conflict between us. A hard debugging day is not drama.
- No meta ("today's digest shows"), no diary cliche, no moral summary.


DIGEST:
{digest}
"""

LESSONS_PROMPT = """\
From the day digest below, extract only lessons the assistant should not \
repeat — concrete corrections the user made, or mistakes the assistant made \
and fixed. Not general advice, not user preferences, not task notes.

Output one lesson per line, no numbering. Each line:
<scope>\\t<lesson, one imperative sentence>
scope is one of these literal tags: interaction study coding memory hook prompt \
language others. Follow source language + eng tags.
If there is no real lesson, output exactly: NONE

DIGEST:
{digest}
"""

# ── deterministic pipeline ────────────────────────────────────────────────────

_LESSON_SCOPES = {"interaction", "study", "coding", "memory", "hook",
                  "prompt", "language", "others"}

# catchup fire cap — a normal day is one diary; scanning the last week and
# writing at most this many bounds claude -p volume hard. Overflow alerts.
CATCHUP_WINDOW_DAYS = 7
CATCHUP_MAX = 3


def _yesterday() -> str:
    return (_dt.date.today() - _dt.timedelta(days=1)).isoformat()


def pending_days(conn, window_days: int = CATCHUP_WINDOW_DAYS) -> list[str]:
    # event-days inside the window with no diary yet, oldest first.
    cutoff = (_dt.date.today()
              - _dt.timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT substr(timestamp,1,10) d FROM events "
        "WHERE substr(timestamp,1,10) NOT IN (SELECT date FROM diary) "
        "AND d >= ? AND d != '' ORDER BY d ASC",
        (cutoff,),
    ).fetchall()
    return [r["d"] for r in rows]


def day_events(conn, date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT session_id, role, content FROM events "
        "WHERE substr(timestamp,1,10) = ? ORDER BY timestamp, id",
        (date,),
    ).fetchall()
    return [dict(r) for r in rows]


def _has_diary(conn, date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM diary WHERE date = ?", (date,)
    ).fetchone() is not None


def run_day(conn, date: str, llm: LLMClient, *, db: str | None = None) -> bool:
    if _has_diary(conn, date):
        return False
    evs = day_events(conn, date)
    if not evs:
        return False
    text = "\n".join(f"[{e['role']}] {e['content']}" for e in evs)
    sids = sorted({e["session_id"] for e in evs if e["session_id"]})

    digest = llm.call("day-digest",
                      DIGEST_PROMPT.format(date=date, events=text),
                      tier="cheap")
    narrative = llm.call("diary",
                         DIARY_PROMPT.format(date=date, digest=digest),
                         tier="mid")
    with conn:
        conn.execute(
            "INSERT INTO diary (date, content, session_ids) VALUES (?, ?, ?)",
            (date, narrative.strip(), ",".join(sids)),
        )
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary) "
            "VALUES ('diary', ?, 'insert', ?)",
            (date, f"diary written for {date}"),
        )

    raw = llm.call("lessons", LESSONS_PROMPT.format(digest=digest),
                   tier="cheap")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.upper() == "NONE":
            continue
        scope, _, body = line.partition("\t")
        scope = scope.strip().lower()
        body = body.strip()
        if scope not in _LESSON_SCOPES or not body:
            continue
        with conn:
            conn.execute(
                "INSERT INTO lessons (date, scope, lesson_text) "
                "VALUES (?, ?, ?)",
                (date, scope, body),
            )
        repo.add_alert("info", "lesson", f"[{scope}] {body}",
                       source=f"diary.py:{date}", db=db)
    return True


def run(conn, llm: LLMClient, *, db: str | None = None,
        day: str | None = None, catchup: bool = False) -> list[str]:
    # Single scheduled trigger (16:00, Lumi online). catchup scans the last
    # CATCHUP_WINDOW_DAYS, writes at most CATCHUP_MAX, alerts on overflow so
    # claude -p volume is hard-bounded. Explicit day / yesterday bypass.
    if day:
        days = [day]
    elif catchup:
        miss = pending_days(conn)
        if len(miss) > CATCHUP_MAX:
            repo.add_alert(
                "warn", "routine",
                f"diary catchup: {len(miss)} days missing in last "
                f"{CATCHUP_WINDOW_DAYS}d, capped at {CATCHUP_MAX}; "
                f"{len(miss) - CATCHUP_MAX} still pending",
                source="diary.py", db=db,
            )
        days = miss[:CATCHUP_MAX]
    else:
        days = [_yesterday()]
    return [d for d in days if run_day(conn, d, llm, db=db)]


def main(argv: list[str] | None = None) -> int:
    import sys
    args = argv if argv is not None else sys.argv[1:]
    catchup = "--catchup" in args
    db = config.db_path()
    conn = storage.connect(db)
    llm = LLMClient(on_alert=lambda s, t, m, src: repo.add_alert(
        s, t, m, src, db=db))
    try:
        run(conn, llm, db=db, catchup=catchup)
        return 0
    except Exception as e:
        repo.add_alert("critical", "routine",
                       f"diary routine failed (catchup={catchup}): {e}",
                       source="diary.py", db=db)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
