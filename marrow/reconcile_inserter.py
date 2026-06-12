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
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from ._atomic import atomic_write as _atomic_write
from .inserter import InserterSpec
from .reconcile import ReconcileReport

_ANCHOR_RE = re.compile(r"<!-- id:(\d+) -->")
_ANCHOR_STR_RE = re.compile(r"<!-- id:([^>]+?) -->")
# Section heading: ## Word  (profile uses ## Person / ## Pref / ## Place)
_SECTION_H2_RE = re.compile(r"^##\s+(?P<label>\S.*?)\s*$")

# Unanchored row patterns for INSERT pass (anchor optional/absent).
# Memes: `- [type] **key**{ → value}{ _context_}`
_MEME_UNANCHORED_RE = re.compile(
    r"^-\s+\[(?P<type>[^\]]+)\]\s+\*\*(?P<key>[^*]+)\*\*"
    r"(?:\s+→\s+(?P<value>.+?))?"
    r"(?:\s+_(?P<context>[^_]+)_)?"
    r"\s*$"
)
# Profile: `- [kind] **name**{ — fact}`
_PROFILE_UNANCHORED_RE = re.compile(
    r"^-\s+\[(?P<kind>[^\]]+)\]\s+\*\*(?P<name>[^*]+)\*\*"
    r"(?:\s+—\s+(?P<fact>.+?))?"
    r"\s*$"
)


def _parse_bare_anchored(line: str, bare_cols: tuple[str, str]) -> dict | None:
    """Parse an anchored bare row (`- text <!-- id:N -->`) for the UPDATE pass.

    Bare rows inserted by the bare-text path keep their hand-typed shape (the
    inserter never rewrites existing blocks), so full-shape parse_row returns
    None on them forever. `text` → primary col; ` → ` / ` — ` split feeds the
    secondary col. Cols not returned are left untouched by the caller.
    Returns None for non-bullet lines or mangled full-shape rows (`[` lead).
    """
    body = _ANCHOR_STR_RE.sub("", line).strip()
    if not body.startswith("- "):
        return None
    text = body[2:].strip()
    if not text or text.startswith("["):
        return None
    primary, secondary = bare_cols
    for sep in (" → ", " — "):
        if sep in text:
            head, _, tail = text.partition(sep)
            if head.strip() and tail.strip():
                return {primary: head.strip(), secondary: tail.strip()}
    return {primary: text}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _scan_anchored_ids(md_text: str) -> set[int]:
    return {int(m.group(1)) for m in _ANCHOR_RE.finditer(md_text)}


def _marker_bounds(md_lines: list[str], key: str) -> tuple[int, int]:
    """Line-index window inside `<!-- marrow:<key>:start/end -->` markers.

    Missing start → 0; missing end → len(md_lines) (legacy / test fixtures).
    Scanners must ignore lines outside — stray content beyond the end marker
    must never reconcile (0613: a duplicate date block after the end marker
    parsed as empty body and wiped the real row's content).
    """
    lo, hi = 0, len(md_lines)
    for i, line in enumerate(md_lines):
        if f"<!-- marrow:{key}:start -->" in line:
            lo = i + 1
        elif f"<!-- marrow:{key}:end -->" in line:
            hi = i
            break
    return lo, hi


def _audit(conn: sqlite3.Connection, table: str, row_id: str,
           action: str, summary: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (target_table, target_id, action, summary)"
        " VALUES (?, ?, ?, ?)",
        (table, row_id, action, summary),
    )




