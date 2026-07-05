"""Timeline trim (C4-rest, Decided 07-04): merge same-scene adjacent tl rows.

Merge rule: same 【label】 + adjacent in time (gap <= merge_gap_min) folds
into one row spanning a natural range (span capped at merge_span_max_min);
distinct scene/affect labels never merge. Rows are fact — bodies are
concatenated verbatim, never rewritten, no filler added. Only rows older
than min_age_hours are touched; flagged rows (unresolved / retired) are
skipped. Ordinary DB-side writer on merged state, runs inside the
dashboard reconcile-before-render pass; her edits flow back via the
existing bidirectional reconcile. Every merge is journaled to
logs/trim.jsonl (original rows kept verbatim) so any merge is reversible
by hand.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3

from . import config

_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
_LABEL_RE = re.compile(r"^(【[^】]*】)\s*(.*)$", re.S)


def _trim_cfg() -> dict:
    cfg = config.load().get("trim", {}) or {}
    return {
        "enabled": cfg.get("enabled", True),
        "min_age_hours": cfg.get("min_age_hours", 48),
        "merge_gap_min": cfg.get("merge_gap_min", 45),
        "merge_span_max_min": cfg.get("merge_span_max_min", 120),
    }


def _parse(ts: str | None) -> _dt.datetime | None:
    if not ts:
        return None
    try:
        return _dt.datetime.strptime(ts, _UTC_FMT).replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def _row_start(row: dict) -> _dt.datetime | None:
    return _parse(row["ts_start"]) or _parse(row["timestamp"])


def _row_end(row: dict) -> _dt.datetime | None:
    return _parse(row["ts_end"]) or _row_start(row)


def _split_label(content: str) -> tuple[str | None, str]:
    m = _LABEL_RE.match((content or "").strip())
    if not m:
        return None, (content or "").strip()
    return m.group(1), m.group(2).strip()


def _log_path():
    logs = config.ensure_data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "trim.jsonl"


def _merge_group(conn: sqlite3.Connection, group: list[dict],
                 dry_run: bool) -> dict:
    label, _ = _split_label(group[0]["content"])
    bodies: list[str] = []
    for row in group:
        _, body = _split_label(row["content"])
        if body and body not in bodies:
            bodies.append(body)
    merged_body = "；".join(bodies)
    content = f"{label}{merged_body}" if label else merged_body

    start = _row_start(group[0])
    end = _row_end(group[-1])
    ts_start = start.strftime(_UTC_FMT) if start else group[0]["ts_start"]
    ts_end = end.strftime(_UTC_FMT) if end and end != start else None
    imps = [r["imp"] for r in group if r["imp"] is not None]
    imp = max(imps) if imps else None

    keep = group[0]
    deleted_ids = [r["id"] for r in group[1:]]
    if not dry_run:
        conn.execute(
            "UPDATE events SET content=?, ts_start=?, ts_end=?, imp=? WHERE id=?",
            (content, ts_start, ts_end, imp, keep["id"]),
        )
        conn.executemany(
            "DELETE FROM events WHERE id=?", [(i,) for i in deleted_ids]
        )
    return {
        "kept": keep["id"],
        "deleted": deleted_ids,
        "after": content,
        "ts_start": ts_start,
        "ts_end": ts_end,
        "before": [dict(r) for r in group],
    }


def trim_timeline(conn: sqlite3.Connection, *, now: _dt.datetime | None = None,
                  dry_run: bool = False) -> dict:
    """Merge same-scene adjacent tl rows older than the trim window.
    Returns {"groups": [...], "merged": n, "deleted": n}. Journals every
    merge (original rows verbatim) to logs/trim.jsonl before writing."""
    tcfg = _trim_cfg()
    if not tcfg["enabled"]:
        return {"groups": [], "merged": 0, "deleted": 0}

    now = now or _dt.datetime.now(_dt.timezone.utc)
    cutoff = (now - _dt.timedelta(hours=tcfg["min_age_hours"])).strftime(_UTC_FMT)
    gap_max = _dt.timedelta(minutes=tcfg["merge_gap_min"])
    span_max = _dt.timedelta(minutes=tcfg["merge_span_max_min"])

    rows = [dict(r) for r in conn.execute(
        "SELECT id, timestamp, content, ts_start, ts_end, imp FROM events"
        " WHERE role='tl' AND (flag IS NULL OR flag='') AND timestamp < ?"
        " ORDER BY COALESCE(ts_start, timestamp), id",
        (cutoff,),
    ).fetchall()]

    groups: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        label, _ = _split_label(row["content"])
        start = _row_start(row)
        if current and label is not None and start is not None:
            cur_label, _ = _split_label(current[0]["content"])
            cur_start = _row_start(current[0])
            cur_end = _row_end(current[-1])
            if (label == cur_label and cur_end is not None
                    and start - cur_end <= gap_max
                    and (_row_end(row) or start) - cur_start <= span_max):
                current.append(row)
                continue
        if len(current) >= 2:
            groups.append(current)
        current = [row] if label is not None else []
    if len(current) >= 2:
        groups.append(current)

    report = {"groups": [], "merged": 0, "deleted": 0, "dry_run": dry_run}
    if not groups:
        return report

    log_entries = []
    for group in groups:
        entry = _merge_group(conn, group, dry_run)
        report["groups"].append(entry)
        report["merged"] += 1
        report["deleted"] += len(entry["deleted"])
        log_entries.append({"at": now.strftime(_UTC_FMT), "dry_run": dry_run, **entry})
    if not dry_run:
        conn.commit()

    with _log_path().open("a", encoding="utf-8") as f:
        for entry in log_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return report
