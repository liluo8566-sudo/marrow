"""Daily routine + catchup. Candidate extraction + diary write.

CLI:
- `python -m marrow.daily`           → 07:00 routine; yesterday only
- `python -m marrow.daily --catchup` → 19:00 catchup scan over CATCHUP_WINDOW_DAYS
- `python -m marrow.daily --day YYYY-MM-DD` → explicit single day
- `--force` → re-write existing diary row

Per day, two sonnet calls:
  1. DAILY_CAND_PROMPT on aggregated session_digests → 3 marker blocks
     (ENTITY_CAND / MILESTONE_CAND / MEMES_CAND). Idempotent — gated on
     has_diary like the diary write itself.
  2. Diary prompt (daily_prompts.py) on the same aggregate + affect_live → diary prose.

Reads `affect_live` + DIGEST text (session_digests table) for the target
date. Writes diary row + candidate inserts in one daily run.
"""
from __future__ import annotations

import datetime as _dt
import sys

from pathlib import Path

from . import candidates, config, daily_catchup, repo, storage, subpages
from .daily_prompts import render_daily_cand_prompt, render_diary_prompt
from .llm import LLMClient, LLMError


def _read_digests(conn, date: str) -> list[tuple[str, str, str]]:
    """List of (sid, text, life_lines) digests for the target date."""
    rows = conn.execute(
        "SELECT sid, text, life_lines FROM session_digests WHERE date=? ORDER BY ts, sid",
        (date,),
    ).fetchall()
    return [(r["sid"], r["text"], r["life_lines"] or "") for r in rows if r["text"]]


def _read_affect_summary(conn, date: str) -> list[dict]:
    """Structured affect episodes for the date — feeds sonnet so it can
    honour the importance=5 → force-milestone rule. Empty rows skipped.
    Includes unresolved flag and eph/epl side marker.
    """
    rows = conn.execute(
        "SELECT ep, importance, label, description, valence, arousal,"
        " unresolved"
        " FROM affect_live WHERE date=? ORDER BY ep", (date,),
    ).fetchall()
    out = []
    for r in rows:
        if not (r["label"] or r["description"]):
            continue
        v = r["valence"]
        side = "eph" if v >= 0.5 else "epl"
        out.append({
            "ep": r["ep"],
            "importance": r["importance"],
            "label": r["label"] or "",
            "description": r["description"] or "",
            "valence": v,
            "arousal": r["arousal"],
            "side": side,
            "unresolved": bool(r["unresolved"]),
        })
    return out


def _format_affect_block(date: str, episodes: list[dict]) -> str:
    if not episodes:
        return ""
    lines = [f"AFFECT episodes for {date}:"]
    for e in episodes:
        label = f"[{e['label']}]" if e["label"] else ""
        desc = e["description"]
        side = e.get("side", "eph")
        imp = e["importance"]
        head = f"- {side}{imp}"
        body = f" {label} {desc}".rstrip()
        tail = f" (v={e['valence']:.2f}, a={e['arousal']:.2f})"
        open_mark = " [open]" if e.get("unresolved") else ""
        lines.append(f"{head}{body}{tail}{open_mark}")
    return "\n".join(lines)


def _assemble_material(digests: list[tuple[str, str, str]],
                       affect_episodes: list[dict],
                       date: str) -> str:
    parts = []
    for sid, text, life_lines in digests:
        section = f"[session {sid}]\n{text}"
        if life_lines:
            section += f"\n\nLIFE_LINES:\n{life_lines}"
        parts.append(section)
    body = "\n\n---\n\n".join(parts) if parts else "(no digests)"
    block = _format_affect_block(date, affect_episodes)
    if block:
        body += "\n\n" + block
    return body


