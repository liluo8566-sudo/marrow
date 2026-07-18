"""md->DB reconcile for sub-pages + monitor/daybrief surfaces.

Scope today:
- milestone subpage (reconcile_milestones)
- memes subpage (reconcile_memes) — anchor-scan delete
- profile subpage (reconcile_profile) — anchor-scan soft-delete via superseded_by
- monitor alerts block (reconcile_alerts) — md-delete = resolve
- daybrief timeline block (reconcile_timeline) — life_lines per-line anchor.

Vocab/pit plug in later via the same shape (parse(md)->rows, diff
against DB, apply). reconcile_* are the public entries; monitor.update
and daybrief.update run their pass before render.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from ._atomic import atomic_write as _atomic_write
from .timeutil import _MELB as _MELB_TZ


MILESTONE_KEY = "milestone"
_M0 = f"<!-- marrow:{MILESTONE_KEY}:start -->"
_M1 = f"<!-- marrow:{MILESTONE_KEY}:end -->"

# Mirrors render_milestone output (H5 + paragraph, 2026-05-24):
#   ##### [YYYY-MM-DD] subject       (Us / dated Me — full date in bracket)
#   ##### [YYYY] subject             (Me — year-only date in bracket, legacy)
#   ##### [<title>]                  (Me historical — title fills bracket,
#                                     date stays in DB; only valid when row
#                                     carries `<!-- id:N -->` anchor so we
#                                     can pull date from the existing row)
#   description paragraph. <!-- id:N -->
_H5_RE = re.compile(
    r"^##### \[(?P<date>\d{4}(?:-\d{2}-\d{2})?)\] (?P<title>.+?)\s*$"
)
_H5_AGE_RE = re.compile(r"^##### \[(?P<title>.+?)\]\s*$")
_ID_RE = re.compile(r"<!-- id:(?P<id>\d+) -->")


def _today_melb() -> str:
    """Return today's date as YYYY-MM-DD in configured local timezone."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).astimezone(_MELB_TZ).strftime("%Y-%m-%d")


@dataclass
class ReconcileReport:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    conflicts: list[str] = field(default_factory=list)

    def any_change(self) -> bool:
        return self.inserted or self.updated or self.deleted


def _parse(md_text: str) -> list[dict]:
    """Yield row dicts in section order.

    Block boundary = H5 heading `##### [date] subject`. Body lines below
    the heading (until the next H5 / `## ` section / end-of-marker) form
    the description paragraph; the `<!-- id:N -->` anchor may live
    anywhere in the body (inline-tail by render convention).
    """
    rows: list[dict] = []
    section: str | None = None
    in_block = False
    cur: dict | None = None
    body: list[str] = []

    def flush():
        nonlocal cur, body
        if cur is None:
            return
        text = "\n".join(body).strip()
        m_id = _ID_RE.search(text)
        if m_id:
            cur["id"] = int(m_id.group("id"))
            text = _ID_RE.sub("", text).strip()
        cur["description"] = text or None
        rows.append(cur)
        cur = None
        body = []

    for lineno, line in enumerate(md_text.splitlines()):
        s = line.rstrip()
        if _M0 in s:
            in_block = True
            continue
        if _M1 in s:
            flush()
            in_block = False
            continue
        if not in_block:
            continue
        if s.startswith("## ") and not s.startswith("##### "):
            flush()
            head = s[3:].strip().lower()
            if head == "us":
                section = "us"
            elif head == "me":
                section = "me"
            else:
                section = None
            continue
        # Strip any inline `<!-- id:N -->` anchor off the heading before
        # matching — bidirectional reconcile may have spliced the id onto
        # the heading line itself (BUG-1 fix). Keep the captured id for flush.
        heading_id: int | None = None
        s_for_h5 = s
        m_inline = _ID_RE.search(s)
        if m_inline:
            try:
                heading_id = int(m_inline.group("id"))
            except ValueError:
                heading_id = None
            s_for_h5 = _ID_RE.sub("", s).rstrip()
        m = _H5_RE.match(s_for_h5)
        if m and section is not None:
            flush()
            cur = {
                "scope": section,
                "date": m.group("date"),
                "title": m.group("title").strip(),
                "theme": None,
                "pinned": 1,
                "description": None,
                "id": heading_id,
                "_heading_line": lineno,
            }
            continue
        # Historical Me — single-bracket form `##### [<title>]` (no date in
        # bracket). Only honoured under `## Me`; date is unknown here and
        # must be recovered from DB via the row's id anchor.
        m_age = _H5_AGE_RE.match(s_for_h5)
        if m_age and section == "me":
            flush()
            cur = {
                "scope": section,
                "date": None,
                "title": m_age.group("title").strip(),
                "theme": None,
                "pinned": 1,
                "description": None,
                "id": heading_id,
                "_heading_line": lineno,
            }
            continue
        # Stale legacy `- [date] ...` bullet rows from a pre-H5 file are
        # NOT body — skip them so they don't pollute the H5 description.
        if s.startswith("- "):
            continue
        if cur is not None and s:
            body.append(s)
            continue
        # Bare text line (non-empty, not `- ` prefixed, not H5) while cur is
        # None inside a section → treat as new milestone typed by the user without
        # using H5 format. date = today (configured local timezone), pinned = 1.
        # _bare_line=True signals write-back to replace the whole line with
        # the canonical H5 form instead of just appending an anchor.
        # Skip lines that carry an existing `<!-- id:N -->` anchor — those are
        # orphaned description lines from a deleted H5 block, not new titles.
        if cur is None and section is not None and s and not _ID_RE.search(s):
            cur = {
                "scope": section,
                "date": _today_melb(),
                "title": s.strip(),
                "theme": None,
                "pinned": 1,
                "description": None,
                "id": None,
                "_heading_line": lineno,
                "_bare_line": True,
            }
            flush()
    flush()
    return rows


def _hash(row: dict) -> str:
    src = "\x1f".join([
        row["scope"], row["date"], row["title"], row.get("description") or "",
    ])
    return hashlib.sha256(src.encode()).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _md_mtime_iso(path) -> str | None:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime))
    except OSError:
        return None


