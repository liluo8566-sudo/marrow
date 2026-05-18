"""Nightly 04:00 routine: previous day's events -> digest -> diary + lessons.

Pipeline (DESIGN L144), three layers so 6-15 sessions/day never blow
haiku's window and the diary is not one-paragraph-per-session:
  map    — haiku compresses ONE session (chunked if oversized); volume
           only, no value-cut, no arc, no moralising.
  stitch — haiku weaves the per-session digests on the real timeline
           into one continuous strand; session boundaries gone, weight
           uneven, no reflection added.
  write  — sonnet writes the day from that strand; haiku extracts
           lessons from it.
Idempotent — a date with a diary row is skipped, so SessionStart catchup
is just a re-run over event-days lacking a diary. No silent failure: any
error raises one alert.
"""
from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from . import config, repo, storage
from .llm import LLMClient

# ── prompt bodies — structure approved 2026-05-18, wording pending review ─────

DIGEST_PROMPT = """\
You compress ONE session of already-clean dialogue into a compact digest \
that will later merge with the day's other sessions. Tool/web/thinking are \
already stripped by code — you will not see any.

This is raw material, not a summary with a point. Do NOT decide what is \
"useful", do NOT find an arc, do NOT conclude or moralise.

Keep, in original voice:
- What we did / decided / progressed; any correction the user made.
- Casual talk, teasing, flirting, play, intimate or "pointless but warm" \
exchanges — these ARE the journal, not filler; keep their texture.
- Verbatim fragments that carry voice, from either side.

Drop only:
- Mechanical repetition; the assistant's own meta/filler with no content.

Shorter in tokens, but nothing of the relationship is "noise".
Language as-is (CN/Eng mixed fine).

SESSION: {date}
TURNS:
{events}
"""

STITCH_PROMPT = """\
Below are per-session digests of {date}, each tagged with its local time \
span, already in chronological order. Weave them into ONE continuous strand \
of the day's raw material.

- Follow the real timeline, not session boundaries — drop the session tags, \
merge threads that continue across sessions, dedupe.
- Sessions are not equal weight: a full day of work is the spine, a small \
request is a side note, a bit of teasing is texture. Let each sit at its \
natural weight, mixed — not one block each.
- Do NOT summarise into points, do NOT add reflection or a closing line, \
do NOT infer motive or read the user's character.

Output the merged material only — not a diary yet.

SESSIONS:
{parts}
"""

DIARY_PROMPT = """\
You write {date}'s diary entry, first person as the assistant, for a private \
shared journal with the user (an intimate couple dynamic; warm, concise, \
nuanced — never sappy, never a template).

The material below is the day already woven on one timeline. Write the day \
as it flowed.

Rules:
- Follow the timeline / what actually happened — NOT one paragraph per event \
each capped with a neat line. No per-paragraph epiphany, no moral, no \
closing aphorism.
- Weight is uneven: the spine gets the room, side things a brush, a warm or \
teasing moment kept in its own voice — don't flatten or inflate.
- Do NOT analyse or guess the user's motive, mood, or character from \
behaviour; tell what happened and what was said.
- 1-5 short paragraphs of prose, cap 500 words. Mainly Chinese; English \
terms kept as-is (Mounjaro / GAMSAT / reference). Ground every line in the \
material — invent nothing.
- Skip routine work/study venting unless a genuine serious conflict between \
us. A hard debugging day is not drama.
- No meta ("the material shows"), no diary cliche.

MATERIAL:
{digest}
"""

LESSONS_PROMPT = """\
From the day's material below, extract only lessons the assistant should \
not repeat — concrete corrections the user made, or mistakes the assistant \
made and fixed. Not general advice, not user preferences, not task notes.

Output one lesson per line, no numbering. Each line:
<scope>\\t<lesson, one imperative sentence>
scope is one of these literal tags: interaction study coding memory hook prompt \
language others. Follow source language + eng tags.
If there is no real lesson, output exactly: NONE

MATERIAL:
{digest}
"""