def _parse_tone_overview(narrative: str) -> tuple[str, str | None, str | None]:
    """Extract TONE and OVERVIEW from diary call output.

    Returns (diary_text, tone, overview).
    Scans from the bottom to find marker lines; everything above is diary text.
    """
    import re as _re
    tone_pattern = _re.compile(r"TONE[：:]\s*(.+)", _re.IGNORECASE)
    overview_pattern = _re.compile(r"OVERVIEW[：:]\s*(.+)", _re.IGNORECASE)

    lines = narrative.splitlines()
    tone: str | None = None
    overview: str | None = None
    diary_lines: list[str] = []

    for ln in reversed(lines):
        t_match = tone_pattern.search(ln)
        o_match = overview_pattern.search(ln)
        if not tone and t_match:
            tone = t_match.group(1).strip() or None
        elif not overview and o_match:
            overview = o_match.group(1).strip() or None
        else:
            diary_lines.append(ln)

    diary_text = "\n".join(reversed(diary_lines)).strip()
    return diary_text, tone, overview


def _extract_candidates(conn, llm: LLMClient, date: str,
                        digest_aggregate: str, *,
                        db: str | None = None) -> dict[str, int]:
    """One sonnet call on aggregated digests → 3 marker block writers.

    Each block writer is independent; one block failing parse does not
    block the others. Returns {segment: rows_written}. Logs a non-
    blocking alert on LLM-level failure.
    """
    counts = {"entity_cand": 0, "milestone_cand": 0, "memes_cand": 0}
    try:
        raw = llm.call(
            "daily_cand",
            render_daily_cand_prompt().format(date=date, digest=digest_aggregate),
            tier="mid",
        )
    except LLMError as e:
        repo.add_alert("warn", "routine",
                       f"daily_cand_failed:{date}",
                       source="daily.py", db=db,
                       message=f"daily {date} candidate extraction failed: {e}")
        return counts
    for name, writer in (
        ("entity_cand", lambda r: candidates.write_entity_cand(conn, r)),
        ("milestone_cand",
         lambda r: candidates.write_milestone_cand(conn, r, date)),
        ("memes_cand",
         lambda r: candidates.write_memes_cand(conn, r, date=date)),
    ):
        try:
            counts[name] = writer(raw)
        except (ValueError, RuntimeError, TypeError, KeyError):
            counts[name] = 0
    return counts


def run_day(conn, date: str, llm: LLMClient, *, db: str | None = None,
            force: bool = False) -> bool:
    existed = daily_catchup.has_diary(conn, date)
    if existed and not force:
        return False
    _act = "update" if existed else "insert"

    digests = _read_digests(conn, date)
    affect_episodes = _read_affect_summary(conn, date)

    if not digests and not affect_episodes:
        with conn:
            conn.execute("DELETE FROM diary WHERE date = ?", (date,))
            conn.execute(
                "INSERT INTO diary (date, content, session_ids) "
                "VALUES (?, ?, ?)", (date, "—", ""))
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id,"
                " action, summary) VALUES ('diary', ?, ?, ?)",
                (date, _act, f"daily stub for {date} (no digests, no affect)"))
        return True

    material = _assemble_material(digests, affect_episodes, date)
    sids = ",".join(sorted(sid for sid, *_ in digests if sid))

    # Candidate extraction (1 sonnet call) — best-effort, never blocks diary.
    if digests:
        cand_counts = _extract_candidates(conn, llm, date, material, db=db)
        with conn:
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id,"
                " action, summary) VALUES ('daily', ?, 'cand_extract', ?)",
                (date,
                 f"entity={cand_counts['entity_cand']} "
                 f"milestone={cand_counts['milestone_cand']} "
                 f"memes={cand_counts['memes_cand']}"),
            )

    try:
        narrative = llm.call("daily",
                             render_diary_prompt().format(
                                 date=date, digest=material),
                             tier="mid")
    except LLMError as e:
        repo.add_alert("critical", "routine",
                       f"daily_sonnet_failed:{date}",
                       source="daily.py", db=db,
                       message=f"daily {date} sonnet call failed: {e}")
        return False
    narrative = (narrative or "").strip() or "—"
    diary_text, tone, overview = _parse_tone_overview(narrative)
    diary_text = diary_text or narrative  # fallback: keep full text if parse yields empty

    with conn:
        conn.execute("DELETE FROM diary WHERE date = ?", (date,))
        conn.execute(
            "INSERT INTO diary (date, content, tone, overview, session_ids, updated_at) "
            "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (date, diary_text, tone, overview, sids),
        )
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('diary', ?, ?, ?)",
            (date, _act, f"daily written for {date} "
                         f"(digests={len(digests)}, affect={len(affect_episodes)}"
                         f", tone={'ok' if tone else 'missing'})"),
        )
    return True


