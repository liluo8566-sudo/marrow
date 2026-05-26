"""md_index — parser + MdIndex + full_scan reconcile."""
from __future__ import annotations

import pytest

from marrow import md_index, storage
from marrow.md_index import MdIndex, parse_blocks


@pytest.fixture()
def store(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    yield MdIndex(conn)
    conn.close()


def test_parse_blocks_id_only_marker():
    text = (
        "# heading\n"
        "preamble line\n"
        "- [fact] alpha <!-- id:1 -->\n"
        "- [fact] beta <!-- id:2 -->\n"
    )
    blocks, has = parse_blocks(text)
    assert has is True
    assert [b.block_id for b in blocks] == ["1", "2"]
    assert blocks[0].body.endswith("<!-- id:1 -->")
    assert blocks[1].body.endswith("<!-- id:2 -->")


def test_parse_blocks_id_with_date():
    text = (
        "## 2026-05-25\n"
        "- entry <!-- id:42 date:2026-05-25 -->\n"
        "  detail line\n"
        "- next <!-- id:43 date:2026-05-25 -->\n"
    )
    blocks, has = parse_blocks(text)
    assert has and [b.block_id for b in blocks] == ["42", "43"]
    assert "detail line" in blocks[0].body


def test_parse_blocks_no_markers():
    blocks, has = parse_blocks("just prose\nno markers\n")
    assert blocks == [] and has is False


def test_record_and_get_hash(store):
    store.record_block("/x/foo.md", "1", "h1")
    assert store.get_hash("/x/foo.md", "1") == "h1"
    assert store.get_hash("/x/foo.md", "missing") is None


def test_record_overwrite_updates_hash_and_clears_tombstone(store):
    store.record_block("/x/foo.md", "1", "h1")
    store.tombstone("/x/foo.md", "1")
    assert store.get_hash("/x/foo.md", "1") is None
    store.record_block("/x/foo.md", "1", "h2")
    assert store.get_hash("/x/foo.md", "1") == "h2"


def test_tombstone_and_list(store):
    store.record_block("/x/a.md", "1", "h")
    store.record_block("/x/a.md", "2", "h")
    store.tombstone("/x/a.md", "1")
    rows = store.list_tombstones("/x/a.md")
    assert [r[0] for r in rows] == ["1"]
    assert rows[0][1]  # ISO timestamp present


def test_clear_tombstone(store):
    store.record_block("/x/a.md", "1", "h")
    store.tombstone("/x/a.md", "1")
    store.clear_tombstone("/x/a.md", "1")
    assert store.get_hash("/x/a.md", "1") == "h"
    assert store.list_tombstones("/x/a.md") == []


def test_sync_file_insert(store, tmp_path):
    f = tmp_path / "f.md"
    f.write_text("- alpha <!-- id:1 -->\n- beta <!-- id:2 -->\n")
    r = store.sync_file(str(f))
    assert r.inserted == 2 and r.tombstoned == 0
    assert store.get_hash(str(f), "1") is not None


def test_sync_file_update(store, tmp_path):
    f = tmp_path / "f.md"
    f.write_text("- alpha <!-- id:1 -->\n")
    store.sync_file(str(f))
    f.write_text("- alpha CHANGED <!-- id:1 -->\n")
    r = store.sync_file(str(f))
    assert r.updated == 1 and r.inserted == 0


def test_sync_file_tombstone_on_delete_block(store, tmp_path):
    f = tmp_path / "f.md"
    f.write_text("- a <!-- id:1 -->\n- b <!-- id:2 -->\n")
    store.sync_file(str(f))
    f.write_text("- a <!-- id:1 -->\n")  # id:2 removed
    r = store.sync_file(str(f))
    assert r.tombstoned == 1
    assert store.get_hash(str(f), "2") is None
    assert store.get_hash(str(f), "1") is not None


def test_sync_file_clear_tombstone_on_readd(store, tmp_path):
    f = tmp_path / "f.md"
    f.write_text("- a <!-- id:1 -->\n")
    store.sync_file(str(f))
    f.write_text("(empty)\n")
    store.sync_file(str(f))
    assert store.get_hash(str(f), "1") is None
    f.write_text("- a <!-- id:1 -->\n")
    r = store.sync_file(str(f))
    assert r.cleared == 1
    assert store.get_hash(str(f), "1") is not None


def test_sync_file_missing_file_is_coldstart_noop(store, tmp_path):
    # Seed db with blocks for a path, delete the file, sync_file must NOT
    # tombstone — treat missing file as cold start, leave md_index untouched.
    f = tmp_path / "ghost.md"
    store.record_block(str(f), "1", "h1")
    store.record_block(str(f), "2", "h2")
    assert not f.exists()
    r = store.sync_file(str(f))
    assert r.tombstoned == 0
    assert store.list_tombstones(str(f)) == []
    assert store.get_hash(str(f), "1") == "h1"
    assert store.get_hash(str(f), "2") == "h2"


def test_sync_file_warns_no_markers(store, tmp_path):
    f = tmp_path / "plain.md"
    f.write_text("nothing here\n")
    r = store.sync_file(str(f))
    assert r.files_without_markers == [str(f)]
    assert r.inserted == 0


def test_full_scan_walks_dir_recursively(store, tmp_path):
    root = tmp_path / "db-pages"
    (root / "sub").mkdir(parents=True)
    (root / "a.md").write_text("- a <!-- id:1 -->\n")
    (root / "sub" / "b.md").write_text("- b <!-- id:2 -->\n")
    r = store.full_scan([str(root)])
    assert r.inserted == 2 and r.scanned_files == 2


def test_full_scan_file_root(store, tmp_path):
    f = tmp_path / "dashboard.md"
    f.write_text("- x <!-- id:1 -->\n")
    r = store.full_scan([str(f)])
    assert r.inserted == 1 and r.scanned_files == 1


def test_full_scan_tombstones_deleted_file(store, tmp_path):
    root = tmp_path / "db-pages"
    root.mkdir()
    f = root / "doomed.md"
    f.write_text("- x <!-- id:1 -->\n- y <!-- id:2 -->\n")
    store.full_scan([str(root)])
    f.unlink()
    r = store.full_scan([str(root)])
    assert r.tombstoned == 2


def test_is_tombstoned(store):
    store.record_block("/x/a.md", "1", "h1")
    assert store.is_tombstoned("/x/a.md", "1") is False
    store.tombstone("/x/a.md", "1")
    assert store.is_tombstoned("/x/a.md", "1") is True
    store.clear_tombstone("/x/a.md", "1")
    assert store.is_tombstoned("/x/a.md", "1") is False
    # Unknown row → not tombstoned.
    assert store.is_tombstoned("/x/a.md", "unknown") is False


def test_full_scan_ignores_paths_outside_roots(store, tmp_path):
    foreign = tmp_path / "elsewhere.md"
    foreign.write_text("- x <!-- id:1 -->\n")
    store.sync_file(str(foreign))
    root = tmp_path / "db-pages"
    root.mkdir()
    r = store.full_scan([str(root)])  # foreign path stays untouched
    assert r.tombstoned == 0
    assert store.get_hash(str(foreign), "1") is not None
