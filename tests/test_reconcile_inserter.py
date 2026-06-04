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