def run(conn, llm: LLMClient, *, db: str | None = None,
        day: str | None = None, catchup: bool = False,
        force: bool = False) -> list[str]:
    if day:
        days = [day]
    elif catchup:
        miss = daily_catchup.pending_days(conn)
        if len(miss) > daily_catchup.CATCHUP_MAX:
            repo.add_alert(
                "warn", "routine",
                "daily_catchup_overflow",
                source="daily.py", db=db,
                message=(f"daily catchup: {len(miss)} days missing in last "
                         f"{daily_catchup.CATCHUP_WINDOW_DAYS}d, capped at "
                         f"{daily_catchup.CATCHUP_MAX}; "
                         f"{len(miss) - daily_catchup.CATCHUP_MAX} still pending"),
            )
        days = miss[:daily_catchup.CATCHUP_MAX]
    else:
        days = [daily_catchup.routine_target()]
    wrote = [d for d in days if run_day(conn, d, llm, db=db, force=force)]
    if catchup:
        remaining = daily_catchup.pending_days(conn)
        if len(remaining) <= daily_catchup.CATCHUP_MAX:
            now_utc = _dt.datetime.now(_dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            with conn:
                conn.execute(
                    "UPDATE alerts SET resolved=1, updated_at=? "
                    "WHERE type IN ('daily_catchup', 'routine') "
                    "AND fingerprint='daily_catchup_overflow' "
                    "AND resolved=0",
                    (now_utc,),
                )
    return wrote


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    catchup = "--catchup" in args
    force = "--force" in args
    day = None
    if "--day" in args:
        i = args.index("--day")
        if i + 1 < len(args):
            day = args[i + 1]
    mode = "catchup" if catchup else "routine"
    db = config.db_path()
    conn = storage.connect(db)
    llm = LLMClient(on_alert=lambda s, t, m, src: repo.add_alert(
        s, t, m, src, db=db))
    ts = _dt.datetime.now(daily_catchup._TZ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with daily_catchup.app_lock():
            wrote = run(conn, llm, db=db, day=day, catchup=catchup,
                        force=force)
        if wrote:
            try:
                sub_folder = config.sub_pages_path()
                sub_state = config.sub_pages_state_path()
                Path(sub_folder).mkdir(parents=True, exist_ok=True)
                Path(sub_state).mkdir(parents=True, exist_ok=True)
                subpages.write_all_subpages(
                    conn, folder=sub_folder, state_dir=sub_state, db=db,
                )
            except Exception as e:
                repo.add_alert(
                    "warn", "sub_pages",
                    f"daily_subpages_failed:{mode}",
                    source="daily.py", db=db,
                    message=f"daily {mode} skipped sub-pages write: {e}",
                )
            # Post-condition: each day daily.run claimed to write must still
            # be in the diary table after subpages render. Catches the
            # "reconcile sweeps a freshly-inserted row in the same pass"
            # silent-delete class of bug (2026-06-04 incident). Emits a
            # critical alert so the missing day is visible at next dashboard
            # refresh; idempotent via repo.add_alert dedup.
            silent = [d for d in wrote
                      if not daily_catchup.has_diary(conn, d)]
            if silent:
                repo.add_alert(
                    "critical", "routine",
                    f"daily_silent_delete:{mode}",
                    source="daily.py", db=db,
                    message=(f"daily {mode} silent-delete: wrote {silent} but row "
                             f"absent post-refresh — rerun `mw daily --day <date> "
                             f"--force` after diagnosis"),
                )
        print(f"[{ts}] daily {mode} ok: wrote={wrote or '[]'}", flush=True)
        return 0
    except Exception as e:
        print(f"[{ts}] daily {mode} FAILED: {e}", flush=True)
        repo.add_alert("critical", "routine",
                       f"daily_failed:{mode}",
                       source="daily.py", db=db,
                       message=f"daily {mode} failed: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