def _audit(conn, mid: int | str, action: str, summary: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (target_table, target_id, action, summary) "
        "VALUES ('milestones', ?, ?, ?)",
        (str(mid), action, summary),
    )


def emit_conflict_alerts(rpt: ReconcileReport, source_name: str,
                          db: str | None = None) -> None:
    """Emit one warn alert per conflict in rpt.conflicts. Fail-soft — alert
    emission must never raise. Dedup via (type, fingerprint, resolved=0)
    so repeated reconcile rounds bump hit_count instead of flooding.
    """
    if not rpt.conflicts:
        return
    try:
        from . import repo as _repo
        for conflict in rpt.conflicts:
            try:
                _repo.add_alert(
                    "warn", "reconcile_conflict",
                    fingerprint=conflict,
                    source=source_name,
                    message=f"{source_name}: {conflict}",
                    db=db,
                )
            except Exception:
                pass
    except Exception:
        pass


def reconcile_milestones(conn: sqlite3.Connection,
                          md_path: Path) -> ReconcileReport:
    """Apply md edits back to milestones, then return a report.

    Contract:
    - Row with `<!-- id:N -->` -> match on id; update if title/desc/theme/
      pinned/date changed.
    - Row without anchor -> insert (new milestone written by the user).
    - DB row whose id is missing from md -> delete.
    - No-op when md == current state: no writes, no audit.
    - Failures surface via rpt.conflicts -> alert in caller. No md backup
      taken; render is the SoT and atomic write keeps the previous md
      intact until the new one lands.
    """
    rpt = ReconcileReport()
    md_path = Path(md_path)
    if not md_path.exists():
        return rpt
    md_text = md_path.read_text(encoding="utf-8")
    md_lines = md_text.splitlines()
    md_rows = _parse(md_text)
    # Reconcile operates on pinned=1 only — the confirmed subpage set.
    # pinned=0 candidates live outside the md ↔ db sync loop; daily.py
    # writes them, dashboard renders them, the user promotes via pinned=1.
    db_rows = {
        r["id"]: dict(r) for r in conn.execute(
            "SELECT id, scope, date, title, description, theme, pinned, updated_at "
            "FROM milestones WHERE pinned=1"
        ).fetchall()
    }

    seen: set[int] = set()
    # (heading_line_index, new_id, bare_line) triples to splice into md after
    # INSERT lands. bare_line=True means the source line was plain text (not
    # H5) and needs a full-line replacement to canonical H5 form.
    line_anchor_writes: list[tuple[int, int, bool]] = []

    md_mtime_iso = _md_mtime_iso(md_path)

    with conn:
        # inserts + updates
        for row in md_rows:
            rid = row["id"]
            # Single-bracket historical Me rows have no date in the md;
            # inherit the DB's existing date so diff doesn't flap.
            if rid is not None and rid in db_rows and row["date"] is None:
                row["date"] = db_rows[rid]["date"] or ""
            if rid is not None and rid in db_rows:
                cur = db_rows[rid]
                changed = (
                    (cur["scope"] or "") != row["scope"]
                    or (cur["date"] or "") != row["date"]
                    or (cur["title"] or "") != row["title"]
                    or (cur["description"] or None) != row["description"]
                    or (cur["theme"] or None) != row["theme"]
                )
                if changed:
                    row_updated = db_rows[rid].get("updated_at") or ""
                    if md_mtime_iso and row_updated > md_mtime_iso:
                        rpt.unchanged += 1
                        seen.add(rid)
                        continue
                    h = _hash(row)
                    conn.execute(
                        "UPDATE milestones SET "
                        "scope=?, date=?, title=?, description=?, theme=?, "
                        "pinned=1, source_hash=?, updated_at=? WHERE id=?",
                        (row["scope"], row["date"], row["title"],
                         row["description"], row["theme"],
                         h, _now(), rid),
                    )
                    _audit(conn, rid, "update",
                           f"md-reconcile: {row['title'][:60]}")
                    rpt.updated += 1
                else:
                    rpt.unchanged += 1
                seen.add(rid)
            elif rid is not None and rid not in db_rows:
                # Anchored id not in DB -> the user referenced a deleted row.
                # Treat as conflict; do not auto-create with a forced id.
                rpt.conflicts.append(
                    f"anchored id {rid} not in db: {row['title'][:40]}"
                )
            else:
                if row["date"] is None:
                    # Unanchored single-bracket Me row (##### [label]) carries
                    # no date and no anchor. Insert with today's configured-local-timezone date
                    # and write anchor back — same path as bare-text inserts.
                    row["date"] = _today_melb()
                # Safety net: exact-match dedup. Prevents runaway loop if the
                # md anchor-write fails for any reason (file lock, perm, race).
                existing = conn.execute(
                    "SELECT id FROM milestones "
                    "WHERE scope=? AND date=? AND title=? LIMIT 1",
                    (row["scope"], row["date"], row["title"]),
                ).fetchone()
                if existing is not None:
                    new_id = existing["id"]
                else:
                    h = _hash(row)
                    cur = conn.execute(
                        "INSERT INTO milestones "
                        "(scope, date, title, description, theme, pinned, "
                        " source_hash) VALUES (?, ?, ?, ?, ?, 1, ?)",
                        (row["scope"], row["date"], row["title"],
                         row["description"], row["theme"], h),
                    )
                    new_id = cur.lastrowid
                    _audit(conn, new_id, "insert",
                           f"md-reconcile: {row['title'][:60]}")
                    rpt.inserted += 1
                # Queue heading-line anchor write so the next inserter pass
                # sees the row as present in md (prevents dup canonical block).
                hl = row.get("_heading_line")
                if hl is not None:
                    line_anchor_writes.append(
                        (hl, new_id, bool(row.get("_bare_line")))
                    )
                seen.add(new_id)

        # deletes: db rows whose ids are not present in md
        for rid in list(db_rows.keys()):
            if rid in seen:
                continue
            # only delete if md had any anchored rows OR md is non-empty;
            # otherwise an empty/missing md would wipe the table. We already
            # returned early on missing file; require at least one parsed row.
            if not md_rows:
                continue
            # mtime gate: skip rows inserted/updated after md was last
            # written — they haven't been rendered yet, so absence from
            # md does not mean "user deleted".
            if md_mtime_iso:
                ts = conn.execute(
                    "SELECT COALESCE(updated_at, created_at) AS ts"
                    " FROM milestones WHERE id=?", (rid,)
                ).fetchone()
                row_ts = ts["ts"] if ts else None
                if not row_ts or row_ts > md_mtime_iso:
                    continue
            conn.execute("DELETE FROM milestones WHERE id=?", (rid,))
            _audit(conn, rid, "delete", "md-reconcile: removed from md")
            rpt.deleted += 1

    # Splice new ids back into the user's heading lines (idempotent: skip if
    # the line already carries any `<!-- id:N -->` anchor). Atomic write keeps
    # the watcher from racing on a half-written file.
    # bare_line=True: replace the whole line with canonical H5 form.
    # bare_line=False: append anchor to existing H5 heading line.
    if line_anchor_writes:
        # Build a lookup of (row.date, row.title) -> row for bare-line rewrites.
        _row_by_hl = {r["_heading_line"]: r for r in md_rows if r.get("_heading_line") is not None}
        changed = False
        for hl, new_id, bare in line_anchor_writes:
            if hl < 0 or hl >= len(md_lines):
                continue
            line = md_lines[hl]
            if _ID_RE.search(line):
                continue
            if bare:
                # Full-line replacement: rewrite bare text as canonical H5.
                row = _row_by_hl.get(hl)
                if row is not None:
                    md_lines[hl] = (
                        f"##### [{row['date']}] {row['title']}"
                        f" <!-- id:{new_id} -->"
                    )
                else:
                    md_lines[hl] = line.rstrip() + f" <!-- id:{new_id} -->"
            else:
                md_lines[hl] = line.rstrip() + f" <!-- id:{new_id} -->"
            changed = True
        if changed:
            trailing_nl = "\n" if md_text.endswith("\n") else ""
            _atomic_write(str(md_path), "\n".join(md_lines) + trailing_nl)

    return rpt


