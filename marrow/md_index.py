"""md_index — canonical TombstoneStore + block parser + full_scan reconcile.

Phase 3 md=SoT: md files are authoritative. md_index tracks per-block
content_hash so the watcher can detect insert/update/delete via diff.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Protocol

# Block id marker. Date is optional — existing memes/tasks shipped `<!-- id:N -->`
# without date; Phase 3 plan adds an optional date attr but stays back-compatible.
_ID_RE = re.compile(r"<!--\s*id:([A-Za-z0-9_:.-]+)(?:\s+date:(\d{4}-\d{2}-\d{2}))?\s*-->")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass
class Block:
    block_id: str
    content_hash: str
    body: str
    line_start: int


@dataclass
class ReconcileReport:
    scanned_files: int = 0
    inserted: int = 0
    updated: int = 0
    tombstoned: int = 0
    cleared: int = 0
    files_without_markers: list[str] = field(default_factory=list)


class TombstoneStore(Protocol):
    """Canonical API shared between watcher, inserters, and handover.

    wt-handover mirrors this Protocol with a placeholder impl; wt-md-a ships
    the real backing in marrow/md_index.py MdIndex. Phase F switches the
    binding, not the callers.
    """

    def record_block(self, path: str, block_id: str, content_hash: str) -> None: ...
    def get_hash(self, path: str, block_id: str) -> str | None: ...
    def tombstone(self, path: str, block_id: str) -> None: ...
    def clear_tombstone(self, path: str, block_id: str) -> None: ...
    def list_tombstones(self, path: str) -> list[tuple[str, str]]: ...
    def full_scan(self, roots: list[str]) -> ReconcileReport: ...


def parse_blocks(text: str) -> tuple[list[Block], bool]:
    """Split text into id-marked blocks.

    Returns (blocks, has_any_marker). A block starts at its marker line and
    runs to the line before the next marker (or EOF). Text before the first
    marker is dropped — only id-marked content is tracked.
    """
    lines = text.splitlines(keepends=True)
    marker_lines: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _ID_RE.search(line)
        if m:
            marker_lines.append((i, m.group(1)))
    if not marker_lines:
        return [], False
    blocks: list[Block] = []
    for idx, (start, bid) in enumerate(marker_lines):
        end = marker_lines[idx + 1][0] if idx + 1 < len(marker_lines) else len(lines)
        body = "".join(lines[start:end]).rstrip("\n")
        blocks.append(Block(
            block_id=bid,
            content_hash=_hash(body),
            body=body,
            line_start=start,
        ))
    return blocks, True


def _walk_md(root: str) -> Iterator[str]:
    rp = Path(root)
    if rp.is_file() and rp.suffix == ".md":
        yield str(rp.resolve())
        return
    if not rp.is_dir():
        return
    for p in rp.rglob("*.md"):
        if p.is_file():
            yield str(p.resolve())


class MdIndex:
    """Concrete TombstoneStore backed by marrow.db md_index table.

    Connection is held for the life of the instance; pass an init_db()
    connection from the caller. Methods auto-commit each write.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record_block(self, path: str, block_id: str, content_hash: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO md_index (path, block_id, content_hash, last_seen_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(path, block_id) DO UPDATE SET"
                "   content_hash=excluded.content_hash,"
                "   last_seen_at=excluded.last_seen_at,"
                "   tombstone_at=NULL",
                (path, block_id, content_hash, _now_iso()),
            )

    def get_hash(self, path: str, block_id: str) -> str | None:
        r = self.conn.execute(
            "SELECT content_hash FROM md_index"
            " WHERE path=? AND block_id=? AND tombstone_at IS NULL",
            (path, block_id),
        ).fetchone()
        return r[0] if r else None

    def tombstone(self, path: str, block_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE md_index SET tombstone_at=? WHERE path=? AND block_id=?",
                (_now_iso(), path, block_id),
            )

    def clear_tombstone(self, path: str, block_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE md_index SET tombstone_at=NULL"
                " WHERE path=? AND block_id=?",
                (path, block_id),
            )

    def list_tombstones(self, path: str) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT block_id, tombstone_at FROM md_index"
            " WHERE path=? AND tombstone_at IS NOT NULL"
            " ORDER BY tombstone_at",
            (path,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def is_tombstoned(self, path: str, block_id: str) -> bool:
        # Helper for inserters — TombstoneStore Protocol unchanged; this is
        # MdIndex-only. Returns True when the row exists AND has tombstone_at.
        r = self.conn.execute(
            "SELECT 1 FROM md_index"
            " WHERE path=? AND block_id=? AND tombstone_at IS NOT NULL",
            (path, block_id),
        ).fetchone()
        return r is not None

    def sync_file(self, path: str, report: ReconcileReport | None = None) -> ReconcileReport:
        """Reconcile one md file with md_index. Read fs, diff, write.

        FULL semantic — overwrites `content_hash` for blocks whose body
        changed on disk. Used by `full_scan` + tests. **Watcher and
        `mw refresh` must NOT call this**: overwriting the baseline
        destroys the "last auto-write" signal the dashboard inserter
        compares user-edited bodies against. They call
        `sync_file_observe` instead.

        Insert: new block_id → record_block.
        Update: same block_id, hash changed → record_block (overwrites + clears tombstone).
        Tombstone: block_id in db but absent from fs → tombstone.
        Clear-tombstone happens automatically inside record_block.
        """
        report = report or ReconcileReport()
        p = Path(path)
        if not p.exists():
            # File deleted — tombstone every known block.
            for bid, _ in self._list_active(path):
                self.tombstone(path, bid)
                report.tombstoned += 1
            return report
        text = p.read_text(encoding="utf-8")
        blocks, has_markers = parse_blocks(text)
        report.scanned_files += 1
        if not has_markers:
            report.files_without_markers.append(path)
            # File exists but markers gone — tombstone any previously-known blocks.
            for bid, _ in self._list_active(path):
                self.tombstone(path, bid)
                report.tombstoned += 1
            return report
        seen_ids: set[str] = set()
        for blk in blocks:
            seen_ids.add(blk.block_id)
            prev = self._raw_row(path, blk.block_id)
            if prev is None:
                self.record_block(path, blk.block_id, blk.content_hash)
                report.inserted += 1
            else:
                prev_hash, prev_tomb = prev
                if prev_tomb is not None:
                    # Was tombstoned, now back — clear + record.
                    self.record_block(path, blk.block_id, blk.content_hash)
                    report.cleared += 1
                    if prev_hash != blk.content_hash:
                        report.updated += 1
                elif prev_hash != blk.content_hash:
                    self.record_block(path, blk.block_id, blk.content_hash)
                    report.updated += 1
        # Tombstone blocks absent from fs.
        for bid, _ in self._list_active(path):
            if bid not in seen_ids:
                self.tombstone(path, bid)
                report.tombstoned += 1
        return report

    def sync_file_observe(self, path: str,
                           report: ReconcileReport | None = None
                           ) -> ReconcileReport:
        """Observe-only sync — keep `content_hash` baseline frozen.

        Used by the watcher debounce + `mw refresh` scan phase. Semantic:
        - new block_id on fs → record_block (first-sight baseline).
        - existing block_id, body changed on fs → leave `content_hash`
          alone (this is the user-edit signal the dashboard inserter
          reads against to decide "preserve user body").
        - block_id in db, absent from fs → tombstone.
        - tombstoned block reappears on fs → record_block (fresh baseline)
          so the next inserter pass doesn't skip-as-tombstoned.

        Counters: `updated` still increments on detected drift even though
        no write happens — log lines benefit from the volume signal.
        """
        report = report or ReconcileReport()
        p = Path(path)
        if not p.exists():
            for bid, _ in self._list_active(path):
                self.tombstone(path, bid)
                report.tombstoned += 1
            return report
        text = p.read_text(encoding="utf-8")
        blocks, has_markers = parse_blocks(text)
        report.scanned_files += 1
        if not has_markers:
            report.files_without_markers.append(path)
            for bid, _ in self._list_active(path):
                self.tombstone(path, bid)
                report.tombstoned += 1
            return report
        seen_ids: set[str] = set()
        for blk in blocks:
            seen_ids.add(blk.block_id)
            prev = self._raw_row(path, blk.block_id)
            if prev is None:
                self.record_block(path, blk.block_id, blk.content_hash)
                report.inserted += 1
            else:
                prev_hash, prev_tomb = prev
                if prev_tomb is not None:
                    # Was tombstoned, now back — clear + record fresh baseline.
                    self.record_block(path, blk.block_id, blk.content_hash)
                    report.cleared += 1
                    if prev_hash != blk.content_hash:
                        report.updated += 1
                elif prev_hash != blk.content_hash:
                    # User edit detected. DO NOT overwrite baseline.
                    report.updated += 1
        for bid, _ in self._list_active(path):
            if bid not in seen_ids:
                self.tombstone(path, bid)
                report.tombstoned += 1
        return report

    def full_scan(self, roots: list[str], *, observe: bool = False
                  ) -> ReconcileReport:
        """Walk roots and reconcile each md file.

        observe=False (default): full sync — overwrites baseline hashes on
        body change. Used by watcher boot + tests.
        observe=True: observe-only — keep baseline frozen. Used by the
        `mw refresh` scan phase so user edits survive the next inserter
        pass.
        """
        report = ReconcileReport()
        seen_paths: set[str] = set()
        sync = self.sync_file_observe if observe else self.sync_file
        for root in roots:
            for md_path in _walk_md(root):
                seen_paths.add(md_path)
                sync(md_path, report)
        # Tombstone files known to md_index but no longer on disk under any root.
        # Only sweep paths whose prefix matches one of the roots — avoids nuking
        # historical rows from rotated/moved dirs.
        root_abs = [str(Path(r).resolve()) for r in roots]
        rows = self.conn.execute(
            "SELECT DISTINCT path FROM md_index WHERE tombstone_at IS NULL"
        ).fetchall()
        for (db_path,) in rows:
            if db_path in seen_paths:
                continue
            if not any(db_path == r or db_path.startswith(r + os.sep)
                       for r in root_abs):
                continue
            for bid, _ in self._list_active(db_path):
                self.tombstone(db_path, bid)
                report.tombstoned += 1
        return report

    def _list_active(self, path: str) -> Iterable[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT block_id, content_hash FROM md_index"
            " WHERE path=? AND tombstone_at IS NULL",
            (path,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def _raw_row(self, path: str, block_id: str) -> tuple[str, str | None] | None:
        r = self.conn.execute(
            "SELECT content_hash, tombstone_at FROM md_index"
            " WHERE path=? AND block_id=?",
            (path, block_id),
        ).fetchone()
        return (r[0], r[1]) if r else None
