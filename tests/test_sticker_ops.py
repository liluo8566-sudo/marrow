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

    assert second == {"duplicate": True, "existing_id": first["id"]}
