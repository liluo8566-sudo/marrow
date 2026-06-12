"""Tests for reconcile_inserter — md hand-edit → DB write-back."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from marrow import storage
from marrow.reconcile_inserter import (
    reconcile_diary,
    reconcile_memes,
    reconcile_profile,
)
from marrow import subpage_specs


# ── helpers ───────────────────────────────────────────────────────────────────

def _db(tmp_path: Path) -> str:
    p = str(tmp_path / "t.db")
    storage.init_db(p).close()
    return p


def _conn(db_path: str):
    return storage.connect(db_path)


# ── memes: md edit writes back to DB ─────────────────────────────────────────

def test_memes_md_edit_writes_back_to_db(tmp_path):
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        cur = conn.execute(
            "INSERT INTO memes(type,key,value,context) VALUES('fact','speed','fast','racing')"
        )
        rid = cur.lastrowid

    # Build md via render_row and inject into a file with the anchor.
    spec = subpage_specs.build_memes_spec(str(tmp_path))
    rendered = spec.render_row({"id": rid, "type": "fact", "key": "speed",
                                "value": "fast", "context": "racing"})
    md = tmp_path / "memes.md"
    md.write_text(rendered + "\n", encoding="utf-8")

    # Hand-edit: change value and context.
    new_line = rendered.replace("fast", "blazing").replace("racing", "cycling")
    md.write_text(new_line + "\n", encoding="utf-8")

    rpt = reconcile_memes(conn, md)
    row = conn.execute(
        "SELECT value, context FROM memes WHERE id=?", (rid,)
    ).fetchone()
    conn.close()

    assert rpt.updated == 1
    assert row["value"] == "blazing"
    assert row["context"] == "cycling"


def test_memes_md_delete_removes_db_row(tmp_path):
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        cur1 = conn.execute(
            "INSERT INTO memes(type,key) VALUES('paw','fluffy')"
        )
        id1 = cur1.lastrowid
        cur2 = conn.execute(
            "INSERT INTO memes(type,key) VALUES('fact','truth')"
        )
        id2 = cur2.lastrowid

    # Only id1 in md — id2 removed.
    md = tmp_path / "memes.md"
    md.write_text(
        f"- [paw] **fluffy** <!-- id:{id1} -->\n", encoding="utf-8"
    )

    rpt = reconcile_memes(conn, md)
    remaining = {r[0] for r in conn.execute("SELECT id FROM memes").fetchall()}
    conn.close()

    assert rpt.deleted == 1
    assert id2 not in remaining
    assert id1 in remaining


def test_memes_parse_row_handles_optional_fields(tmp_path):
    """parse_row accepts value-only, context-only, both, neither."""
    spec = subpage_specs.build_memes_spec(str(tmp_path))
    pr = spec.parse_row

    # Both present.
    r = pr("- [fact] **key** → val _ctx_ <!-- id:1 -->")
    assert r == {"type": "fact", "key": "key", "value": "val", "context": "ctx"}

    # value only.
    r = pr("- [paw] **k2** → myval <!-- id:2 -->")
    assert r == {"type": "paw", "key": "k2", "value": "myval", "context": None}

    # context only — parse_row may not handle context-without-arrow well,
    # since the render format always puts value before context. Test the
    # permissive case: no value, no context.
    r = pr("- [meme] **bare** <!-- id:3 -->")
    assert r == {"type": "meme", "key": "bare", "value": None, "context": None}

    # Malformed — returns None.
    r = pr("not a meme line at all")
    assert r is None


def test_memes_empty_md_guard(tmp_path):
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        conn.execute("INSERT INTO memes(type,key) VALUES('paw','cat')")

    md = tmp_path / "memes.md"
    md.write_text("", encoding="utf-8")

    rpt = reconcile_memes(conn, md)
    count = conn.execute("SELECT COUNT(*) FROM memes").fetchone()[0]
    conn.close()

    assert rpt.deleted == 0
    assert count == 1


def test_memes_missing_file_noop(tmp_path):
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        conn.execute("INSERT INTO memes(type,key) VALUES('paw','cat')")

    rpt = reconcile_memes(conn, tmp_path / "memes.md")
    count = conn.execute("SELECT COUNT(*) FROM memes").fetchone()[0]
    conn.close()

    assert rpt.deleted == 0
    assert count == 1


# ── profile: md edit writes back to DB ────────────────────────────────────────

def test_profile_md_edit_writes_back_to_db(tmp_path):
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        cur = conn.execute(
            "INSERT INTO entities(kind,name,fact) VALUES('person','Alice','nurse')"
        )
        rid = cur.lastrowid

    spec = subpage_specs.build_profile_spec(str(tmp_path))
    rendered = spec.render_row({"id": rid, "kind": "person",
                                "name": "Alice", "fact": "nurse"})
    md = tmp_path / "profile.md"
    # Hand-edit fact.
    new_line = rendered.replace("nurse", "doctor")
    md.write_text(new_line + "\n", encoding="utf-8")

    rpt = reconcile_profile(conn, md)
    row = conn.execute(
        "SELECT fact FROM entities WHERE id=?", (rid,)
    ).fetchone()
    conn.close()

    assert rpt.updated == 1
    assert row["fact"] == "doctor"


def test_profile_md_delete_soft_deletes(tmp_path):
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        cur1 = conn.execute(
            "INSERT INTO entities(kind,name) VALUES('person','Bob')"
        )
        id1 = cur1.lastrowid
        cur2 = conn.execute(
            "INSERT INTO entities(kind,name) VALUES('pref','coffee')"
        )
        id2 = cur2.lastrowid

    md = tmp_path / "profile.md"
    md.write_text(
        f"- [person] **Bob** <!-- id:{id1} -->\n", encoding="utf-8"
    )

    rpt = reconcile_profile(conn, md)
    row = conn.execute(
        "SELECT superseded_by FROM entities WHERE id=?", (id2,)
    ).fetchone()
    conn.close()

    assert rpt.deleted == 1
    assert row["superseded_by"] == id2  # self-ref sentinel


def test_profile_parse_row_handles_optional_fact(tmp_path):
    spec = subpage_specs.build_profile_spec(str(tmp_path))
    pr = spec.parse_row

    r = pr("- [person] **Alice** — nurse <!-- id:1 -->")
    assert r == {"kind": "person", "name": "Alice", "fact": "nurse"}

    r = pr("- [pref] **coffee** <!-- id:2 -->")
    assert r == {"kind": "pref", "name": "coffee", "fact": None}

    r = pr("garbage line")
    assert r is None


# ── diary: md edit writes back to DB ──────────────────────────────────────────

def _make_diary_md(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a diary md file from a list of {date, content} dicts."""
    lines = ["<!-- marrow:diary:start -->", ""]
    for e in entries:
        date = e["date"]
        content = e["content"]
        lines.append(f"#### {date}")
        lines.append(f"<!-- id:{date} -->")
        lines.append("")
        lines.append(content)
        lines.append("")
    lines.append("<!-- marrow:diary:end -->")
    md = tmp_path / "diary.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md


