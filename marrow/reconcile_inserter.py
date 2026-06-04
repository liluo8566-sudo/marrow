"""Generic md->DB reconcile for inserter-spec subpages.

reconcile_inserter_sync absorbs hand-edits from any InserterSpec-backed
md file back into the DB. It also refactors the legacy reconcile_memes /
reconcile_profile into thin wrappers.

Public surface (imported by subpages.py):
  reconcile_inserter_sync — generic helper (UPDATE + DELETE)
  reconcile_memes         — memes wrapper (retains public name)
  reconcile_profile       — profile wrapper (retains public name)
  reconcile_diary         — diary wrapper
  reconcile_stickers      — stickers wrapper
  reconcile_wallet        — wallet wrapper
  reconcile_goose         — goose wrapper
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from .inserter import InserterSpec
from .reconcile import ReconcileReport

_ANCHOR_RE = re.compile(r"<!-- id:(\d+) -->")
_ANCHOR_STR_RE = re.compile(r"<!-- id:([^>]+?) -->")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _scan_anchored_ids(md_text: str) -> set[int]:
    return {int(m.group(1)) for m in _ANCHOR_RE.finditer(md_text)}


def _audit(conn: sqlite3.Connection, table: str, row_id: str,
           action: str, summary: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (target_table, target_id, action, summary)"
        " VALUES (?, ?, ?, ?)",
        (table, row_id, action, summary),
    )


def reconcile_inserter_sync(
    conn: sqlite3.Connection,
    spec: InserterSpec,
    md_path: Path,
    table: str,
    *,
    editable_cols: list[str],
    soft_delete: bool = False,
    block_id_col: str = "id",
) -> ReconcileReport:
    """Generic md->DB sync for any InserterSpec-backed subpage.

    Pass 1 (UPDATE): for each `<!-- id:N -->` line, call spec.parse_row;
    compare parsed editable_cols against DB row; UPDATE on diff.
    Pass 2 (DELETE/soft-delete): DB ids absent from md → remove.

    Guards:
    - md absent or no anchors → return empty rpt (never wipe table).
    - spec.parse_row is None → skip UPDATE pass (only delete-by-absence).
    """
    rpt = ReconcileReport()
    md_path = Path(md_path)
    if not md_path.exists():
        return rpt
    md_text = md_path.read_text(encoding="utf-8")
    md_ids = _scan_anchored_ids(md_text)
    if not md_ids:
        return rpt  # empty-file guard

    # ── pass 1: UPDATE editable fields ────────────────────────────────────
    if spec.parse_row is not None:
        # Build per-id lookup of md lines that carry an anchor.
        id_to_line: dict[int, str] = {}
        for line in md_text.splitlines():
            m = _ANCHOR_RE.search(line)
            if m:
                try:
                    id_to_line[int(m.group(1))] = line
                except ValueError:
                    pass

        if id_to_line:
            placeholders = ",".join("?" for _ in id_to_line)
            col_list = ", ".join([block_id_col] + editable_cols)
            db_rows: dict[int, dict] = {}
            try:
                for row in conn.execute(
                    f"SELECT {col_list} FROM {table}"
                    f" WHERE {block_id_col} IN ({placeholders})",
                    list(id_to_line.keys()),
                ).fetchall():
                    db_rows[row[block_id_col]] = dict(row)
            except sqlite3.Error:
                pass  # table may not exist yet (fresh install)

            updates: list[tuple[int, dict]] = []
            for rid, line in id_to_line.items():
                if rid not in db_rows:
                    continue
                try:
                    parsed = spec.parse_row(line)
                except Exception:
                    continue
                if parsed is None:
                    continue
                db_row = db_rows[rid]
                changes: dict[str, object] = {}
                for col in editable_cols:
                    md_val = parsed.get(col) or None
                    db_val = db_row.get(col) or None
                    if md_val != db_val:
                        changes[col] = md_val
                if changes:
                    updates.append((rid, changes))

            if updates:
                with conn:
                    for rid, changes in updates:
                        set_clause = ", ".join(f"{c}=?" for c in changes)
                        vals = list(changes.values()) + [_now(), rid]
                        try:
                            conn.execute(
                                f"UPDATE {table} SET {set_clause},"
                                f" updated_at=? WHERE {block_id_col}=?",
                                vals,
                            )
                        except sqlite3.OperationalError:
                            # Table has no updated_at (e.g. diary uses date PK)
                            set_clause2 = ", ".join(f"{c}=?" for c in changes)
                            vals2 = list(changes.values()) + [rid]
                            conn.execute(
                                f"UPDATE {table} SET {set_clause2}"
                                f" WHERE {block_id_col}=?",
                                vals2,
                            )
                        summary = "md-reconcile: " + ", ".join(
                            f"{c}={str(v)[:40]}" for c, v in changes.items()
                        )
                        _audit(conn, table, str(rid), "update", summary)
                        rpt.updated += 1

    # ── pass 2: DELETE rows absent from md ────────────────────────────────
    try:
        if soft_delete:
            db_active = {
                r[0] for r in conn.execute(
                    f"SELECT {block_id_col} FROM {table}"
                    f" WHERE superseded_by IS NULL"
                ).fetchall()
            }
        else:
            db_active = {
                r[0] for r in conn.execute(
                    f"SELECT {block_id_col} FROM {table}"
                ).fetchall()
            }
    except sqlite3.Error:
        return rpt

    to_remove = db_active - md_ids
    if not to_remove:
        return rpt

    with conn:
        for rid in to_remove:
            if soft_delete:
                conn.execute(
                    f"UPDATE {table} SET superseded_by=?"
                    f" WHERE {block_id_col}=?",
                    (rid, rid),
                )
                _audit(conn, table, str(rid), "soft_delete",
                       "md-reconcile: removed from md")
            else:
                conn.execute(
                    f"DELETE FROM {table} WHERE {block_id_col}=?", (rid,)
                )
                _audit(conn, table, str(rid), "delete",
                       "md-reconcile: removed from md")
            rpt.deleted += 1

    return rpt


# ── thin wrappers ─────────────────────────────────────────────────────────────

def reconcile_memes(conn: sqlite3.Connection, md_path: Path) -> ReconcileReport:
    """Delete memes rows absent from md; UPDATE editable fields on edit."""
    from . import subpage_specs
    spec = subpage_specs.build_memes_spec(str(Path(md_path).parent))
    return reconcile_inserter_sync(
        conn, spec, md_path, "memes",
        editable_cols=["type", "key", "value", "context"],
        soft_delete=False,
    )


def reconcile_profile(conn: sqlite3.Connection,
                      md_path: Path) -> ReconcileReport:
    """Soft-delete entity rows absent from md; UPDATE editable fields."""
    from . import subpage_specs
    spec = subpage_specs.build_profile_spec(str(Path(md_path).parent))
    return reconcile_inserter_sync(
        conn, spec, md_path, "entities",
        editable_cols=["name", "kind", "fact"],
        soft_delete=True,
    )


def reconcile_diary(conn: sqlite3.Connection,
                    md_path: Path) -> ReconcileReport:
    """UPDATE diary.content on edit; DELETE date rows absent from md.

    Diary blocks are multi-line (#### heading / anchor / body), so we use
    a block-level scanner rather than the single-line reconcile_inserter_sync.
    The anchor line is `<!-- id:YYYY-MM-DD -->` (date string, not numeric id).
    """
    rpt = ReconcileReport()
    md_path = Path(md_path)
    if not md_path.exists():
        return rpt
    md_text = md_path.read_text(encoding="utf-8")

    # Collect all date anchors present in md.
    _DATE_ANCHOR_RE = re.compile(r"<!-- id:(\d{4}-\d{2}-\d{2}) -->")
    md_dates = {m.group(1) for m in _DATE_ANCHOR_RE.finditer(md_text)}
    if not md_dates:
        return rpt  # empty-file guard

    # Parse blocks: #### YYYY-MM-DD[...] heading signals a new entry.
    # Body runs from the line after the anchor until the next #### or ## or EOF.
    _H4_RE = re.compile(r"^#### (?P<date>\d{4}-\d{2}-\d{2})")
    blocks: dict[str, str] = {}  # date -> content body
    cur_date: str | None = None
    body_lines: list[str] = []
    anchor_seen = False

    def _flush():
        if cur_date is not None:
            body = "\n".join(body_lines).strip()
            blocks[cur_date] = body

    for line in md_text.splitlines():
        # Stop collecting at any marrow marker line.
        if "<!-- marrow:" in line:
            _flush()
            cur_date = None
            continue
        h4 = _H4_RE.match(line)
        if h4:
            _flush()
            cur_date = h4.group("date")
            body_lines = []
            anchor_seen = False
            continue
        if cur_date is None:
            continue
        if _DATE_ANCHOR_RE.search(line):
            anchor_seen = True
            continue  # skip the anchor line itself
        if anchor_seen:
            body_lines.append(line)
    _flush()

    # Load DB rows for dates present in md.
    db_rows: dict[str, str] = {}
    if md_dates:
        placeholders = ",".join("?" for _ in md_dates)
        for row in conn.execute(
            f"SELECT date, content FROM diary WHERE date IN ({placeholders})",
            list(md_dates),
        ).fetchall():
            db_rows[row["date"]] = row["content"] or ""

    updates: list[tuple[str, str]] = []
    for date, md_content in blocks.items():
        if date not in db_rows:
            continue
        if md_content != (db_rows[date] or "").strip():
            updates.append((date, md_content))

    if updates:
        with conn:
            for date, content in updates:
                conn.execute(
                    "UPDATE diary SET content=?, updated_at=? WHERE date=?",
                    (content, _now(), date),
                )
                _audit(conn, "diary", date, "update",
                       f"md-reconcile: content edit {date}")
                rpt.updated += 1

    # Delete pass: DB dates absent from md.
    try:
        all_db_dates = {
            r[0] for r in conn.execute("SELECT date FROM diary").fetchall()
        }
    except sqlite3.Error:
        return rpt

    to_delete = all_db_dates - md_dates
    if to_delete:
        with conn:
            for date in to_delete:
                conn.execute("DELETE FROM diary WHERE date=?", (date,))
                _audit(conn, "diary", date, "delete",
                       "md-reconcile: removed from md")
                rpt.deleted += 1

    return rpt


def reconcile_stickers(conn: sqlite3.Connection,
                       md_path: Path) -> ReconcileReport:
    """UPDATE sticker key/asset_path/mime_type on edit; DELETE absent rows."""
    from . import subpage_specs
    spec = subpage_specs.build_stickers_spec(str(Path(md_path).parent))
    return reconcile_inserter_sync(
        conn, spec, md_path, "stickers",
        editable_cols=["key", "asset_path", "mime_type"],
        soft_delete=False,
    )


def reconcile_wallet(conn: sqlite3.Connection,
                     md_path: Path) -> ReconcileReport:
    """UPDATE wallet summary on edit; DELETE absent rows."""
    from . import subpage_specs
    spec = subpage_specs.build_wallet_spec(str(Path(md_path).parent))
    return reconcile_inserter_sync(
        conn, spec, md_path, "wallet",
        editable_cols=["summary"],
        soft_delete=False,
    )


def reconcile_goose(conn: sqlite3.Connection,
                    md_path: Path) -> ReconcileReport:
    """UPDATE goose_bites.bites on edit; DELETE absent rows."""
    from . import subpage_specs
    spec = subpage_specs.build_goose_spec(str(Path(md_path).parent))
    return reconcile_inserter_sync(
        conn, spec, md_path, "goose_bites",
        editable_cols=["bites"],
        soft_delete=False,
    )