# ── inserter-subpage anchor-scan reconciles ───────────────────────────────────

_ANCHOR_RE = re.compile(r"<!-- id:(\d+) -->")


def _scan_anchored_ids(md_text: str) -> set[int]:
    """Collect every numeric `<!-- id:N -->` anchor in the file."""
    return {int(m.group(1)) for m in _ANCHOR_RE.finditer(md_text)}


def reconcile_memes(conn: sqlite3.Connection,
                    md_path: Path) -> ReconcileReport:
    """Delete/UPDATE memes rows from md edits. Delegates to reconcile_inserter."""
    from .reconcile_inserter import reconcile_memes as _reconcile_memes
    return _reconcile_memes(conn, md_path)


def reconcile_profile(conn: sqlite3.Connection,
                      md_path: Path) -> ReconcileReport:
    """Soft-delete/UPDATE entity rows from md edits. Delegates to reconcile_inserter."""
    from .reconcile_inserter import reconcile_profile as _reconcile_profile
    return _reconcile_profile(conn, md_path)


# ── alerts reconcile ─────────────────────────────────────────────────────────
_ALERT_H2 = "## Alerts"
_ALERT_ID_RE = re.compile(r"<!-- id:alert\.(?P<id>\d+) -->")


def reconcile_alerts(conn: sqlite3.Connection,
                     dashboard_path: str | Path) -> ReconcileReport:
    """Absorb md-side alert deletions back into the alerts table.

    Each rendered alert bullet carries `<!-- id:alert.N -->`. If the user
    removes a bullet from the dashboard md, that row is treated as
    `resolved=1` — the md-side delete IS the resolve gesture. Idempotent;
    no-op when md is absent or the Alerts block is missing.

    md mtime gates the deletion path so alerts created AFTER the md
    snapshot (e.g. by a background hook) are not misread as user deletes.
    """
    rpt = ReconcileReport()
    dashboard_path = Path(dashboard_path)
    if not dashboard_path.exists():
        return rpt
    text = dashboard_path.read_text(encoding="utf-8")
    start = text.find(_ALERT_H2)
    if start == -1:
        return rpt
    after_h2 = text[start + len(_ALERT_H2):]
    next_h2 = re.search(r"\n##\s", after_h2)
    block = after_h2[: next_h2.start()] if next_h2 else after_h2

    md_ids: set[int] = set()
    for raw in block.splitlines():
        m = _ALERT_ID_RE.search(raw)
        if not m:
            continue
        try:
            md_ids.add(int(m.group("id")))
        except ValueError:
            continue

    md_mtime_iso = _md_mtime_iso(dashboard_path)

    sql = "SELECT id FROM alerts WHERE resolved=0"
    params: list = []
    if md_mtime_iso:
        sql += " AND created_at <= ?"
        params.append(md_mtime_iso)
    db_unresolved = {r[0] for r in conn.execute(sql, params).fetchall()}

    has_sentinel = "<!-- alert-block-anchored -->" in block
    if not md_ids:
        if not has_sentinel or not db_unresolved:
            return rpt
        now_iso = _now()
        with conn:
            for aid in db_unresolved:
                conn.execute(
                    "UPDATE alerts SET resolved=1, "
                    "resolved_at=COALESCE(resolved_at, ?) "
                    "WHERE id=? AND resolved=0",
                    (now_iso, aid),
                )
                rpt.deleted += 1
        return rpt

    deleted = db_unresolved - md_ids
    if not deleted:
        return rpt
    now_iso = _now()
    with conn:
        for aid in deleted:
            conn.execute(
                "UPDATE alerts SET resolved=1, "
                "resolved_at=COALESCE(resolved_at, ?) "
                "WHERE id=? AND resolved=0",
                (now_iso, aid),
            )
            rpt.deleted += 1
    return rpt


# ── timeline reconcile ────────────────────────────────────────────────────────