def _insert_diary_blocks(
    conn: sqlite3.Connection,
    md_path: Path,
    rpt: ReconcileReport,
) -> None:
    """INSERT pass for diary block-shaped subpage.

    A `#### YYYY-MM-DD` heading block with no `<!-- id:YYYY-MM-DD -->`
    anchor line → INSERT diary(date, content), then write the anchor
    line back under the heading.

    Guards: future-dated or malformed date → conflict, skip.
    Idempotent: if the date is already in DB, skip silently (dedup).
    """
    import datetime

    _DATE_ANCHOR_RE = re.compile(r"<!-- id:(\d{4}-\d{2}-\d{2}) -->")
    _H4_DATE_RE = re.compile(r"^#### (?P<date>\d{4}-\d{2}-\d{2})(?P<rest>.*)?$")
    _MARROW_RE = re.compile(r"<!-- marrow:")

    if not md_path.exists():
        return
    md_text = md_path.read_text(encoding="utf-8")
    md_lines = md_text.splitlines()

    today_str = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    # Scan for heading blocks that lack an anchor.
    # Each block: heading_line_idx, date, body lines, has_anchor flag.
    blocks: list[tuple[int, str, list[str]]] = []  # (heading_idx, date, body_lines)
    cur_heading_idx: int | None = None
    cur_date: str | None = None
    cur_body: list[str] = []
    cur_has_anchor = False

    def _flush_block():
        nonlocal cur_heading_idx, cur_date, cur_body, cur_has_anchor
        if cur_heading_idx is not None and cur_date is not None and not cur_has_anchor:
            blocks.append((cur_heading_idx, cur_date, list(cur_body)))
        cur_heading_idx = None
        cur_date = None
        cur_body = []
        cur_has_anchor = False

    lo, hi = _marker_bounds(md_lines, "diary")
    for idx in range(lo, hi):
        line = md_lines[idx]
        if _MARROW_RE.search(line):
            _flush_block()
            continue
        h4 = _H4_DATE_RE.match(line)
        if h4:
            _flush_block()
            cur_heading_idx = idx
            cur_date = h4.group("date")
            cur_body = []
            cur_has_anchor = False
            continue
        if cur_heading_idx is None:
            continue
        if _DATE_ANCHOR_RE.search(line):
            cur_has_anchor = True
            continue
        cur_body.append(line)
    _flush_block()

    if not blocks:
        return

    # For each candidate block: validate date, dedup, insert, queue anchor.
    anchor_inserts: list[tuple[int, str]] = []  # (heading_idx, date)

    with conn:
        for heading_idx, date, body_lines in blocks:
            # Validate date format and not future.
            try:
                d = datetime.date.fromisoformat(date)
            except ValueError:
                rpt.conflicts.append(f"diary insert: malformed date '{date}'")
                continue
            if date > today_str:
                rpt.conflicts.append(f"diary insert: future date '{date}'")
                continue

            # Dedup: date already in DB → skip.
            existing = conn.execute(
                "SELECT date FROM diary WHERE date=?", (date,)
            ).fetchone()
            if existing is not None:
                continue

            content = "\n".join(body_lines).strip()
            conn.execute(
                "INSERT INTO diary(date, content) VALUES(?, ?)",
                (date, content),
            )
            _audit(conn, "diary", date, "insert",
                   f"md-reconcile: new diary entry {date}")
            rpt.inserted += 1
            anchor_inserts.append((heading_idx, date))

    if not anchor_inserts:
        return

    # Write anchor lines back. Insert `<!-- id:YYYY-MM-DD -->` as the
    # line immediately after the heading (matching render output).
    offset = 0
    for heading_idx, date in anchor_inserts:
        insert_at = heading_idx + 1 + offset
        md_lines.insert(insert_at, f"<!-- id:{date} -->")
        offset += 1

    trailing_nl = "\n" if md_text.endswith("\n") else ""
    _atomic_write(str(md_path), "\n".join(md_lines) + trailing_nl)


