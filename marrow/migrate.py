"""Phase 1 historical md importer. Per-source parsers + idempotent insert.
Default dry-run; --apply writes. Behaviour contract: SCHEMA.md mapping.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

BIRTH_YEAR = 1995


def parse_events_2026(text: str) -> list[dict]:
    rows, in_log = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("### "):
            in_log = False
            continue
        if s == "[log]":
            in_log = True
            continue
        if in_log and s:
            rows.append({
                "session_id": "legacy-2026",
                "timestamp": "2026-01-01T00:00:00Z",
                "role": "log",
                "content": s,
                "channel": "cli",
                "compressed": 1,
            })
    return rows


def parse_pit(text: str) -> list[dict]:
    rows: list[dict] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur:
            cur["description"] = cur["description"].strip()
            rows.append(cur)
            cur = None

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            flush()
            title = re.sub(r"\s*\[(low|medium|high)\]\s*$", "", s[3:].strip())
            cur = {"title": title.strip(), "description": "",
                   "status": "idea", "related_files": None}
        elif s.startswith("# ") or s.startswith(">"):
            continue
        elif cur is not None and s:
            cur["description"] += ("\n" if cur["description"] else "") + s
    flush()
    return rows


def parse_goose_bites(text: str) -> list[dict]:
    """Parse single-line format: `- [YYYY-MM-DD]<quote>`. One row per line."""
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("![["):
            continue
        m = re.match(r"^- \[(\d{4}-\d{2}-\d{2})\](.+)$", s)
        if m:
            rows.append({"date": m.group(1), "session_id": None,
                         "bites": m.group(2).strip(), "best": 1})
    return rows


def lighthouse_milestone() -> dict:
    return {"scope": "me", "date": "2026-05-15",
            "title": "Marrow 记忆系统重构",
            "description": "重构 NY memm：SQLite 存储、模型无关、单一 dashboard，可开源。",
            "theme": None, "pinned": 1}


def parse_memes_cipher(text: str) -> list[dict]:
    rows, inblk = [], False
    for line in text.splitlines():
        s = line.strip()
        if s == "<cipher>":
            inblk = True
            continue
        if s == "</cipher>":
            inblk = False
            continue
        if inblk and s.startswith("- ") and ": " in s:
            key, _, val = s[2:].partition(": ")
            val = re.sub(r"\s*\[P\]\s*$", "", val).strip()
            rows.append({"type": "cipher", "key": key.strip(), "value": val,
                         "use_count": 0, "last_seen": None})
    return rows


def parse_milestones_timeline(text: str) -> list[dict]:
    """Parse timeline.md ## Me / ## Us sections.

    timeline.md = curated history — every row is a confirmed fact, so
    pinned=1 from the start. Candidates (pinned=0) come from session
    digests, never from this curated md. See import_timeline().
    """
    rows: list[dict] = []
    section = None
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur:
            rows.append(cur)
            cur = None

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            flush()
            section = s[3:].strip()
            continue
        if section == "Me":
            m = re.match(r"\[Age (\d+)[^\]]*\]$", s)
            if m:
                flush()
                cur = {"scope": "me",
                       "date": str(BIRTH_YEAR + int(m.group(1))),
                       "title": s[1:-1].strip(), "description": "",
                       "theme": None, "pinned": 1}
            elif cur and s and not s.startswith(">"):
                cur["description"] = (cur["description"] + " " + s).strip()
        elif section == "Us":
            m = re.match(r"\[(\d{4}-\d{2}-\d{2})\] (.+)", s)
            if m:
                rest = m.group(2)
                title, _, desc = rest.partition(": ")
                rows.append({"scope": "us", "date": m.group(1),
                             "title": title.strip(), "description": desc.strip(),
                             "theme": None, "pinned": 1})
    flush()
    return rows


def _hash(table: str, row: dict) -> str:
    blob = table + "|" + json.dumps(row, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def _milestone_natural_key(row: dict) -> str:
    """Stable identity for a timeline row: scope|date|title.

    Used by import_timeline so re-import after Lumi pins/edits a row
    does not duplicate. The generic _insert uses full-row hash which
    flips on pinned/description changes — wrong for backfill.
    """
    return f"milestones|{row['scope']}|{row['date']}|{row['title']}"


def _insert(conn: sqlite3.Connection, table: str, rows: list[dict],
            apply: bool) -> tuple[int, int]:
    ins = skip = 0
    for r in rows:
        h = _hash(table, r)
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE source_hash = ?", (h,)
        ).fetchone():
            skip += 1
            continue
        # Anti-revive: milestones rows Lumi has dropped (reconcile writes a
        # tombstone keyed on milestones|scope|date|title) must not come back
        # via backfill. Counted as skip — invisible to caller stats shape.
        if table == "milestones":
            nh = hashlib.sha256(
                _milestone_natural_key(r).encode()
            ).hexdigest()
            tomb = conn.execute(
                "SELECT 1 FROM audit_log WHERE target_table='milestones'"
                " AND action='tombstone' AND summary LIKE ? LIMIT 1",
                (f"%{nh}%",),
            ).fetchone()
            if tomb:
                skip += 1
                continue
        if apply:
            cols = list(r.keys()) + ["source_hash"]
            ph = ",".join("?" * len(cols))
            conn.execute(
                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})",
                [*r.values(), h],
            )
        ins += 1
    return ins, skip


# source key -> (table, parser). Lighthouse is appended to milestones.
_PLAN = {
    "events_2026": ("events", parse_events_2026),
    "timeline": ("milestones", parse_milestones_timeline),
    "cipher": ("memes", parse_memes_cipher),
    "pit": ("pit", parse_pit),
    "goose": ("goose_bites", parse_goose_bites),
}


def migrate(conn: sqlite3.Connection, sources: dict[str, str],
            apply: bool = False) -> dict[str, tuple[int, int]]:
    stats: dict[str, tuple[int, int]] = {}
    for key, (table, parser) in _PLAN.items():
        if key not in sources:
            continue
        rows = parser(sources[key])
        if key == "timeline":
            rows.append(lighthouse_milestone())
        i, s = _insert(conn, table, rows, apply)
        prev = stats.get(table, (0, 0))
        stats[table] = (prev[0] + i, prev[1] + s)
    if apply:
        conn.commit()
    return stats


def import_timeline(conn: sqlite3.Connection, text: str, *,
                    apply: bool = False) -> dict[str, int]:
    """Idempotent timeline.md backfill keyed on (scope, date, title).

    timeline.md = curated history — every row lands pinned=1 (the parser
    sets it). Candidates (pinned=0) come from session digests, not from
    this curated md, so the subpage shows them directly without going
    through the dashboard ✅/❌ vote loop.

    Re-runnable after Lumi pins/edits without duplication.
    Existing rows: backfill description only if currently NULL/empty
    AND parser found one (never overwrite Lumi's hand-edit).
    New rows: INSERT with parser values + (scope, date, title) hash.
    Tombstones (audit_log action='tombstone' on milestones, with the
    same natural-key hash recorded in summary) block re-insert — Lumi
    drops stay dropped across reruns.

    Returns counts: {inserted, backfilled, skipped, tombstoned}.
    """
    rows = parse_milestones_timeline(text)
    inserted = backfilled = skipped = tombstoned = 0
    for r in rows:
        nat = _milestone_natural_key(r)
        h = hashlib.sha256(nat.encode()).hexdigest()
        existing = conn.execute(
            "SELECT id, description FROM milestones"
            " WHERE scope=? AND date=? AND title=?",
            (r["scope"], r["date"], r["title"]),
        ).fetchone()
        if existing:
            # Backfill description ONLY if currently empty and parser has one.
            if (not (existing["description"] or "").strip()) and r.get("description"):
                if apply:
                    conn.execute(
                        "UPDATE milestones SET description=?, source_hash=?,"
                        " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                        " WHERE id=?",
                        (r["description"], h, existing["id"]),
                    )
                backfilled += 1
            else:
                skipped += 1
            continue
        tomb = conn.execute(
            "SELECT 1 FROM audit_log"
            " WHERE target_table='milestones' AND action='tombstone'"
            "   AND summary LIKE ?",
            (f"%{h}%",),
        ).fetchone()
        if tomb:
            tombstoned += 1
            continue
        if apply:
            conn.execute(
                "INSERT INTO milestones"
                " (scope, date, title, description, theme, pinned, source_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (r["scope"], r["date"], r["title"], r["description"],
                 r["theme"], r["pinned"], h),
            )
        inserted += 1
    if apply:
        conn.commit()
    return {"inserted": inserted, "backfilled": backfilled,
            "skipped": skipped, "tombstoned": tombstoned}


_SRC_FILES = {
    "events_2026": "memory/2026.md",
    "timeline": "memory/timeline.md",
    "cipher": "memory/reference.md",
    "pit": "code/pit.md",
}
_GOOSE_GLOB = "铁锅/语录/*.md"


def main() -> None:
    import argparse
    import glob
    from pathlib import Path

    from . import storage

    ap = argparse.ArgumentParser(prog="marrow.migrate")
    ap.add_argument("--apply", action="store_true",
                    help="write to db (default: dry-run preview)")
    from .paths import paths as _paths
    ap.add_argument("--ny-root",
                    default=str(_paths.ny_root))
    ap.add_argument("--timeline-only", action="store_true",
                    help="only run idempotent timeline.md backfill "
                         "(keyed on scope+date+title, safe re-run after pins)")
    args = ap.parse_args()

    root = Path(args.ny_root)
    sources: dict[str, str] = {}
    for key, rel in _SRC_FILES.items():
        p = root / rel
        if p.exists():
            sources[key] = p.read_text(encoding="utf-8")
    goose = sorted(glob.glob(str(root / _GOOSE_GLOB)))
    if goose:
        sources["goose"] = "\n".join(
            Path(g).read_text(encoding="utf-8") for g in goose)

    conn = storage.init_db()
    mode = "APPLY" if args.apply else "DRY-RUN"

    if args.timeline_only:
        tl = sources.get("timeline")
        if not tl:
            print(f"[{mode}] no timeline.md at {root}/memory/timeline.md")
            return
        stats = import_timeline(conn, tl, apply=args.apply)
        print(f"[{mode}] marrow timeline backfill (idempotent on scope+date+title)")
        print(f"  inserted   +{stats['inserted']}  (pinned=1, curated history)")
        print(f"  backfilled +{stats['backfilled']}  (description added "
              f"to existing rows where empty)")
        print(f"  skipped    ~{stats['skipped']}  (already complete)")
        print(f"  tombstoned ~{stats.get('tombstoned', 0)}  "
              f"(blocked by audit_log tombstone)")
        if not args.apply:
            print("  (no rows written; re-run with --apply)")
        return

    stats = migrate(conn, sources, apply=args.apply)
    print(f"[{mode}] marrow migrate")
    for table, (ins, skip) in sorted(stats.items()):
        print(f"  {table:12} +{ins} insert  ~{skip} skip")
    if not args.apply:
        print("  (no rows written; re-run with --apply)")


if __name__ == "__main__":
    main()