_TIMELINE_H2 = "## Timeline"
_TIMELINE_END_MARKER = "<!-- marrow:timeline:end -->"
_TL_TRAIL_T_RE = re.compile(r"t=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
_TL_TRAIL_Z_RE = re.compile(r"z=([0-9a-f]{8})")
# Matches `<!-- tl:<sid> -->` (session) and `<!-- tl:d:YYYY-MM-DD -->` (diary).
_TL_SID_RE  = re.compile(r"<!--\s*tl:(?!d:|e:|ep:)(?P<sid>[^:\s>]+)(?::(?P<seq>\d+))?(?::(?P<ln>\d+))?\s*-->")
_TL_DATE_RE = re.compile(r"<!--\s*tl:d:(?P<date>\d{4}-\d{2}-\d{2})\s*-->")
# Strip anchors from a line to get the user-editable text.
_TL_ANCHOR_RE = re.compile(r"\s*<!--\s*tl:[^>]+-->\s*$")
_TL_MD_DATE_HEADER_RE = re.compile(r"^\*\*\d{2}-\d{2}\s+\w+(?:\s+【[^】]*】)?\*\*\s*")
# Strip leading prefixes from timeline lines before extracting editable text.
# \s* (not \s+) so prefix-only stub lines strip to empty → no write-back.
_TL_HHMM_RE   = re.compile(r"^\d{2}:\d{2}\s*")
_TL_TONE_RE   = re.compile(r"^【[^】]*】\s*")
_TL_PERIOD_RE  = re.compile(r"^(?:AM|PM|ND)\s+")
# Diary day-line prefix: "MM-DD Day 【tone】" (day 4-8 zone).
_TL_DAY_RE    = re.compile(r"^\d{2}-\d{2}\s+Day\s+【[^】]*】\s*")


_TL_TRAIL_RE  = re.compile(r"<!--\s*tl-rendered:(?P<payload>[^>]+)\s*-->")


def _trail_t_iso(block: str) -> str | None:
    """Render timestamp from the tl-rendered trail (t=), or None if absent."""
    m_trail = _TL_TRAIL_RE.search(block)
    if not m_trail:
        return None
    m_t = _TL_TRAIL_T_RE.search(m_trail.group("payload"))
    return m_t.group(1) if m_t else None


def _trail_z(block: str) -> str | None:
    """Zone fingerprint (z=) stored in the tl-rendered trail, or None if the
    trail is absent or pre-migration (no z= field). None → treat as possibly
    human-edited; one render after deploy backfills it."""
    m_trail = _TL_TRAIL_RE.search(block)
    if not m_trail:
        return None
    m_z = _TL_TRAIL_Z_RE.search(m_trail.group("payload"))
    return m_z.group(1) if m_z else None
_TL_EVT_RE    = re.compile(r"<!--\s*tl:e:(?P<eid>\d+)\s*-->")
_TL_EP_RE     = re.compile(r"<!--\s*tl:ep:(?P<epid>\d+)\s*-->")
_TL_PLUS_RE   = re.compile(
    r"^\+\s*(?:(?P<hhmm>\d{2}:\d{2})|(?P<period>AM|PM|ND))?\s*(?P<text>.+)$",
    re.IGNORECASE,
)
_TL_DAY_DIVIDER_RE = re.compile(r"^-+\s*(?P<mmdd>\d{2}-\d{2})\s*-+\s*$")
_TL_DAY_HEADER_RE = re.compile(
    r"^\**(?P<mmdd>\d{2}-\d{2})\s+\w"
)
_TZ_MELB      = _MELB_TZ
_TL_PERIOD_HOUR = {"AM": 9, "PM": 15, "ND": 21}


def _strip_tl_anchor(line: str) -> str:
    return _TL_ANCHOR_RE.sub("", line).rstrip()


def _strip_tl_date_header(line: str) -> str:
    return _TL_MD_DATE_HEADER_RE.sub("", line).strip()


def _extract_tl_text(line: str) -> str:
    """Strip prefixes (HH:MM / AM/PM/ND / MM-DD Day) and anchor — preserves 【tone】."""
    s = _strip_tl_anchor(line)
    s = _TL_HHMM_RE.sub("", s)
    s = _TL_PERIOD_RE.sub("", s)
    s = _TL_DAY_RE.sub("", s)
    return s.strip()


def _tl_now_melb() -> _dt.datetime:
    return _dt.datetime.now(_TZ_MELB)


def _resolve_tl_mmdd(mmdd: str, today: _dt.date) -> _dt.date | None:
    try:
        month, day = (int(x) for x in mmdd.split("-", 1))
        candidate = _dt.date(today.year, month, day)
    except ValueError:
        return None
    if candidate > today:
        try:
            candidate = _dt.date(today.year - 1, month, day)
        except ValueError:
            return None
    return candidate


def _timeline_day_context(line: str, today: _dt.date) -> _dt.date | None:
    m_date = _TL_DATE_RE.search(line)
    if m_date:
        try:
            return _dt.date.fromisoformat(m_date.group("date"))
        except ValueError:
            return None

    m_div = _TL_DAY_DIVIDER_RE.match(line.strip())
    if m_div:
        return _resolve_tl_mmdd(m_div.group("mmdd"), today)

    m_head = _TL_DAY_HEADER_RE.match(line.strip())
    if m_head:
        return _resolve_tl_mmdd(m_head.group("mmdd"), today)

    return None


def _manual_event_ts_utc(day: _dt.date, explicit_day: bool,
                         hhmm_str: str | None,
                         period_str: str | None) -> str:
    now_melb = _tl_now_melb()
    if hhmm_str:
        h, mi = int(hhmm_str[:2]), int(hhmm_str[3:5])
        ts_melb = _dt.datetime(day.year, day.month, day.day, h, mi, tzinfo=_TZ_MELB)
        if not explicit_day and day == now_melb.date() and ts_melb > now_melb:
            ts_melb -= _dt.timedelta(days=1)
    elif period_str:
        h = _TL_PERIOD_HOUR[period_str.upper()]
        ts_melb = _dt.datetime(day.year, day.month, day.day, h, 0, tzinfo=_TZ_MELB)
    elif day == now_melb.date():
        ts_melb = now_melb.replace(microsecond=0)
    else:
        ts_melb = _dt.datetime(day.year, day.month, day.day, 12, 0, tzinfo=_TZ_MELB)
    return ts_melb.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_TL_SELF_RE = re.compile(
    r"^(?P<rng>\d{2}:\d{2}(?:-\d{2}:\d{2})?)?\s*"
    r"(?:【(?P<label>[^】]*)】)?\s*(?P<body>.*)$",
    re.DOTALL,
)


def _reconcile_self_edit(conn, rpt, eid, raw_text, row, now_iso) -> None:
    """Round-trip an edited self (tl_add) line back to events.content.

    Parses HH:mm[-HH:mm] 【label】body -> events.content = 【label】body
    (the affect phrase lives inside content; no affect row to touch).
    """
    m = _TL_SELF_RE.match(raw_text.strip())
    if not m:
        rpt.unchanged += 1
        return
    body = (m.group("body") or "").strip()
    label = m.group("label")
    new_content = f"【{label.strip()}】{body}" if label else body
    if new_content and new_content != (row["content"] or ""):
        conn.execute("UPDATE events SET content=?, updated_at=? WHERE id=?",
                     (new_content, now_iso, eid))
        try:
            conn.execute("DELETE FROM events_vec WHERE rowid=?", (eid,))
            conn.execute("DELETE FROM events_vec_meta WHERE rowid=?", (eid,))
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary)"
            " VALUES ('events', ?, 'tl_self_edit', ?)",
            (str(eid), f"md-reconcile: {new_content[:40]!r}"))
        rpt.updated += 1
    else:
        rpt.unchanged += 1