def reconcile_inserter_sync(
    conn: sqlite3.Connection,
    spec: InserterSpec,
    md_path: Path,
    table: str,
    *,
    editable_cols: list[str],
    soft_delete: bool = False,
    block_id_col: str = "id",
    bare_cols: tuple[str, str] | None = None,
) -> ReconcileReport:
    """Generic md->DB sync for any InserterSpec-backed subpage.

    Pass 1 (UPDATE): for each `<!-- id:N -->` line, call spec.parse_row;
    compare parsed editable_cols against DB row; UPDATE on diff.
    Pass 2 (DELETE/soft-delete): DB ids absent from md → remove. Rows whose
    `updated_at` post-dates the md snapshot are spared — they were written
    after md was last rendered (e.g. daily.py insert in the same refresh
    pass) and absence from md is expected, not a user deletion. Mirrors the
    md-mtime gating used in reconcile_alerts / reconcile_affect.

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

    md_mtime_iso: str | None = None
    try:
        md_mtime_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(md_path.stat().st_mtime)
        )
    except OSError:
        pass

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
                if parsed is None and bare_cols is not None:
                    # Bare hand-typed row that gained an anchor on insert —
                    # keep its in-line edits syncing even without full shape.
                    parsed = _parse_bare_anchored(line, bare_cols)
                if parsed is None:
                    continue
                db_row = db_rows[rid]
                changes: dict[str, object] = {}
                for col in editable_cols:
                    if col not in parsed:
                        continue  # bare fallback edits only the cols it parsed
                    md_val = parsed.get(col) or None
                    db_val = db_row.get(col) or None
                    if md_val != db_val:
                        changes[col] = md_val
                if changes:
                    updates.append((rid, changes))

            if updates:
                # Pre-check updated_at column existence — try/except after a
                # failed execute leaves sqlite cursor mid-abort and the
                # fallback statement errors with "SQL logic error".
                try:
                    _cols = {
                        r[1] for r in conn.execute(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                    }
                except sqlite3.Error:
                    _cols = set()
                has_updated_at = "updated_at" in _cols
                with conn:
                    for rid, changes in updates:
                        set_clause = ", ".join(f"{c}=?" for c in changes)
                        if has_updated_at:
                            sql = (
                                f"UPDATE {table} SET {set_clause},"
                                f" updated_at=? WHERE {block_id_col}=?"
                            )
                            vals = list(changes.values()) + [_now(), rid]
                        else:
                            sql = (
                                f"UPDATE {table} SET {set_clause}"
                                f" WHERE {block_id_col}=?"
                            )
                            vals = list(changes.values()) + [rid]
                        conn.execute(sql, vals)
                        summary = "md-reconcile: " + ", ".join(
                            f"{c}={str(v)[:40]}" for c, v in changes.items()
                        )
                        _audit(conn, table, str(rid), "update", summary)
                        rpt.updated += 1

    # ── pass 2: DELETE rows absent from md ────────────────────────────────
    # md-mtime gate: rows whose updated_at (or created_at if no updated_at)
    # post-dates the md snapshot were inserted after the file was last
    # rendered (typical: daily.py insert then write_all_subpages in one
    # pass). Skip them — inserter will write them to md on the same refresh
    # round. Mirrors reconcile_alerts / reconcile_affect mtime gating.
    try:
        cols = {
            r[1] for r in conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
    except sqlite3.Error:
        cols = set()
    gate_col = "updated_at" if "updated_at" in cols else (
        "created_at" if "created_at" in cols else None
    )
    gate_sql = ""
    gate_params: list = []
    if md_mtime_iso and gate_col:
        gate_sql = f" AND ({gate_col} IS NULL OR {gate_col} <= ?)"
        gate_params = [md_mtime_iso]

    try:
        if soft_delete:
            db_active = {
                r[0] for r in conn.execute(
                    f"SELECT {block_id_col} FROM {table}"
                    f" WHERE superseded_by IS NULL" + gate_sql,
                    gate_params,
                ).fetchall()
            }
        else:
            db_active = {
                r[0] for r in conn.execute(
                    f"SELECT {block_id_col} FROM {table}"
                    + (f" WHERE 1=1{gate_sql}" if gate_sql else ""),
                    gate_params,
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


# ── concrete insert helpers ───────────────────────────────────────────────────

def _insert_memes(
    conn: sqlite3.Connection,
    spec: InserterSpec,
    md_path: Path,
    rpt: ReconcileReport,
) -> None:
    """INSERT pass for memes.md.

    Unanchored lines matching the meme row format are inserted into DB.
    type is read directly from the line's [type] bracket (already in
    parse_row output). Section heading ("Personal" / "Public") is used
    only to validate consistency:
      Personal → type must be in _MEME_PERSONAL (paw, fact)
      Public   → type must NOT be in _MEME_PERSONAL
    If inconsistent, add to conflicts and skip.
    Dedup: same type+key with status='active' already present → skip.
    Hand-typed rows get pinned=1, status='active'.
    """
    from .subpage_specs import _MEME_PERSONAL
    _personal_types: frozenset[str] = frozenset(_MEME_PERSONAL)
    _valid_sections = {"Personal", "Public"}

    if not md_path.exists():
        return
    md_text = md_path.read_text(encoding="utf-8")
    md_lines = md_text.splitlines()

    try:
        table_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(memes)").fetchall()
        }
    except sqlite3.Error:
        return

    candidates: list[tuple[int, dict]] = []
    cur_section: str | None = None

    _lo, _hi = _marker_bounds(md_lines, "memes")
    for idx in range(_lo, _hi):
        line = md_lines[idx]
        stripped = line.strip()
        if not stripped:
            continue
        # Track section headings (## Personal / ## Public).
        m_sec = _SECTION_H2_RE.match(stripped)
        if m_sec:
            cur_section = m_sec.group("label")
            continue
        # Skip heading lines, marrow markers, quote lines.
        if stripped.startswith("#") or stripped.startswith("<!-- ") or stripped.startswith(">"):
            continue
        # Skip already-anchored lines.
        if _ANCHOR_RE.search(line):
            continue
        # Use unanchored regex — spec.parse_row requires the anchor and returns
        # None for bare hand-typed lines.
        m = _MEME_UNANCHORED_RE.match(stripped)
        if not m:
            # Bare hand-typed bullet: `- some text`. Full row shape is the
            # renderer's job — the whole text becomes the meme key, type
            # defaults from the section.
            if not stripped.startswith("- "):
                continue
            bare = stripped[2:].strip()
            if not bare:
                continue
            if cur_section not in _valid_sections:
                rpt.conflicts.append(
                    f"memes insert: unmappable section '{cur_section}' — {line[:60]}"
                )
                continue
            default_type = "fact" if cur_section == "Personal" else "others"
            candidates.append((idx, {
                "type": default_type,
                "key": bare,
                "value": None,
                "context": None,
            }))
            continue
        parsed = {
            "type": m.group("type").strip(),
            "key": m.group("key").strip(),
            "value": (m.group("value") or "").strip() or None,
            "context": (m.group("context") or "").strip() or None,
        }
        meme_type = (parsed.get("type") or "").strip()
        if not meme_type:
            rpt.conflicts.append(f"memes insert: missing type — {line[:60]}")
            continue
        # Section consistency check.
        if cur_section not in _valid_sections:
            rpt.conflicts.append(
                f"memes insert: unmappable section '{cur_section}' — {line[:60]}"
            )
            continue
        is_personal = meme_type in _personal_types
        if cur_section == "Personal" and not is_personal:
            rpt.conflicts.append(
                f"memes insert: type '{meme_type}' under Personal section — {line[:60]}"
            )
            continue
        if cur_section == "Public" and is_personal:
            rpt.conflicts.append(
                f"memes insert: personal type '{meme_type}' under Public section — {line[:60]}"
            )
            continue
        candidates.append((idx, parsed))

    if not candidates:
        return

    anchor_writes: list[tuple[int, int]] = []
    with conn:
        for idx, parsed in candidates:
            meme_type = parsed["type"]
            meme_key = parsed.get("key") or ""
            # Dedup: type+key active.
            existing = conn.execute(
                "SELECT id FROM memes WHERE type=? AND key=? AND status='active'",
                (meme_type, meme_key),
            ).fetchone()
            if existing is not None:
                continue
            insert_cols: dict[str, object] = {
                "type": meme_type,
                "key": meme_key,
                "value": parsed.get("value"),
                "context": parsed.get("context"),
                "pinned": 1,
                "status": "active",
            }
            valid = {k: v for k, v in insert_cols.items() if k in table_cols}
            col_names = ", ".join(valid.keys())
            placeholders = ", ".join("?" for _ in valid)
            cur = conn.execute(
                f"INSERT INTO memes ({col_names}) VALUES ({placeholders})",
                list(valid.values()),
            )
            new_id = cur.lastrowid
            _audit(conn, "memes", str(new_id), "insert",
                   f"md-reconcile: {meme_key[:60]}")
            rpt.inserted += 1
            anchor_writes.append((idx, new_id))

    if not anchor_writes:
        return
    changed = False
    for idx, new_id in anchor_writes:
        if _ANCHOR_RE.search(md_lines[idx]):
            continue
        md_lines[idx] = md_lines[idx].rstrip() + f" <!-- id:{new_id} -->"
        changed = True
    if changed:
        trailing_nl = "\n" if md_text.endswith("\n") else ""
        _atomic_write(str(md_path), "\n".join(md_lines) + trailing_nl)


def _insert_profile(
    conn: sqlite3.Connection,
    spec: InserterSpec,
    md_path: Path,
    rpt: ReconcileReport,
) -> None:
    """INSERT pass for profile.md.

    Unanchored lines matching the entity row format are inserted into DB.
    kind is derived from the section heading: `## Person` → kind='person',
    `## Pref` → kind='pref', `## Place` → kind='place'
    (render_section_header uses k.capitalize() so heading is capitalised).
    Dedup: same kind+name not superseded → skip silently.
    """
    # Heading label → kind (reverse of k.capitalize() used by renderer).
    _section_to_kind: dict[str, str] = {
        "Person": "person",
        "Pref": "pref",
        "Preference": "pref",
        "Place": "place",
    }

    if not md_path.exists():
        return
    md_text = md_path.read_text(encoding="utf-8")
    md_lines = md_text.splitlines()

    try:
        table_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(entities)").fetchall()
        }
    except sqlite3.Error:
        return

    candidates: list[tuple[int, dict, str]] = []  # (idx, parsed, kind)
    cur_section: str | None = None

    _lo, _hi = _marker_bounds(md_lines, "profile")
    for idx in range(_lo, _hi):
        line = md_lines[idx]
        stripped = line.strip()
        if not stripped:
            continue
        m_sec = _SECTION_H2_RE.match(stripped)
        if m_sec:
            cur_section = m_sec.group("label")
            continue
        if stripped.startswith("#") or stripped.startswith("<!-- ") or stripped.startswith(">"):
            continue
        if _ANCHOR_RE.search(line):
            continue
        # Use unanchored regex — spec.parse_row requires the anchor.
        m = _PROFILE_UNANCHORED_RE.match(stripped)
        if m:
            parsed = {
                "kind": m.group("kind").strip(),
                "name": m.group("name").strip(),
                "fact": (m.group("fact") or "").strip() or None,
            }
        elif stripped.startswith("- "):
            # Bare hand-typed bullet: `- name — fact` or `- name`.
            bare = stripped[2:].strip()
            if not bare:
                continue
            if " — " in bare:
                bname, _, bfact = bare.partition(" — ")
                parsed = {"kind": "", "name": bname.strip(),
                          "fact": bfact.strip() or None}
            else:
                parsed = {"kind": "", "name": bare, "fact": None}
        else:
            continue
        kind = _section_to_kind.get(cur_section or "")
        if kind is None:
            rpt.conflicts.append(
                f"profile insert: unmappable section '{cur_section}' — {line[:60]}"
            )
            continue
        candidates.append((idx, parsed, kind))

    if not candidates:
        return

    anchor_writes: list[tuple[int, int]] = []
    with conn:
        for idx, parsed, kind in candidates:
            name = (parsed.get("name") or "").strip()
            # Dedup: kind+name not superseded.
            existing = conn.execute(
                "SELECT id FROM entities WHERE kind=? AND name=? AND superseded_by IS NULL",
                (kind, name),
            ).fetchone()
            if existing is not None:
                continue
            insert_cols: dict[str, object] = {
                "kind": kind,
                "name": name,
                "fact": parsed.get("fact"),
            }
            valid = {k: v for k, v in insert_cols.items() if k in table_cols}
            col_names = ", ".join(valid.keys())
            placeholders = ", ".join("?" for _ in valid)
            cur = conn.execute(
                f"INSERT INTO entities ({col_names}) VALUES ({placeholders})",
                list(valid.values()),
            )
            new_id = cur.lastrowid
            _audit(conn, "entities", str(new_id), "insert",
                   f"md-reconcile: {name[:60]}")
            rpt.inserted += 1
            anchor_writes.append((idx, new_id))

    if not anchor_writes:
        return
    changed = False
    for idx, new_id in anchor_writes:
        if _ANCHOR_RE.search(md_lines[idx]):
            continue
        md_lines[idx] = md_lines[idx].rstrip() + f" <!-- id:{new_id} -->"
        changed = True
    if changed:
        trailing_nl = "\n" if md_text.endswith("\n") else ""
        _atomic_write(str(md_path), "\n".join(md_lines) + trailing_nl)


# ── thin wrappers ─────────────────────────────────────────────────────────────

def reconcile_memes(conn: sqlite3.Connection, md_path: Path) -> ReconcileReport:
    """INSERT unanchored rows; UPDATE editable fields; DELETE absent rows."""
    from . import subpage_specs
    spec = subpage_specs.build_memes_spec(str(Path(md_path).parent))
    rpt = reconcile_inserter_sync(
        conn, spec, md_path, "memes",
        editable_cols=["type", "key", "value", "context"],
        bare_cols=("key", "value"),
        soft_delete=False,
    )
    _insert_memes(conn, spec, Path(md_path), rpt)
    return rpt


def reconcile_profile(conn: sqlite3.Connection,
                      md_path: Path) -> ReconcileReport:
    """INSERT unanchored rows; UPDATE editable fields; soft-delete absent rows."""
    from . import subpage_specs
    spec = subpage_specs.build_profile_spec(str(Path(md_path).parent))
    rpt = reconcile_inserter_sync(
        conn, spec, md_path, "entities",
        editable_cols=["name", "kind", "fact"],
        bare_cols=("name", "fact"),
        soft_delete=True,
    )
    _insert_profile(conn, spec, Path(md_path), rpt)
    return rpt


def reconcile_diary(conn: sqlite3.Connection,
                    md_path: Path) -> ReconcileReport:
    """UPDATE diary.content on edit; DELETE date rows absent from md.

    Diary blocks are multi-line (#### heading / anchor / body), so we use
    a block-level scanner rather than the single-line reconcile_inserter_sync.
    The anchor line is `<!-- id:YYYY-MM-DD -->` (date string, not numeric id).

    DELETE pass gates by md mtime: rows whose `updated_at` post-dates the
    md snapshot are spared (daily.py writes a fresh diary row immediately
    before write_all_subpages — the inserter renders it on the same pass).
    """
    rpt = ReconcileReport()
    md_path = Path(md_path)
    if not md_path.exists():
        return rpt
    md_text = md_path.read_text(encoding="utf-8")

    md_mtime_iso: str | None = None
    try:
        md_mtime_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(md_path.stat().st_mtime)
        )
    except OSError:
        pass

    # INSERT pass: run before the anchor-guard so a brand-new file with no
    # existing anchors can still receive inserts.
    _insert_diary_blocks(conn, md_path, rpt)

    # Reload md_text after potential anchor write-back. Scan only inside the
    # marrow markers — stray blocks beyond them must never reconcile.
    md_text = md_path.read_text(encoding="utf-8")
    _all_lines = md_text.splitlines()
    _lo, _hi = _marker_bounds(_all_lines, "diary")
    bounded_text = "\n".join(_all_lines[_lo:_hi])

    # Collect all date anchors present in md (bounded).
    _DATE_ANCHOR_RE = re.compile(r"<!-- id:(\d{4}-\d{2}-\d{2}) -->")
    md_dates = {m.group(1) for m in _DATE_ANCHOR_RE.finditer(bounded_text)}
    if not md_dates:
        return rpt  # empty-file guard (no anchors even after insert pass)

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
            if cur_date in blocks:
                # Duplicate date block — keep the first, never overwrite
                # (an empty-bodied duplicate would wipe the real content).
                rpt.conflicts.append(f"diary: duplicate block #### {cur_date}")
            else:
                blocks[cur_date] = body

    for line in bounded_text.splitlines():
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

    # Delete pass: DB dates absent from md. md-mtime gate spares rows
    # written after the md snapshot (daily.py same-pass insert).
    try:
        if md_mtime_iso:
            all_db_dates = {
                r[0] for r in conn.execute(
                    "SELECT date FROM diary"
                    " WHERE updated_at IS NULL OR updated_at <= ?",
                    (md_mtime_iso,),
                ).fetchall()
            }
        else:
            all_db_dates = {
                r[0] for r in conn.execute(
                    "SELECT date FROM diary"
                ).fetchall()
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