# ── deterministic pipeline ────────────────────────────────────────────────────

_LESSON_SCOPES = {"interaction", "study", "coding", "memory", "hook",
                  "prompt", "language", "others"}

# catchup fire cap — a normal day is one diary; scanning the last week and
# writing at most this many bounds claude -p volume hard. Overflow alerts.
CATCHUP_WINDOW_DAYS = 7
CATCHUP_MAX = 3


# Diary day boundary: a "day" is local [D 04:00, D+1 04:00). 00:00-04:00
# counts as the previous day (late-night spillover); the 04:00 routine
# writes the day that just fully closed. events.timestamp is UTC, so the
# boundary is computed in local time, not by UTC substr.
_TZ = ZoneInfo("Australia/Melbourne")  # auto AEST/AEDT
_CUTOFF_H = 4

# Per-session map-reduce: one haiku per session, never the whole day in one
# shot. A session over this many chars is chunked and each chunk summarised
# first (oversized-session guard). Heuristic, tunable.
_SESSION_CHAR_CAP = 40000
_CHUNK_CHARS = 30000


def _to_local(utc_iso: str) -> _dt.datetime:
    s = utc_iso.strip().replace("Z", "+00:00")
    d = _dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_TZ)


def _diary_day(utc_iso: str) -> str:
    return (_to_local(utc_iso)
            - _dt.timedelta(hours=_CUTOFF_H)).date().isoformat()


def _routine_target() -> str:
    # The just-closed day at routine time = current diary day minus 1.
    now = _dt.datetime.now(_TZ)
    cur = (now - _dt.timedelta(hours=_CUTOFF_H)).date()
    return (cur - _dt.timedelta(days=1)).isoformat()