def reconcile_timeline(conn: sqlite3.Connection,
                       dashboard_path: str | Path,
                       *, db: str | None = None) -> ReconcileReport:
    """Absorb timeline edits from the dashboard ## Timeline block back into DB.

    Anchors:
      `<!-- tl:<sid> -->` → session_digests anchor (present/absent → hidden sweep)
      `<!-- tl:d:YYYY-MM-DD -->` → diary anchor (present/absent → hidden sweep)
      `<!-- tl:e:N -->` → events.content for manual event id N (edit)
      `<!-- tl-rendered:s=...;d=...;e=... -->` → trail marker; absent sid/date/evt = hidden

    Lines starting with `+ ` → insert as manual events (channel='manual').
    """
    rpt = ReconcileReport()
    dashboard_path = Path(dashboard_path)
    if not dashboard_path.exists():
        return rpt
    text = dashboard_path.read_text(encoding="utf-8")
    start = text.find(_TIMELINE_H2)
    if start == -1:
        return rpt
    after_h2 = text[start + len(_TIMELINE_H2):]
    # Block ends at the earliest of the next H2 or the timeline end marker.
    # Dashboard files have no end marker → falls back to next-H2 behaviour.
    next_h2 = re.search(r"\n##\s", after_h2)
    end_marker = after_h2.find(_TIMELINE_END_MARKER)
    ends = [m.start() if hasattr(m, "start") else m
            for m in (next_h2, end_marker) if m is not None and m != -1]
    block = after_h2[: min(ends)] if ends else after_h2
    # Gate base: render timestamp from the trail (t=), when the block content
    # actually last changed. Falls back to file mtime for legacy files or a
    # hand-deleted trail. A volatile second writer (Status zone) bumps mtime
    # every render, so mtime alone would lose its "content last rendered" meaning.
    trail_t_iso = _trail_t_iso(block)
    md_mtime_iso = trail_t_iso or _md_mtime_iso(dashboard_path)
    # Zone fingerprint from the trail: lets the db_win branch tell render
    # residue (zone untouched since render) from a clobbered human edit,
    # content-based rather than mtime-based (mtime lies on multi-zone pages
    # like daybrief.md where a co-writer rewrites the file each render).
    trail_z = _trail_z(block)

    # Parse trail marker first — tells us what was rendered last time
    trail_sid_seqs: set[tuple[str, int]] = set()
    trail_sid_triples: set[tuple[str, int, int]] = set()
    trail_dates: set[str] = set()
    trail_evts:  set[int] = set()
    trail_eps:   set[int] = set()
    m_trail = _TL_TRAIL_RE.search(block)
    if m_trail:
        for segment in m_trail.group("payload").split(";"):
            segment = segment.strip()
            if segment.startswith("s="):
                for x in segment[2:].split(","):
                    x = x.strip()
                    if not x:
                        continue
                    parts = x.split(":")
                    if len(parts) >= 3:
                        trail_sid_seqs.add((parts[0].strip(), int(parts[1].strip())))
                        trail_sid_triples.add((parts[0].strip(), int(parts[1].strip()), int(parts[2].strip())))
                    elif len(parts) == 2:
                        trail_sid_seqs.add((parts[0].strip(), int(parts[1].strip())))
                    else:
                        trail_sid_seqs.add((parts[0].strip(), 0))
            elif segment.startswith("d="):
                trail_dates.update(x.strip() for x in segment[2:].split(",") if x.strip())
            elif segment.startswith("ep="):
                trail_eps.update(int(x.strip()) for x in segment[3:].split(",") if x.strip())
            elif segment.startswith("e="):
                trail_evts.update(int(x.strip()) for x in segment[2:].split(",") if x.strip())

    sid_edits:      dict[tuple[str, int, int | None], str] = {}
    date_edits:     dict[str, str] = {}
    date_overview_edits: dict[str, str] = {}
    tone_edits:     dict[str, str] = {}
    present_sid_seqs: set[tuple[str, int]] = set()
    present_sid_triples: set[tuple[str, int, int]] = set()
    present_dates:  set[str] = set()
    block_dates:    set[str] = set()   # ALL dates from day-context headers
    present_evts:  set[int] = set()
    present_eps:   set[int] = set()
    plus_lines:    list[tuple[_dt.date, bool, str]] = []
    evt_edits:     dict[int, str] = {}
    now_melb = _tl_now_melb()
    current_day = now_melb.date()
    current_day_explicit = False
    pending_overview_date: str | None = None

    for raw in block.splitlines():
        line = raw.rstrip()
        # Skip the trail marker line itself
        if _TL_TRAIL_RE.search(line):
            continue
        if pending_overview_date and line.strip() and "<!--" not in line:
            date_overview_edits[pending_overview_date] = line.strip()
            pending_overview_date = None
        day_context = _timeline_day_context(line, now_melb.date())
        if day_context is not None:
            current_day = day_context
            current_day_explicit = True
            block_dates.add(day_context.isoformat())
        # Lines starting with `+ ` are manual add requests
        if _TL_PLUS_RE.match(line):
            plus_lines.append((current_day, current_day_explicit, line))
            continue
        # Episode anchor (unresolved affect)
        m_ep = _TL_EP_RE.search(line)
        if m_ep:
            present_eps.add(int(m_ep.group("epid")))
            continue
        # Event anchor (tl:e) — manual events + self (tl_add) rows.
        # Store the anchor-stripped line; prefix/label parsing is channel-aware
        # and happens in the write phase.
        m_evt = _TL_EVT_RE.search(line)
        if m_evt:
            eid = int(m_evt.group("eid"))
            present_evts.add(eid)
            text_part = re.sub(r"\s*<!--\s*tl:e:\d+\s*-->\s*$", "", line).rstrip()
            if text_part:
                evt_edits[eid] = text_part
            continue
        m_sid = _TL_SID_RE.search(line)
        if m_sid:
            sid = m_sid.group("sid")
            seq = int(m_sid.group("seq")) if m_sid.group("seq") else 0
            ln = int(m_sid.group("ln")) if m_sid.group("ln") else None
            present_sid_seqs.add((sid, seq))
            if ln is not None:
                present_sid_triples.add((sid, seq, ln))
            text_part = _strip_tl_date_header(_strip_tl_anchor(line))
            if text_part:
                sid_edits[(sid, seq, ln)] = text_part
            continue
        m_date = _TL_DATE_RE.search(line)
        if m_date:
            date = m_date.group("date")
            present_dates.add(date)
            pending_overview_date = date
            text_part = _extract_tl_text(line)
            if text_part:
                date_edits[date] = text_part
            tone_m = re.search(r'【([^】]+)】', line)
            if tone_m:
                tone_edits[date] = tone_m.group(1)

    if (not sid_edits and not date_edits and not date_overview_edits
            and not tone_edits and not m_trail and not plus_lines
            and not evt_edits and not trail_eps and not trail_sid_seqs
            and not present_sid_seqs and not present_dates
            and not block_dates
            and not present_evts and not present_eps):
        return rpt

    now_iso = _now()
    # db-win skips: DB row newer than the render gate AND md text still differs
    # → md is stale, DB kept. Collected here, one summary alert per run below.
    db_win_skips: list[str] = []
    with conn:
        for (sid, seq, ln), new_text in sid_edits.items():
            row = conn.execute(
                "SELECT life_lines, COALESCE(updated_at, ts) AS mts"
                " FROM session_digests WHERE sid = ? AND segment_seq = ?",
                (sid, seq),
            ).fetchone()
            if row is None:
                rpt.conflicts.append(f"tl:sid {sid!r}:{seq} not in session_digests")
                continue
            if md_mtime_iso and (row["mts"] or "") > md_mtime_iso:
                _life = (row["life_lines"] or "").splitlines()
                if ln is not None and 0 <= ln < len(_life) and _life[ln] != new_text:
                    db_win_skips.append(f"{sid}:{seq}:{ln}")
                rpt.unchanged += 1
                continue
            if ln is None:
                rpt.unchanged += 1
                continue
            life_lines = (row["life_lines"] or "").splitlines()
            if ln < 0 or ln >= len(life_lines):
                rpt.conflicts.append(
                    f"tl:sid {sid!r}:{seq}:{ln} life_lines index out of range"
                )
                continue
            if life_lines[ln] == new_text:
                rpt.unchanged += 1
                continue
            life_lines[ln] = new_text
            conn.execute(
                "UPDATE session_digests SET life_lines=?, updated_at=?"
                " WHERE sid=? AND segment_seq=?",
                ("\n".join(life_lines), now_iso, sid, seq),
            )
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('session_digests', ?, 'tl_edit', ?)",
                (f"{sid}:{seq}:{ln}", f"md-reconcile: life_lines[{ln}]={new_text[:60]!r}"),
            )
            rpt.updated += 1

        for date in date_edits:
            row = conn.execute(
                "SELECT rowid FROM diary WHERE date = ?", (date,)
            ).fetchone()
            if row is None:
                now_melb = _dt.datetime.now(_MELB_TZ)
                diary_cutoff = now_melb.date().isoformat()
                if date >= diary_cutoff:
                    continue
                rpt.conflicts.append(f"tl:d:{date} not in diary")
                continue
            rpt.unchanged += 1

        for date, overview in date_overview_edits.items():
            row = conn.execute(
                "SELECT overview, COALESCE(updated_at, date) AS mts"
                " FROM diary WHERE date = ?",
                (date,),
            ).fetchone()
            if row is None:
                now_melb = _dt.datetime.now(_MELB_TZ)
                diary_cutoff = now_melb.date().isoformat()
                if date >= diary_cutoff:
                    continue
                rpt.conflicts.append(f"tl:d:{date} not in diary")
                continue
            if md_mtime_iso and (row["mts"] or "") > md_mtime_iso:
                rpt.unchanged += 1
                continue
            if (row["overview"] or "") == overview:
                rpt.unchanged += 1
                continue
            conn.execute(
                "UPDATE diary SET overview=?, updated_at=? WHERE date=?",
                (overview, now_iso, date),
            )
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('diary', ?, 'overview_edit', ?)",
                (date, f"md-reconcile: overview={overview[:60]!r}"),
            )
            rpt.updated += 1

        for date, tone in tone_edits.items():
            row = conn.execute(
                "SELECT tone, COALESCE(updated_at, date) AS mts FROM diary WHERE date=?",
                (date,),
            ).fetchone()
            if row is None:
                continue
            if md_mtime_iso and (row["mts"] or "") > md_mtime_iso:
                rpt.unchanged += 1
                continue
            if (row["tone"] or "") == tone:
                rpt.unchanged += 1
                continue
            conn.execute(
                "UPDATE diary SET tone=?, updated_at=? WHERE date=?",
                (tone, now_iso, date),
            )
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('diary', ?, 'tone_edit', ?)",
                (date, f"md-reconcile: tone={tone!r}"),
            )
            rpt.updated += 1

        # ── DELETE: expected anchors absent from current block → hidden ──
        if m_trail:
            expected_sid_seqs = trail_sid_seqs
            expected_dates = trail_dates
            expected_evts = trail_evts
            expected_eps = trail_eps
        elif present_sid_seqs or present_dates or present_evts or present_eps:
            # Trail marker absent — reconstruct expected from DB,
            # scoped to dates observed in the MD block.
            # block_dates covers ALL day-context headers (zone A bare
            # headers + zone B tl:d: anchored); present_dates only has
            # tl:d:-anchored dates. Use the union for session_digests/events
            # scope so zone A deletions are also detected.
            expected_sid_seqs: set[tuple[str, int]] = set()
            expected_dates: set[str] = set()
            expected_evts: set[int] = set()
            expected_eps: set[int] = set()
            scope_dates = block_dates | present_dates
            if scope_dates:
                ph = ",".join("?" * len(scope_dates))
                dates_vals = tuple(sorted(scope_dates))
                expected_sid_seqs = {
                    (r["sid"], r["segment_seq"])
                    for r in conn.execute(
                        "SELECT sid, segment_seq FROM session_digests"
                        f" WHERE tl_hidden=0 AND date IN ({ph})",
                        dates_vals,
                    ).fetchall()
                }
                # Diary deletion detection: only tl:d:-anchored dates
                if present_dates:
                    ph_d = ",".join("?" * len(present_dates))
                    dates_d = tuple(sorted(present_dates))
                    expected_dates = {
                        r["date"]
                        for r in conn.execute(
                            "SELECT date FROM diary"
                            f" WHERE tl_hidden=0 AND date IN ({ph_d})",
                            dates_d,
                        ).fetchall()
                    }
                min_d, max_d = min(scope_dates), max(scope_dates)
                from_utc = _dt.datetime.combine(
                    _dt.date.fromisoformat(min_d), _dt.time.min,
                    tzinfo=_TZ_MELB,
                ).astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                to_utc = _dt.datetime.combine(
                    _dt.date.fromisoformat(max_d) + _dt.timedelta(days=1),
                    _dt.time.min, tzinfo=_TZ_MELB,
                ).astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                expected_evts = {
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM events"
                        " WHERE (channel='manual' OR role='tl')"
                        " AND timestamp >= ? AND timestamp < ?",
                        (from_utc, to_utc),
                    ).fetchall()
                }
            expected_eps = {
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM affect WHERE unresolved=1",
                ).fetchall()
            }
        else:
            expected_sid_seqs = set()
            expected_dates = set()
            expected_evts = set()
            expected_eps = set()

        for (sid, seq) in expected_sid_seqs - present_sid_seqs:
            if md_mtime_iso:
                r = conn.execute(
                    "SELECT COALESCE(updated_at, ts) AS mts FROM session_digests"
                    " WHERE sid=? AND segment_seq=?", (sid, seq)
                ).fetchone()
                if r and (r["mts"] or "") > md_mtime_iso:
                    continue
            conn.execute(
                "UPDATE session_digests SET tl_hidden=1 WHERE sid=? AND segment_seq=?",
                (sid, seq))
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('session_digests', ?, 'tl_delete', 'user deleted tl line')",
                (f"{sid}:{seq}",))
            rpt.updated += 1

        # Partial deletion: individual life_lines removed while digest stays
        if trail_sid_triples:
            _deleted_triples = trail_sid_triples - present_sid_triples
            _deleted_by_digest: dict[tuple[str, int], set[int]] = {}
            for _s, _q, _ln in _deleted_triples:
                if (_s, _q) in present_sid_seqs:
                    _deleted_by_digest.setdefault((_s, _q), set()).add(_ln)
            for (p_sid, p_seq), deleted_lns in _deleted_by_digest.items():
                if md_mtime_iso:
                    r = conn.execute(
                        "SELECT COALESCE(updated_at, ts) AS mts FROM session_digests"
                        " WHERE sid=? AND segment_seq=?", (p_sid, p_seq)
                    ).fetchone()
                    if r and (r["mts"] or "") > md_mtime_iso:
                        continue
                row = conn.execute(
                    "SELECT life_lines FROM session_digests"
                    " WHERE sid=? AND segment_seq=?", (p_sid, p_seq),
                ).fetchone()
                if row is None:
                    continue
                life = (row["life_lines"] or "").splitlines()
                new_life = [ln_text for i, ln_text in enumerate(life)
                            if i not in deleted_lns]
                conn.execute(
                    "UPDATE session_digests SET life_lines=?, updated_at=?"
                    " WHERE sid=? AND segment_seq=?",
                    ("\n".join(new_life), now_iso, p_sid, p_seq),
                )
                conn.execute(
                    "INSERT INTO audit_log (target_table, target_id, action, summary)"
                    " VALUES ('session_digests', ?, 'tl_partial_delete', ?)",
                    (f"{p_sid}:{p_seq}",
                     f"removed line_indexes {sorted(deleted_lns)}"),
                )
                rpt.updated += 1

        for date in expected_dates - present_dates:
            if md_mtime_iso:
                r = conn.execute(
                    "SELECT COALESCE(updated_at, date) AS mts FROM diary WHERE date=?",
                    (date,)
                ).fetchone()
                if r and (r["mts"] or "") > md_mtime_iso:
                    continue
            conn.execute(
                "UPDATE diary SET tl_hidden=1 WHERE date=?", (date,))
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('diary', ?, 'tl_delete', 'user deleted tl line')",
                (date,))
            rpt.updated += 1
        for eid in expected_evts - present_evts:
            erow = conn.execute(
                "SELECT channel, role, COALESCE(updated_at, created_at) AS mts"
                " FROM events WHERE id=?", (eid,)
            ).fetchone()
            is_self = erow is not None and erow["role"] == "tl"
            if erow is None or not (is_self or erow["channel"] == "manual"):
                continue
            if md_mtime_iso and (erow["mts"] or "") > md_mtime_iso:
                continue
            conn.execute(
                "DELETE FROM events WHERE id=? AND (channel='manual' OR role='tl')",
                (eid,))
            try:
                conn.execute("DELETE FROM events_vec WHERE rowid=?", (eid,))
                conn.execute("DELETE FROM events_vec_meta WHERE rowid=?", (eid,))
            except sqlite3.OperationalError:
                pass
            _action = "tl_self_delete" if is_self else "tl_manual_delete"
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('events', ?, ?, 'user deleted timeline event')",
                (str(eid), _action))
            rpt.updated += 1
        for epid in expected_eps - present_eps:
            if md_mtime_iso:
                r = conn.execute(
                    "SELECT COALESCE(updated_at, created_at) AS mts FROM affect WHERE id=?",
                    (epid,)
                ).fetchone()
                if r and (r["mts"] or "") > md_mtime_iso:
                    continue
            conn.execute(
                "UPDATE affect SET resolved_at=?, unresolved=0, updated_at=? WHERE id=?",
                (now_iso, now_iso, epid))
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('affect', ?, 'ep_resolve', 'user deleted unresolved episode from timeline')",
                (str(epid),))
            rpt.updated += 1

        # ── ADD: lines starting with `+ ` → insert manual events ─────────────
        for day_context, explicit_day, raw_line in plus_lines:
            m_plus = _TL_PLUS_RE.match(raw_line)
            if not m_plus:
                rpt.conflicts.append(f"tl:+ unparseable: {raw_line[:60]!r}")
                continue
            hhmm_str = m_plus.group("hhmm")
            period_str = m_plus.group("period")
            add_text = (m_plus.group("text") or "").strip()
            if not add_text:
                rpt.conflicts.append(f"tl:+ empty text: {raw_line[:60]!r}")
                continue
            if hhmm_str:
                try:
                    int(hhmm_str[:2]), int(hhmm_str[3:5])
                except ValueError:
                    rpt.conflicts.append(f"tl:+ bad time {hhmm_str!r}: {raw_line[:60]!r}")
                    continue
            ts_utc = _manual_event_ts_utc(day_context, explicit_day, hhmm_str, period_str)
            sid_manual = "manual:" + secrets.token_hex(4)
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content, channel)"
                " VALUES (?, ?, 'user', ?, 'manual')",
                (sid_manual, ts_utc, add_text),
            )
            rpt.updated += 1

        # ── EDIT: event content (manual + self rows) ──────────────────────────
        for eid, raw_text in evt_edits.items():
            row = conn.execute(
                "SELECT content, channel, role,"
                " COALESCE(updated_at, created_at) AS mts FROM events WHERE id=?",
                (eid,)
            ).fetchone()
            if row is None:
                rpt.conflicts.append(f"tl:e:{eid} not in events")
                continue
            # Freshness gate: DB row content-written after the render (t=) AND
            # md text still differs → md is the stale second surface, DB wins.
            # Prevents the two-writer ping-pong (dashboard.md ⇄ daybrief.md).
            if md_mtime_iso and (row["mts"] or "") > md_mtime_iso:
                _cur = (row["content"] or "")
                _md = _TL_PERIOD_RE.sub("", _TL_HHMM_RE.sub("", raw_text)).strip()
                if row["role"] == "tl":
                    _m = _TL_SELF_RE.match(raw_text.strip())
                    if _m:
                        _b = (_m.group("body") or "").strip()
                        _l = _m.group("label")
                        _md = f"【{_l.strip()}】{_b}" if _l else _b
                if _md and _md != _cur:
                    db_win_skips.append(f"e:{eid}")
                    rpt.unchanged += 1
                    continue
            if row["role"] == "tl":
                _reconcile_self_edit(conn, rpt, eid, raw_text, row, now_iso)
                continue
            if row["channel"] != "manual":
                rpt.conflicts.append(
                    f"tl:e:{eid} not a manual event (channel={row['channel']!r})")
                continue
            new_text = _TL_PERIOD_RE.sub("", _TL_HHMM_RE.sub("", raw_text)).strip()
            if not new_text:
                rpt.unchanged += 1
                continue
            if new_text != (row["content"] or ""):
                conn.execute("UPDATE events SET content=?, updated_at=? WHERE id=?",
                             (new_text, now_iso, eid))
                try:
                    conn.execute("DELETE FROM events_vec WHERE rowid=?", (eid,))
                    conn.execute("DELETE FROM events_vec_meta WHERE rowid=?", (eid,))
                except sqlite3.OperationalError:
                    pass
                conn.execute(
                    "INSERT INTO audit_log (target_table, target_id, action, summary)"
                    " VALUES ('events', ?, 'tl_edit', ?)",
                    (str(eid), f"md-reconcile: content={new_text[:60]!r}"))
                rpt.updated += 1
            else:
                rpt.unchanged += 1

    if db_win_skips:
        # Split render residue (zone untouched since last render → silent
        # self-heal) from a clobbered human edit (zone text changed after
        # render → real warning). Recompute the zone fingerprint over the
        # CURRENT file zone and compare to the trail's stored z=. Matching
        # z= → nobody touched the timeline zone since we wrote it → residue.
        # z= differs or absent (pre-migration trail) → possible human edit.
        from .timeline import _zone_fingerprint
        current_z = _zone_fingerprint(_TIMELINE_H2 + block)
        residue = trail_z is not None and current_z == trail_z
        if residue:
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('timeline', ?, 'md_stale_db_win', ?)",
                (db_win_skips[0],
                 f"{len(db_win_skips)} stale md line(s) kept DB text"))
        else:
            from . import repo as _repo
            shown = ", ".join(db_win_skips[:10])
            more = f" +{len(db_win_skips) - 10} more" if len(db_win_skips) > 10 else ""
            _repo.add_alert(
                "warn", "timeline", "timeline_reconcile:db_win",
                source="reconcile.py", db=db,
                message=(f"{len(db_win_skips)} stale md line(s) kept DB text: "
                         f"{shown}{more}"),
            )
    # Own our writes: bare conn.execute() outside the `with conn:` blocks
    # above (e.g. the residue audit_log INSERT) opens an implicit transaction
    # under sqlite3's default isolation. daybrief.update() never commits its
    # read-only conn, so those rows were silently rolled back on close().
    # No-op when there's no open transaction (safe on every call path).
    conn.commit()
    return rpt
