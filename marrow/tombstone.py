"""TombstoneStore protocol + audit_log fallback impl.

Tombstones mark bullets a user deleted from handover.md. Next render filters
them out so sonnet's re-emission can never re-grow a Lumi-cleared bullet.

Two impls share the surface so wt-md-a can ship the md_index-backed store
without touching any caller (Phase F = `storage_for_tombstone()` binding flip).
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Protocol

from .handover_norm import hash_bullet, normalize_bullet


# Wt-md-a will implement this Protocol with the md_index.tombstones table.
class TombstoneStore(Protocol):
    def record_block(self, block_id: str, content_hash: str) -> None: ...
    def get_hash(self, block_id: str) -> str | None: ...
    def tombstone(self, content_hash: str, *, summary: str = "") -> None: ...
    def clear_tombstone(self, content_hash: str) -> None: ...
    def list_tombstones(self) -> set[str]: ...


class AuditLogTombstoneStore:
    """Placeholder store backed by audit_log rows. Drop-in replacement until
    wt-md-a ships the md_index impl. Per-block content_hash recording is a
    no-op here — only tombstone listing matters for the handover-render filter.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def record_block(self, block_id: str, content_hash: str) -> None:
        # md_index block tracking not modeled here. wt-md-a ships the real one.
        return None

    def get_hash(self, block_id: str) -> str | None:
        return None

    def tombstone(self, content_hash: str, *, summary: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('handover', ?, 'handover_tombstone', ?)",
                (content_hash, summary[:200]),
            )

    def clear_tombstone(self, content_hash: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM audit_log"
                " WHERE target_table='handover' AND action='handover_tombstone'"
                " AND target_id=?",
                (content_hash,),
            )

    def list_tombstones(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT target_id FROM audit_log"
            " WHERE target_table='handover' AND action='handover_tombstone'"
            " AND target_id IS NOT NULL"
        ).fetchall()
        return {r["target_id"] for r in rows if r["target_id"]}


class MdIndexTombstoneStore:
    """Thin adapter: bullet-level TombstoneStore Protocol over MdIndex.

    Binds a fixed md path (handover.md) so callers stay bullet-hash-keyed
    while persistence shares the md_index table used by dashboard / subpages.
    block_id == content_hash; content_hash arg == summary placeholder.
    """

    def __init__(self, conn: sqlite3.Connection, path: str):
        from .md_index import MdIndex
        self._idx = MdIndex(conn)
        self._path = path

    def record_block(self, block_id: str, content_hash: str) -> None:
        self._idx.record_block(self._path, block_id, content_hash)

    def get_hash(self, block_id: str) -> str | None:
        return self._idx.get_hash(self._path, block_id)

    def tombstone(self, content_hash: str, *, summary: str = "") -> None:
        # Record then tombstone so the row exists with the hash payload.
        self._idx.record_block(self._path, content_hash, summary[:200])
        self._idx.tombstone(self._path, content_hash)

    def clear_tombstone(self, content_hash: str) -> None:
        self._idx.clear_tombstone(self._path, content_hash)

    def list_tombstones(self) -> set[str]:
        return {bid for bid, _ts in self._idx.list_tombstones(self._path)}


# ── tombstone-aware bullet helpers (used by handover_render) ────────────────

def filter_tombstoned(bullets: Iterable[str], tombstones: set[str]) -> list[str]:
    """Drop bullets whose hash sits in the tombstone set."""
    return [ln for ln in bullets if hash_bullet(ln) not in tombstones]


def diff_user_removed(prior_text: str, current_text: str) -> list[str]:
    """Bullets present in prior but absent in current — these are user deletes."""
    from .handover_norm import bullet_lines
    cur_hashes = {hash_bullet(ln) for ln in bullet_lines(current_text)}
    return [ln for ln in bullet_lines(prior_text)
            if hash_bullet(ln) not in cur_hashes]


def record_user_deletes(store: TombstoneStore,
                        prior_text: str, current_text: str) -> int:
    """Walk prior → current diff, tombstone every dropped bullet. Return count."""
    n = 0
    for ln in diff_user_removed(prior_text, current_text):
        store.tombstone(hash_bullet(ln), summary=normalize_bullet(ln))
        n += 1
    return n