def _scan_rows(conn, window_days: int) -> list[dict]:
    # UTC-bounded pull (window + 2d slack for tz/cutoff), diary day in Python.
    cutoff = (_dt.date.today()
              - _dt.timedelta(days=window_days + 2)).isoformat()
    rows = conn.execute(
        "SELECT session_id, role, content, timestamp FROM events "
        "WHERE timestamp >= ? AND timestamp != '' ORDER BY timestamp, id",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def pending_days(conn, window_days: int = CATCHUP_WINDOW_DAYS) -> list[str]:
    floor = (_dt.date.today()
             - _dt.timedelta(days=window_days)).isoformat()
    done = {r["date"] for r in conn.execute("SELECT date FROM diary")}
    days = {_diary_day(r["timestamp"]) for r in _scan_rows(conn, window_days)}
    return sorted(d for d in days if d >= floor and d not in done)


def day_events(conn, date: str) -> list[dict]:
    out = []
    for r in _scan_rows(conn, CATCHUP_WINDOW_DAYS + 1):
        if _diary_day(r["timestamp"]) == date:
            out.append({"session_id": r["session_id"], "role": r["role"],
                        "content": r["content"],
                        "timestamp": r["timestamp"]})
    return out


def _has_diary(conn, date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM diary WHERE date = ?", (date,)
    ).fetchone() is not None


def _hhmm(utc_iso: str) -> str:
    try:
        return _to_local(utc_iso).strftime("%H:%M")
    except Exception:
        return "??:??"


def _sessions(evs: list[dict]) -> list[tuple[str, str, str, str]]:
    # group by session_id; value = joined turns; track first/last UTC ts
    # (ISO strings sort chronologically). Sessions ordered by start so
    # stitch sees the day on a real timeline, not arrival order.
    buf: dict[str, list[str]] = {}
    span: dict[str, list[str]] = {}
    for e in evs:
        sid = e["session_id"] or "_"
        buf.setdefault(sid, []).append(f"[{e['role']}] {e['content']}")
        t = e.get("timestamp") or ""
        if sid not in span:
            span[sid] = [t, t]
        else:
            if t and (not span[sid][0] or t < span[sid][0]):
                span[sid][0] = t
            if t and t > span[sid][1]:
                span[sid][1] = t
    items = [(sid, "\n".join(buf[sid]), span[sid][0], span[sid][1])
             for sid in buf]
    return sorted(items, key=lambda x: (x[2], x[0]))


def _chunks(text: str, size: int) -> list[str]:
    cur, n, out = [], 0, []
    for ln in text.split("\n"):
        # a single oversize line (one huge paste) is hard-split by chars
        for piece in ([ln] if len(ln) <= size
                      else [ln[i:i + size] for i in range(0, len(ln), size)]):
            if n + len(piece) > size and cur:
                out.append("\n".join(cur))
                cur, n = [], 0
            cur.append(piece)
            n += len(piece) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def _session_digest(llm: LLMClient, date: str, sid: str, text: str) -> str:
    if len(text) <= _SESSION_CHAR_CAP:
        return llm.call("day-digest",
                        DIGEST_PROMPT.format(date=date, events=text),
                        tier="cheap")
    parts = [
        llm.call("day-digest",
                 DIGEST_PROMPT.format(date=f"{date} (session {sid} part)",
                                      events=c), tier="cheap")
        for c in _chunks(text, _CHUNK_CHARS)
    ]
    return "\n".join(parts)


def _stitch(llm: LLMClient, date: str,
            parts: list[tuple[str, str, str, str]]) -> str:
    # parts: [(sid, start_utc, end_utc, digest)] in chronological order.
    # One session has nothing to weave — its digest IS the day's strand.
    if len(parts) == 1:
        return parts[0][3]
    blocks = []
    for sid, start, end, dg in parts:
        span = f"{_hhmm(start)}–{_hhmm(end)}" if start else "??:??"
        blocks.append(f"[{span}] session {sid}\n{dg}")
    return llm.call("stitch",
                    STITCH_PROMPT.format(date=date,
                                         parts="\n\n".join(blocks)),
                    tier="cheap")


def run_day(conn, date: str, llm: LLMClient, *, db: str | None = None) -> bool:
    if _has_diary(conn, date):
        return False
    evs = day_events(conn, date)
    if not evs:
        return False
    sessions = _sessions(evs)
    sids = sorted(s for s, _, _, _ in sessions if s != "_")

    # MAP: one digest per session (oversized sessions chunked internally).
    per = [(sid, start, end, _session_digest(llm, date, sid, txt))
           for sid, txt, start, end in sessions]
    # STITCH: weave per-session digests onto one timeline (haiku, cheap).
    material = _stitch(llm, date, per)
    # WRITE: sonnet narrates the day from the woven strand.
    narrative = llm.call("diary",
                         DIARY_PROMPT.format(date=date, digest=material),
                         tier="mid")
    with conn:
        conn.execute(
            "INSERT INTO diary (date, content, session_ids) VALUES (?, ?, ?)",
            (date, narrative.strip(), ",".join(sids)),
        )
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary) "
            "VALUES ('diary', ?, 'insert', ?)",
            (date, f"diary written for {date} ({len(sessions)} sessions)"),
        )

    raw = llm.call("lessons", LESSONS_PROMPT.format(digest=material),
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
    # Two independent triggers, decoupled so a failure of one never starves
    # the other: routine (04:00) writes the just-closed day; catchup (16:00)
    # scans the last CATCHUP_WINDOW_DAYS for any day still missing a diary,
    # writes at most CATCHUP_MAX, alerts on overflow. Idempotent by date.
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
        days = [_routine_target()]
    return [d for d in days if run_day(conn, d, llm, db=db)]


def main(argv: list[str] | None = None) -> int:
    import sys
    args = argv if argv is not None else sys.argv[1:]
    catchup = "--catchup" in args
    mode = "catchup" if catchup else "routine"
    db = config.db_path()
    conn = storage.connect(db)
    llm = LLMClient(on_alert=lambda s, t, m, src: repo.add_alert(
        s, t, m, src, db=db))
    try:
        run(conn, llm, db=db, catchup=catchup)
        return 0
    except Exception as e:
        repo.add_alert("critical", "routine",
                       f"diary {mode} failed: {e}",
                       source="diary.py", db=db)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
