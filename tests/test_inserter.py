"""Inserter — md-as-SoT block-level upsert.

Contract (Plan M Phase B):
- Cold start: file absent or no markers → bootstrap full file, record every
  block in md_index baseline.
- User-edit-wins: existing block hash differs from md_index baseline →
  preserve, do not overwrite.
- DB-changed-md-untouched: existing block hash == baseline, DB row changed
  → emit new body, update baseline.
- Tombstoned: block missing from md AND tombstone present in store → skip.
- Fresh row: block missing from md AND no baseline → append in correct
  section.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from marrow import storage
from marrow.inserter import InserterSpec, write_subpage_inserter
from marrow.md_index import MdIndex


@pytest.fixture()
def store(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    yield MdIndex(conn)
    conn.close()


def _spec(path: str, rows: list[dict],
          group_by: str = "append",
          section_of=lambda r: "",
          section_order=lambda s: sorted(set(s)),
          render_section_header=lambda label: f"## {label}",
          render_row=None) -> InserterSpec:
    """Test helper with sensible defaults — caller supplies rows."""
    def _fetch(_conn):
        return list(rows)
    render = render_row or (lambda r: f"- {r['text']} <!-- id:{r['id']} -->")
    return InserterSpec(
        key="test",
        path=path,
        fetch=_fetch,
        block_id_of=lambda r: str(r["id"]),
        render_row=render,
        group_by=group_by,
        section_of=section_of,
        section_order=section_order,
        render_section_header=render_section_header,
        empty_message="_(none yet)_",
    )


# ── cold-start bootstrap ───────────────────────────────────────────────────

def test_cold_start_bootstrap_creates_file(store, tmp_path):
    path = str(tmp_path / "p.md")
    rows = [{"id": 1, "text": "alpha"}, {"id": 2, "text": "beta"}]
    spec = _spec(path, rows)
    counts = write_subpage_inserter(spec, store.conn, store)
    text = Path(path).read_text()
    assert "<!-- marrow:test:start -->" in text
    assert "<!-- id:1 -->" in text
    assert "<!-- id:2 -->" in text
    assert counts["bootstrapped"] == 2
    # md_index baseline recorded for every block.
    assert store.get_hash(path, "1") is not None
    assert store.get_hash(path, "2") is not None


def test_cold_start_empty_rows_emits_placeholder(store, tmp_path):
    path = str(tmp_path / "p.md")
    spec = _spec(path, [])
    write_subpage_inserter(spec, store.conn, store)
    text = Path(path).read_text()
    assert "_(none yet)_" in text
    assert "<!-- marrow:test:start -->" in text
    assert "<!-- marrow:test:end -->" in text


# ── user-edit-wins ──────────────────────────────────────────────────────────

def test_user_edit_preserved(store, tmp_path):
    """User edits block in md → next inserter pass keeps the edit."""
    path = str(tmp_path / "p.md")
    rows = [{"id": 1, "text": "alpha"}]
    spec = _spec(path, rows)
    write_subpage_inserter(spec, store.conn, store)

    # User edits the block.
    text = Path(path).read_text()
    edited = text.replace("- alpha <!-- id:1 -->", "- alpha HAND EDIT <!-- id:1 -->")
    Path(path).write_text(edited)
    # Watcher updates md_index baseline to reflect the user's edit.
    store.sync_file(path)

    # Re-run inserter — DB still says "alpha", but user version is canonical.
    counts = write_subpage_inserter(spec, store.conn, store)
    final = Path(path).read_text()
    assert "HAND EDIT" in final
    assert counts["preserved"] == 1
    assert counts["appended"] == 0


def test_db_changed_md_untouched_does_not_overwrite(store, tmp_path):
    """md always wins. DB row changes → existing block stays as-is. The user
    must delete the block to let the next inserter pass re-emit fresh data.
    """
    path = str(tmp_path / "p.md")
    rows = [{"id": 1, "text": "alpha"}]
    spec = _spec(path, rows)
    write_subpage_inserter(spec, store.conn, store)

    # DB updates the row body.
    rows[0] = {"id": 1, "text": "alpha v2"}
    counts = write_subpage_inserter(spec, store.conn, store)
    final = Path(path).read_text()
    assert "alpha v2" not in final
    assert "- alpha <!-- id:1 -->" in final
    assert counts["preserved"] == 1


def test_user_delete_then_db_change_then_inserter_reemits(store, tmp_path):
    """Workflow: user deletes block, DB still has the row but NOT tombstoned
    in store yet (watcher hasn't run) → next inserter pass appends the block
    fresh. After tombstone arrives via watcher, the same flow now skips."""
    path = str(tmp_path / "p.md")
    rows = [{"id": 1, "text": "alpha"}, {"id": 2, "text": "beta"}]
    spec = _spec(path, rows)
    write_subpage_inserter(spec, store.conn, store)

    # User deletes id:2 manually without watcher.
    text = Path(path).read_text()
    Path(path).write_text(text.replace("- beta <!-- id:2 -->\n", ""))

    # Inserter runs before watcher — block is absent and not tombstoned → re-emit.
    counts = write_subpage_inserter(spec, store.conn, store)
    final = Path(path).read_text()
    assert "<!-- id:2 -->" in final
    assert counts["appended"] == 1


# ── tombstone ──────────────────────────────────────────────────────────────

def test_tombstoned_block_not_resurrected(store, tmp_path):
    """User deletes block → watcher tombstones → inserter does not bring it back."""
    path = str(tmp_path / "p.md")
    rows = [{"id": 1, "text": "alpha"}, {"id": 2, "text": "beta"}]
    spec = _spec(path, rows)
    write_subpage_inserter(spec, store.conn, store)

    # User deletes id:2.
    text = Path(path).read_text()
    edited = text.replace("- beta <!-- id:2 -->\n", "")
    Path(path).write_text(edited)
    store.sync_file(path)
    assert any(t[0] == "2" for t in store.list_tombstones(path))

    # Re-run inserter — id:2 should NOT be re-emitted.
    counts = write_subpage_inserter(spec, store.conn, store)
    final = Path(path).read_text()
    assert "<!-- id:2 -->" not in final
    assert counts["tombstoned_skipped"] == 1


def test_tombstoned_fetch_emits_ghost_alert(store, tmp_path, monkeypatch):
    """A DB row fetched with a tombstoned block_id is a ghost (freed-id
    reuse) — should be impossible in steady state, so it must raise a
    warn alert alongside the existing skip behaviour."""
    from marrow import repo

    path = str(tmp_path / "p.md")
    rows = [{"id": 1, "text": "alpha"}, {"id": 2, "text": "beta"}]
    spec = _spec(path, rows)
    write_subpage_inserter(spec, store.conn, store)

    text = Path(path).read_text()
    Path(path).write_text(text.replace("- beta <!-- id:2 -->\n", ""))
    store.sync_file(path)

    calls = []
    monkeypatch.setattr(
        repo, "add_alert",
        lambda *a, **kw: calls.append((a, kw)),
    )
    counts = write_subpage_inserter(spec, store.conn, store)
    assert counts["tombstoned_skipped"] == 1
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "warn"
    assert "inserter_ghost_tombstoned:test" in args[2]
    assert "2" in kwargs["message"]


def test_no_ghost_alert_when_nothing_tombstoned(store, tmp_path, monkeypatch):
    from marrow import repo

    path = str(tmp_path / "p.md")
    rows = [{"id": 1, "text": "alpha"}]
    spec = _spec(path, rows)
    write_subpage_inserter(spec, store.conn, store)

    calls = []
    monkeypatch.setattr(
        repo, "add_alert",
        lambda *a, **kw: calls.append((a, kw)),
    )
    write_subpage_inserter(spec, store.conn, store)
    assert calls == []


# ── append new rows ────────────────────────────────────────────────────────

def test_new_row_appended_in_section(store, tmp_path):
    """New DB row → appended under the right section header."""
    path = str(tmp_path / "p.md")
    rows = [
        {"id": 1, "tag": "personal", "text": "alpha"},
        {"id": 2, "tag": "public", "text": "beta"},
    ]
    spec = _spec(path, rows,
                 group_by="tag",
                 section_of=lambda r: r["tag"],
                 section_order=lambda s: ["personal", "public"])
    write_subpage_inserter(spec, store.conn, store)

    # Add a new row under personal.
    rows.append({"id": 3, "tag": "personal", "text": "gamma"})
    counts = write_subpage_inserter(spec, store.conn, store)
    text = Path(path).read_text()
    assert "<!-- id:3 -->" in text
    # Personal section should now hold ids 1 and 3 above the Public section.
    p_idx = text.find("## personal")
    pub_idx = text.find("## public")
    g_idx = text.find("<!-- id:3 -->")
    assert p_idx < g_idx < pub_idx
    assert counts["appended"] == 1


def test_new_row_with_empty_section_appended_before_end_marker(store, tmp_path):
    """group_by=append → new row sits just before the end marker."""
    path = str(tmp_path / "p.md")
    rows = [{"id": 1, "text": "alpha"}]
    spec = _spec(path, rows)
    write_subpage_inserter(spec, store.conn, store)

    rows.append({"id": 2, "text": "beta"})
    write_subpage_inserter(spec, store.conn, store)
    text = Path(path).read_text()
    assert text.index("<!-- id:1 -->") < text.index("<!-- id:2 -->")
    assert "<!-- id:2 -->" in text
    assert text.index("<!-- id:2 -->") < text.index("<!-- marrow:test:end -->")


# ── idempotency ────────────────────────────────────────────────────────────

def test_rerun_idempotent_when_md_matches_db(store, tmp_path):
    """Re-running with no changes → file content stable, baseline unchanged."""
    path = str(tmp_path / "p.md")
    rows = [{"id": 1, "text": "alpha"}, {"id": 2, "text": "beta"}]
    spec = _spec(path, rows)
    write_subpage_inserter(spec, store.conn, store)
    first = Path(path).read_text()
    first_hash = store.get_hash(path, "1")

    write_subpage_inserter(spec, store.conn, store)
    second = Path(path).read_text()
    assert first == second
    assert store.get_hash(path, "1") == first_hash


# ── layout: rows in same section run flush ─────────────────────────────────


def test_bootstrap_rows_in_same_section_have_no_blank_between(store, tmp_path):
    """Cold-start emits a flush list — adjacent rows are not separated by a
    blank line."""
    path = str(tmp_path / "p.md")
    rows = [
        {"id": 1, "tag": "A", "text": "alpha"},
        {"id": 2, "tag": "A", "text": "beta"},
        {"id": 3, "tag": "A", "text": "gamma"},
    ]
    spec = _spec(path, rows, group_by="tag",
                 section_of=lambda r: r["tag"],
                 section_order=lambda s: ["A"])
    write_subpage_inserter(spec, store.conn, store)
    text = Path(path).read_text()
    # Adjacent rows: no blank line between them.
    assert "- alpha <!-- id:1 -->\n- beta <!-- id:2 -->" in text
    assert "- beta <!-- id:2 -->\n- gamma <!-- id:3 -->" in text


def test_bootstrap_sections_separated_by_blank_line(store, tmp_path):
    """Each section header sits between blank lines; last row of one
    section + first row of the next are not adjacent."""
    path = str(tmp_path / "p.md")
    rows = [
        {"id": 1, "tag": "A", "text": "alpha"},
        {"id": 2, "tag": "B", "text": "beta"},
    ]
    spec = _spec(path, rows, group_by="tag",
                 section_of=lambda r: r["tag"],
                 section_order=lambda s: ["A", "B"])
    write_subpage_inserter(spec, store.conn, store)
    text = Path(path).read_text()
    # Section header keeps a blank above + below.
    assert "\n\n## B\n\n- beta" in text


def test_bootstrap_with_subsection_emits_sub_header(store, tmp_path):
    """When subsection_of is set, cold-start emits subsection headers and
    rows under each subsection stay flush."""
    path = str(tmp_path / "p.md")
    rows = [
        {"id": 1, "year": "2026", "month": "April", "text": "a"},
        {"id": 2, "year": "2026", "month": "April", "text": "b"},
        {"id": 3, "year": "2026", "month": "May", "text": "c"},
    ]
    spec = InserterSpec(
        key="test",
        path=path,
        fetch=lambda _c: list(rows),
        block_id_of=lambda r: str(r["id"]),
        render_row=lambda r: f"- {r['text']} <!-- id:{r['id']} -->",
        group_by="date",
        section_of=lambda r: r["year"],
        section_order=lambda s: sorted(set(s)),
        subsection_of=lambda r: r["month"],
        render_subsection_header=lambda m: f"### {m}",
        empty_message="_(none)_",
    )
    write_subpage_inserter(spec, store.conn, store)
    text = Path(path).read_text()
    assert "## 2026" in text
    assert "### April" in text
    assert "### May" in text
    # Adjacent rows inside the same sub-month run flush.
    assert "- a <!-- id:1 -->\n- b <!-- id:2 -->" in text
    # Sub header bordered by blank lines.
    assert "\n\n### May\n\n- c <!-- id:3 -->" in text


def test_append_keeps_flush_layout(store, tmp_path):
    """New row appended into an existing section sits flush against the
    previous row — no extra blank line."""
    path = str(tmp_path / "p.md")
    rows = [{"id": 1, "tag": "A", "text": "alpha"}]
    spec = _spec(path, rows, group_by="tag",
                 section_of=lambda r: r["tag"],
                 section_order=lambda s: ["A"])
    write_subpage_inserter(spec, store.conn, store)
    rows.append({"id": 2, "tag": "A", "text": "beta"})
    write_subpage_inserter(spec, store.conn, store)
    text = Path(path).read_text()
    assert "- alpha <!-- id:1 -->\n- beta <!-- id:2 -->" in text


# ── force_sort_consistency: rebootstrap on order divergence ───────────────


def test_force_sort_consistency_rebootstraps_when_md_diverges(store, tmp_path):
    """When md anchored blocks have drifted out of fetch ORDER (e.g. catchup
    appended an older row after a newer one), force_sort_consistency triggers
    a rebootstrap so the file returns to canonical order."""
    path = str(tmp_path / "p.md")
    # Manually craft an out-of-order md state to simulate catchup append.
    initial = (
        "<!-- marrow:test:start -->\n\n"
        "- z <!-- id:2026-05-25 -->\n"
        "- a <!-- id:2026-05-17 -->\n"
        "- b <!-- id:2026-05-18 -->\n\n"
        "<!-- marrow:test:end -->\n"
    )
    Path(path).write_text(initial)
    store.sync_file(path)
    rows = [
        {"id": "2026-05-17", "text": "a"},
        {"id": "2026-05-18", "text": "b"},
        {"id": "2026-05-25", "text": "z"},
    ]
    spec = InserterSpec(
        key="test", path=path,
        fetch=lambda _c: list(rows),
        block_id_of=lambda r: r["id"],
        render_row=lambda r: f"- {r['text']} <!-- id:{r['id']} -->",
        empty_message="_(none)_",
        force_sort_consistency=True,
    )
    write_subpage_inserter(spec, store.conn, store)
    text = Path(path).read_text()
    # After rebootstrap, blocks appear in fetch order (oldest → newest).
    a_idx = text.index("<!-- id:2026-05-17 -->")
    b_idx = text.index("<!-- id:2026-05-18 -->")
    z_idx = text.index("<!-- id:2026-05-25 -->")
    assert a_idx < b_idx < z_idx


def test_force_sort_consistency_skips_when_order_matches(store, tmp_path):
    """When md order already matches fetch ORDER, the inserter does not
    touch the file (preserve mode runs as usual)."""
    path = str(tmp_path / "p.md")
    rows = [
        {"id": "2026-05-17", "text": "a"},
        {"id": "2026-05-18", "text": "b"},
    ]
    spec = InserterSpec(
        key="test", path=path,
        fetch=lambda _c: list(rows),
        block_id_of=lambda r: r["id"],
        render_row=lambda r: f"- {r['text']} <!-- id:{r['id']} -->",
        empty_message="_(none)_",
        force_sort_consistency=True,
    )
    write_subpage_inserter(spec, store.conn, store)
    first = Path(path).read_text()
    # User adds a non-anchored note inside the block.
    edited = first.replace(
        "- a <!-- id:2026-05-17 -->\n",
        "- a <!-- id:2026-05-17 -->\n\n> hand note\n\n",
    )
    Path(path).write_text(edited)
    store.sync_file(path)
    # Re-run — order still matches DB, so the hand note must survive.
    write_subpage_inserter(spec, store.conn, store)
    final = Path(path).read_text()
    assert "> hand note" in final


# ── recovery: file missing markers triggers bootstrap ──────────────────────

def test_file_with_no_markers_rebootstraps(store, tmp_path):
    """File exists but has no <!-- id:N --> markers → treat as cold start."""
    path = str(tmp_path / "p.md")
    Path(path).write_text("some unrelated text\nno markers here\n")
    rows = [{"id": 1, "text": "alpha"}]
    spec = _spec(path, rows)
    counts = write_subpage_inserter(spec, store.conn, store)
    text = Path(path).read_text()
    assert "<!-- id:1 -->" in text
    # Bootstrap wipes the prior content — this matches the cold-start contract.
    # If we ever need to preserve prior text, do it via a backup, not in-file.
    assert "no markers here" not in text
    assert counts["bootstrapped"] == 1


# ── write-failure isolation (Outcome 1) ────────────────────────────────────

def test_write_failure_does_not_corrupt_baseline(store, tmp_path, monkeypatch):
    """If _atomic_write raises (ENOSPC / EACCES / SIGTERM mid-write), md_index
    must keep its prior baseline — never the body that failed to land. Today
    the inserter would have recorded the new hash before the write attempt,
    making the next refresh think the user's old on-disk content was a user edit
    against fresh DB rows.
    """
    from marrow import inserter

    path = str(tmp_path / "p.md")
    rows_v1 = [{"id": 1, "text": "alpha"}]
    spec_v1 = _spec(path, rows_v1)
    write_subpage_inserter(spec_v1, store.conn, store)
    on_disk_text = Path(path).read_text(encoding="utf-8")
    baseline_v1 = store.get_hash(path, "1")
    assert baseline_v1 is not None

    # Append a brand-new row, but force the second write to fail.
    rows_v2 = rows_v1 + [{"id": 2, "text": "beta"}]
    spec_v2 = _spec(path, rows_v2)
    call_count = {"n": 0}
    real_write = inserter._atomic_write

    def flaky_write(p, data):
        call_count["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(inserter, "_atomic_write", flaky_write)
    with pytest.raises(OSError):
        write_subpage_inserter(spec_v2, store.conn, store)
    monkeypatch.setattr(inserter, "_atomic_write", real_write)

    # File must still hold the v1 content — the failed write never landed.
    assert Path(path).read_text(encoding="utf-8") == on_disk_text
    # md_index must NOT have a baseline for the row whose write failed; the
    # v1 baseline must be untouched.
    assert store.get_hash(path, "1") == baseline_v1
    assert store.get_hash(path, "2") is None
