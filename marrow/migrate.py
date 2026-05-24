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
    rows: list[dict] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur and cur["bites"].strip():
            rows.append(cur)
        cur = None

    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"### (\d{4}-\d{2}-\d{2})$", s)
        if m:
            flush()
            cur = {"date": m.group(1), "session_id": None,
                   "bites": "", "best": 0}
        elif s.startswith("![["):
            continue
        elif cur is not None and s:
            cur["bites"] += ("\n" if cur["bites"] else "") + s
    flush()
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
                         "context": None, "use_count": 0, "last_seen": None})
    return rows


def parse_milestones_timeline(text: str) -> list[dict]:
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
                       "theme": None, "pinned": 0}
            elif cur and s and not s.startswith(">"):
                cur["description"] = (cur["description"] + " " + s).strip()
        elif section == "Us":
            m = re.match(r"\[(\d{4}-\d{2}-\d{2})\] (.+)", s)
            if m:
                rest = m.group(2)
                title, _, desc = rest.partition(": ")
                rows.append({"scope": "us", "date": m.group(1),
                             "title": title.strip(), "description": desc.strip(),
                             "theme": None, "pinned": 0})
    flush()
    return rows


def _hash(table: str, row: dict) -> str:
    blob = table + "|" + json.dumps(row, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


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


_SRC_FILES = {
    "events_2026": "memory/2026.md",
    "timeline": "memory/timeline.md",
    "cipher": "memory/reference.md",
    "pit": "code/_pit.md",
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
    ap.add_argument("--ny-root",
                    default=str(Path.home() / "Desktop" / "NY"))
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
    stats = migrate(conn, sources, apply=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] marrow migrate")
    for table, (ins, skip) in sorted(stats.items()):
        print(f"  {table:12} +{ins} insert  ~{skip} skip")
    if not args.apply:
        print("  (no rows written; re-run with --apply)")


if __name__ == "__main__":
    main()