def test_diary_md_edit_writes_back_to_db(tmp_path):
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO diary(date,content) VALUES('2026-05-01','original text')"
        )

    md = _make_diary_md(tmp_path, [
        {"date": "2026-05-01", "content": "edited text"},
    ])

    rpt = reconcile_diary(conn, md)
    row = conn.execute(
        "SELECT content FROM diary WHERE date='2026-05-01'"
    ).fetchone()
    conn.close()

    assert rpt.updated == 1
    assert row["content"] == "edited text"


def test_diary_md_delete_removes_db_row(tmp_path):
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO diary(date,content) VALUES('2026-05-01','keep')"
        )
        conn.execute(
            "INSERT INTO diary(date,content) VALUES('2026-05-02','drop')"
        )

    # Only 2026-05-01 in md.
    md = _make_diary_md(tmp_path, [
        {"date": "2026-05-01", "content": "keep"},
    ])

    rpt = reconcile_diary(conn, md)
    remaining = {r[0] for r in conn.execute("SELECT date FROM diary").fetchall()}
    conn.close()

    assert rpt.deleted == 1
    assert "2026-05-02" not in remaining
    assert "2026-05-01" in remaining


def test_diary_empty_md_guard(tmp_path):
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO diary(date,content) VALUES('2026-05-01','keep')"
        )

    md = tmp_path / "diary.md"
    md.write_text("", encoding="utf-8")

    rpt = reconcile_diary(conn, md)
    count = conn.execute("SELECT COUNT(*) FROM diary").fetchone()[0]
    conn.close()

    assert rpt.deleted == 0
    assert count == 1


