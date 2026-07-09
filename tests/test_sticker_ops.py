import hashlib
import sys
import types

import pytest

from marrow import storage
from marrow import sticker_ops


@pytest.fixture()
def db(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    yield conn
    conn.close()


def _fake_pil(monkeypatch):
    class FakeImage:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def thumbnail(self, _size): pass

        def save(self, path, _format):
            path.write_bytes(b"webp")

    image_mod = types.SimpleNamespace(open=lambda _path: FakeImage())
    pil_mod = types.ModuleType("PIL")
    pil_mod.Image = image_mod
    monkeypatch.setitem(sys.modules, "PIL", pil_mod)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_mod)


def test_sha256_file(tmp_path):
    p = tmp_path / "known.bin"
    p.write_bytes(b"known bytes")
    assert sticker_ops.sha256_file(str(p)) == hashlib.sha256(b"known bytes").hexdigest()


def test_ingest_sticker_basic(db, tmp_path, monkeypatch):
    _fake_pil(monkeypatch)
    monkeypatch.setattr(sticker_ops, "STICKERS_DIR", tmp_path / "stickers")
    src = tmp_path / "src.png"
    src.write_bytes(b"fake png")

    out = sticker_ops.ingest_sticker(db, str(src), "small grin", "test")

    assert out["duplicate"] is False
    row = db.execute(
        "SELECT path, desc, source FROM stickers WHERE id = ?", (out["id"],)
    ).fetchone()
    assert dict(row) == {"path": out["path"], "desc": "small grin", "source": "test"}
    assert (tmp_path / "stickers" / "stk_001.png").exists()
    assert (tmp_path / "stickers" / "_thumb" / "stk_001.webp").exists()


def test_ingest_sticker_sha256_dedup(db, tmp_path, monkeypatch):
    _fake_pil(monkeypatch)
    monkeypatch.setattr(sticker_ops, "STICKERS_DIR", tmp_path / "stickers")
    src = tmp_path / "src.png"
    src.write_bytes(b"same image")

    first = sticker_ops.ingest_sticker(db, str(src), "first", "test")
    second = sticker_ops.ingest_sticker(db, str(src), "second", "test")

    assert second["duplicate"] is True
    assert second["existing_id"] == first["id"]
    assert second["path"] == first["path"]


def test_update_sticker_success(db, tmp_path, monkeypatch):
    _fake_pil(monkeypatch)
    monkeypatch.setattr(sticker_ops, "STICKERS_DIR", tmp_path / "stickers")
    src = tmp_path / "src.png"
    src.write_bytes(b"img data")
    result = sticker_ops.ingest_sticker(db, str(src), "old desc", "test")

    out = sticker_ops.update_sticker(db, result["id"], "new desc")

    assert out == {"ok": True, "id": result["id"], "desc": "new desc", "old_desc": "old desc"}
    row = db.execute("SELECT desc FROM stickers WHERE id = ?", (result["id"],)).fetchone()
    assert row["desc"] == "new desc"


def test_update_sticker_not_found(db):
    out = sticker_ops.update_sticker(db, 9999, "whatever")
    assert out == {"ok": False, "error": "not_found"}


def test_delete_sticker_success(db, tmp_path, monkeypatch):
    _fake_pil(monkeypatch)
    stickers_dir = tmp_path / "stickers"
    monkeypatch.setattr(sticker_ops, "STICKERS_DIR", stickers_dir)
    src = tmp_path / "src.png"
    src.write_bytes(b"img data")
    result = sticker_ops.ingest_sticker(db, str(src), "a sticker", "test")

    sticker_file = stickers_dir / "stk_001.png"
    thumb_file = stickers_dir / "_thumb" / "stk_001.webp"
    assert sticker_file.exists()
    assert thumb_file.exists()

    out = sticker_ops.delete_sticker(db, result["id"])

    assert out == {"ok": True, "id": result["id"], "desc": "a sticker",
                    "deleted_path": str(sticker_file)}
    # trashed, not hard-deleted: gone from the original location either way
    assert not sticker_file.exists()
    assert not thumb_file.exists()
    assert db.execute("SELECT id FROM stickers WHERE id = ?", (result["id"],)).fetchone() is None


def test_delete_sticker_not_found(db):
    out = sticker_ops.delete_sticker(db, 9999)
    assert out == {"ok": False, "error": "not_found"}


def test_reconcile_stickers_md_delete_unlinks_files(db, tmp_path):
    """Reverse path: hand-deleting a sticker's md line makes reconcile drop the
    DB row AND unlink the orphaned image + thumbnail from disk."""
    from marrow.reconcile_inserter import reconcile_stickers

    img = tmp_path / "stk_001.png"
    img.write_bytes(b"img")
    thumb = tmp_path / "_thumb" / "stk_001.webp"
    thumb.parent.mkdir()
    thumb.write_bytes(b"thumb")
    with db:
        db.execute(
            "INSERT INTO stickers (id, path, sha256, desc, source,"
            " created_at, updated_at) VALUES"
            " (1, ?, 'abc', 'happy', 'wechat',"
            " '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')", (str(img),))
    md = tmp_path / "stickers.md"
    # md WITHOUT id=1 (hand-deleted) but with a surviving anchor so the
    # empty-file guard in reconcile_inserter_sync does not short-circuit.
    md.write_text(
        "<!-- marrow:stickers:start -->\n"
        "- stk_002 keep <!-- id:2 -->\n"
        "<!-- marrow:stickers:end -->\n", encoding="utf-8")

    rpt = reconcile_stickers(db, md)

    assert rpt.deleted == 1
    assert db.execute(
        "SELECT COUNT(*) FROM stickers WHERE id=1").fetchone()[0] == 0
    assert not img.exists()
    assert not thumb.exists()