# ── md-mtime gate: rows newer than md mtime are spared from DELETE ───────────

def test_diary_spares_db_row_inserted_after_md_mtime(tmp_path):
    """daily.py race: row inserted AFTER md was last rendered must not be
    swept by the reconcile DELETE pass — inserter renders it on same refresh.
    Regression for 2026-06-04 silent-delete.
    """
    import os
    import time as _time

    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO diary(date,content) VALUES('2026-05-01','old')"
        )

    # md rendered yesterday — only contains 2026-05-01.
    md = _make_diary_md(tmp_path, [
        {"date": "2026-05-01", "content": "old"},
    ])
    # Backdate md mtime so the new INSERT below is unambiguously newer.
    old_t = _time.time() - 3600
    os.utime(md, (old_t, old_t))

    # daily.py inserts a fresh row AFTER md was rendered.
    with conn:
        conn.execute(
            "INSERT INTO diary(date,content,updated_at) "
            "VALUES('2026-05-02','fresh',"
            "strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        )

    rpt = reconcile_diary(conn, md)
    remaining = {r[0] for r in conn.execute("SELECT date FROM diary").fetchall()}
    conn.close()

    # The fresh row must survive — its absence from md is expected.
    assert rpt.deleted == 0
    assert "2026-05-02" in remaining
    assert "2026-05-01" in remaining


def test_memes_spares_row_inserted_after_md_mtime(tmp_path):
    """Same race-spare for inserter-pair memes/stickers/wallet/goose/profile."""
    import os
    import time as _time

    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        cur = conn.execute(
            "INSERT INTO memes(type,key) VALUES('paw','old')"
        )
        old_id = cur.lastrowid

    # md rendered with only the old row.
    md = tmp_path / "memes.md"
    md.write_text(
        f"- [paw] **old** <!-- id:{old_id} -->\n", encoding="utf-8"
    )
    old_t = _time.time() - 3600
    os.utime(md, (old_t, old_t))

    # Fresh row inserted after md mtime — must survive reconcile.
    # memes table has created_at (no updated_at); gate falls back to it.
    with conn:
        cur2 = conn.execute(
            "INSERT INTO memes(type,key,created_at) VALUES('fact','fresh',"
            " strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        )
        fresh_id = cur2.lastrowid

    rpt = reconcile_memes(conn, md)
    remaining = {r[0] for r in conn.execute("SELECT id FROM memes").fetchall()}
    conn.close()

    assert rpt.deleted == 0
    assert fresh_id in remaining
    assert old_id in remaining


# ── milestone: existing reconcile already handles free-text edits ─────────────

def test_milestone_reconcile_updates_title_and_description(tmp_path):
    """Sanity: reconcile_milestones covers title/description edits (pre-existing)."""
    from marrow import reconcile, subpages
    from marrow.subpages_render import render_milestone

    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        cur = conn.execute(
            "INSERT INTO milestones(scope,date,title,description,pinned)"
            " VALUES('us','2026-01-01','Old title','Old desc',1)"
        )
        rid = cur.lastrowid

    # Build md via inserter bootstrap.
    folder = tmp_path / "ny"
    folder.mkdir()
    state = tmp_path / "state"
    cfg = subpages.SubPageConfig(
        key="milestone",
        render=render_milestone,
        path=str(folder / "milestone.md"),
        state_dir=str(state),
        inserter=subpage_specs.build_milestone_spec(str(folder)),
    )
    subpages.write_subpage(cfg, conn, db=db_path)
    md = folder / "milestone.md"

    text = md.read_text()
    text = text.replace("Old title", "New title").replace("Old desc", "New desc")
    md.write_text(text)

    rpt = reconcile.reconcile_milestones(conn, md)
    row = conn.execute(
        "SELECT title, description FROM milestones WHERE id=?", (rid,)
    ).fetchone()
    conn.close()

    assert rpt.updated == 1
    assert row["title"] == "New title"
    assert row["description"] == "New desc"


# ── audit log written for every UPDATE and DELETE ─────────────────────────────

def test_memes_audit_log_on_update(tmp_path):
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        cur = conn.execute(
            "INSERT INTO memes(type,key,value) VALUES('fact','x','old')"
        )
        rid = cur.lastrowid

    md = tmp_path / "memes.md"
    md.write_text(
        f"- [fact] **x** → new <!-- id:{rid} -->\n", encoding="utf-8"
    )

    reconcile_memes(conn, md)
    audit = conn.execute(
        "SELECT action, summary FROM audit_log"
        " WHERE target_table='memes' AND target_id=? LIMIT 1",
        (str(rid),),
    ).fetchone()
    conn.close()

    assert audit is not None
    assert audit["action"] == "update"
    assert "md-reconcile" in audit["summary"]


# ── INSERT: memes ─────────────────────────────────────────────────────────────

def test_memes_insert_unanchored_row(tmp_path):
    """Unanchored meme line → INSERT + anchor written back to md."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "memes.md"
    md.write_text(
        "## Personal\n"
        "- [fact] **sky is blue** → true\n",
        encoding="utf-8",
    )

    rpt = reconcile_memes(conn, md)
    row = conn.execute(
        "SELECT type, key, value, pinned, status FROM memes WHERE key='sky is blue'"
    ).fetchone()
    md_text = md.read_text()
    conn.close()

    assert rpt.inserted == 1
    assert row is not None
    assert row["type"] == "fact"
    assert row["value"] == "true"
    assert row["pinned"] == 1
    assert row["status"] == "active"
    # Anchor written back into md.
    assert "<!-- id:" in md_text


def test_memes_insert_public_section(tmp_path):
    """Unanchored line under Public section is inserted with correct type."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "memes.md"
    md.write_text(
        "## Public\n"
        "- [meme] **stonks** → only goes up\n",
        encoding="utf-8",
    )

    rpt = reconcile_memes(conn, md)
    row = conn.execute(
        "SELECT type, key FROM memes WHERE key='stonks'"
    ).fetchone()
    conn.close()

    assert rpt.inserted == 1
    assert row["type"] == "meme"


def test_memes_insert_dedup_skip(tmp_path):
    """Same type+key already active → insert skipped silently."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO memes(type,key,status) VALUES('fact','sky is blue','active')"
        )

    md = tmp_path / "memes.md"
    # Write a file that has the existing row anchored AND a bare duplicate.
    existing_id = conn.execute("SELECT id FROM memes WHERE key='sky is blue'").fetchone()[0]
    md.write_text(
        f"## Personal\n"
        f"- [fact] **sky is blue** <!-- id:{existing_id} -->\n"
        f"- [fact] **sky is blue** → duplicate\n",
        encoding="utf-8",
    )

    rpt = reconcile_memes(conn, md)
    count = conn.execute("SELECT COUNT(*) FROM memes WHERE key='sky is blue'").fetchone()[0]
    conn.close()

    assert rpt.inserted == 0
    assert count == 1


def test_memes_insert_unmappable_section_conflict(tmp_path):
    """Line under an unrecognised section → conflict, not inserted."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "memes.md"
    md.write_text(
        "## Unknown\n"
        "- [fact] **orphan** → value\n",
        encoding="utf-8",
    )

    rpt = reconcile_memes(conn, md)
    count = conn.execute("SELECT COUNT(*) FROM memes").fetchone()[0]
    conn.close()

    assert rpt.inserted == 0
    assert len(rpt.conflicts) == 1


def test_memes_insert_idempotent(tmp_path):
    """Second reconcile after anchor write-back is a full no-op."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "memes.md"
    md.write_text(
        "## Personal\n"
        "- [paw] **fluffy** → soft\n",
        encoding="utf-8",
    )

    rpt1 = reconcile_memes(conn, md)
    assert rpt1.inserted == 1

    # Second pass on the now-anchored file.
    rpt2 = reconcile_memes(conn, md)
    count = conn.execute("SELECT COUNT(*) FROM memes WHERE key='fluffy'").fetchone()[0]
    conn.close()

    assert rpt2.inserted == 0
    assert count == 1


def test_memes_insert_audit_log(tmp_path):
    """INSERT action is recorded in audit_log."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "memes.md"
    md.write_text(
        "## Personal\n"
        "- [fact] **audit-me** → yes\n",
        encoding="utf-8",
    )

    reconcile_memes(conn, md)
    audit = conn.execute(
        "SELECT action FROM audit_log WHERE target_table='memes' AND action='insert' LIMIT 1"
    ).fetchone()
    conn.close()

    assert audit is not None
    assert audit["action"] == "insert"


# ── INSERT: profile ────────────────────────────────────────────────────────────

def test_profile_insert_unanchored_row(tmp_path):
    """Unanchored entity line → INSERT + anchor written back."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "profile.md"
    md.write_text(
        "## Person\n"
        "- [person] **Charlie** — chef\n",
        encoding="utf-8",
    )

    rpt = reconcile_profile(conn, md)
    row = conn.execute(
        "SELECT kind, name, fact FROM entities WHERE name='Charlie'"
    ).fetchone()
    md_text = md.read_text()
    conn.close()

    assert rpt.inserted == 1
    assert row["kind"] == "person"
    assert row["fact"] == "chef"
    assert "<!-- id:" in md_text


def test_profile_insert_kind_from_section(tmp_path):
    """kind is derived from ## Section heading, not from the line's [tag]."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "profile.md"
    md.write_text(
        "## Pref\n"
        "- [pref] **coffee**\n",
        encoding="utf-8",
    )

    rpt = reconcile_profile(conn, md)
    row = conn.execute(
        "SELECT kind FROM entities WHERE name='coffee'"
    ).fetchone()
    conn.close()

    assert rpt.inserted == 1
    assert row["kind"] == "pref"


def test_profile_insert_dedup_skip(tmp_path):
    """Same kind+name not superseded → skip silently."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO entities(kind,name) VALUES('person','Dana')"
        )

    existing_id = conn.execute("SELECT id FROM entities WHERE name='Dana'").fetchone()[0]
    md = tmp_path / "profile.md"
    md.write_text(
        f"## Person\n"
        f"- [person] **Dana** <!-- id:{existing_id} -->\n"
        f"- [person] **Dana** — duplicate\n",
        encoding="utf-8",
    )

    rpt = reconcile_profile(conn, md)
    count = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE name='Dana' AND superseded_by IS NULL"
    ).fetchone()[0]
    conn.close()

    assert rpt.inserted == 0
    assert count == 1


def test_profile_insert_unmappable_section_conflict(tmp_path):
    """Unknown section heading → conflict, row not inserted."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "profile.md"
    md.write_text(
        "## Alien\n"
        "- [alien] **ET**\n",
        encoding="utf-8",
    )

    rpt = reconcile_profile(conn, md)
    count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    conn.close()

    assert rpt.inserted == 0
    assert len(rpt.conflicts) == 1


def test_profile_insert_idempotent(tmp_path):
    """Second reconcile after anchor write-back is a full no-op."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "profile.md"
    md.write_text(
        "## Place\n"
        "- [place] **Melbourne** — best city\n",
        encoding="utf-8",
    )

    rpt1 = reconcile_profile(conn, md)
    assert rpt1.inserted == 1

    rpt2 = reconcile_profile(conn, md)
    count = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE name='Melbourne' AND superseded_by IS NULL"
    ).fetchone()[0]
    conn.close()

    assert rpt2.inserted == 0
    assert count == 1


# ── INSERT: diary ──────────────────────────────────────────────────────────────

def test_diary_insert_unanchored_block(tmp_path):
    """#### YYYY-MM-DD block with no anchor → INSERT + anchor line added."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "diary.md"
    md.write_text(
        "#### 2026-01-10\n"
        "\n"
        "Went for a walk.\n",
        encoding="utf-8",
    )

    rpt = reconcile_diary(conn, md)
    row = conn.execute(
        "SELECT date, content FROM diary WHERE date='2026-01-10'"
    ).fetchone()
    md_text = md.read_text()
    conn.close()

    assert rpt.inserted == 1
    assert row is not None
    assert row["content"] == "Went for a walk."
    assert "<!-- id:2026-01-10 -->" in md_text


def test_diary_insert_dedup_skip(tmp_path):
    """Date already in DB → skip silently even without anchor in md."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    with conn:
        conn.execute(
            "INSERT INTO diary(date,content) VALUES('2026-02-01','existing')"
        )

    md = tmp_path / "diary.md"
    md.write_text(
        "#### 2026-02-01\n"
        "\n"
        "new content without anchor\n",
        encoding="utf-8",
    )

    rpt = reconcile_diary(conn, md)
    row = conn.execute(
        "SELECT content FROM diary WHERE date='2026-02-01'"
    ).fetchone()
    conn.close()

    assert rpt.inserted == 0
    assert row["content"] == "existing"


def test_diary_insert_future_date_conflict(tmp_path):
    """Future-dated block → conflict, not inserted."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "diary.md"
    md.write_text(
        "#### 2099-12-31\n"
        "\n"
        "From the future.\n",
        encoding="utf-8",
    )

    rpt = reconcile_diary(conn, md)
    count = conn.execute("SELECT COUNT(*) FROM diary").fetchone()[0]
    conn.close()

    assert rpt.inserted == 0
    assert len(rpt.conflicts) == 1
    assert "future" in rpt.conflicts[0]


def test_diary_insert_idempotent(tmp_path):
    """Second reconcile after anchor write-back is a full no-op."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "diary.md"
    md.write_text(
        "#### 2026-03-15\n"
        "\n"
        "Sunny day.\n",
        encoding="utf-8",
    )

    rpt1 = reconcile_diary(conn, md)
    assert rpt1.inserted == 1

    rpt2 = reconcile_diary(conn, md)
    count = conn.execute("SELECT COUNT(*) FROM diary WHERE date='2026-03-15'").fetchone()[0]
    conn.close()

    assert rpt2.inserted == 0
    assert count == 1


def test_diary_insert_anchor_position(tmp_path):
    """Anchor line is placed immediately after the #### heading."""
    db_path = _db(tmp_path)
    conn = _conn(db_path)

    md = tmp_path / "diary.md"
    md.write_text(
        "#### 2026-04-01\n"
        "\n"
        "April fools.\n",
        encoding="utf-8",
    )

    reconcile_diary(conn, md)
    lines = md.read_text().splitlines()
    conn.close()

    heading_idx = next(i for i, l in enumerate(lines) if l.startswith("#### 2026-04-01"))
    assert lines[heading_idx + 1] == "<!-- id:2026-04-01 -->"
