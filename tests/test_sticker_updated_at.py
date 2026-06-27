"""Tests for stickers.updated_at — migration, write paths, bidirectional reconcile."""
from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path

import pytest

from marrow import storage, sticker_ops
from marrow.reconcile_inserter import reconcile_stickers


@pytest.fixture()
def db(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    yield conn
    conn.close()


def _fake_pil(monkeypatch):
    class FakeImage:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def thumbnail(self, _size): pass
        def save(self, path, _fmt): Path(path).write_bytes(b"webp")

    img_mod = types.SimpleNamespace(open=lambda _p: FakeImage())
    pil_mod = types.ModuleType("PIL")
    pil_mod.Image = img_mod
    monkeypatch.setitem(sys.modules, "PIL", pil_mod)
    monkeypatch.setitem(sys.modules, "PIL.Image", img_mod)


def _ingest(db, tmp_path, monkeypatch, *, content=b"img", desc="a sticker"):
    _fake_pil(monkeypatch)
    monkeypatch.setattr(sticker_ops, "STICKERS_DIR", tmp_path / "stickers")
    src = tmp_path / f"src_{content.hex()[:6]}.png"
    src.write_bytes(content)
    return sticker_ops.ingest_sticker(db, str(src), desc, "test")


def test_migration_updated_at_column_exists(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(stickers)").fetchall()}
    assert "updated_at" in cols


def test_ingest_sets_updated_at(db, tmp_path, monkeypatch):
    result = _ingest(db, tmp_path, monkeypatch)
    row = db.execute(
        "SELECT updated_at, created_at FROM stickers WHERE id=?", (result["id"],)
    ).fetchone()
    assert row["updated_at"] is not None
    assert row["updated_at"] == row["created_at"]


def test_update_sticker_bumps_updated_at(db, tmp_path, monkeypatch):
    result = _ingest(db, tmp_path, monkeypatch)
    before = db.execute(
        "SELECT updated_at FROM stickers WHERE id=?", (result["id"],)
    ).fetchone()["updated_at"]

    time.sleep(1.1)
    sticker_ops.update_sticker(db, result["id"], "new desc")

    after = db.execute(
        "SELECT updated_at FROM stickers WHERE id=?", (result["id"],)
    ).fetchone()["updated_at"]
    assert after > before


def test_db_wins_when_db_newer_than_md(db, tmp_path, monkeypatch):
    result = _ingest(db, tmp_path, monkeypatch, desc="original")
    stk_id = result["id"]

    md_path = tmp_path / "stickers.md"
    md_path.write_text(
        f"- stk_{stk_id:03d} original <!-- id:{stk_id} -->\n",
        encoding="utf-8",
    )

    old_mtime = time.time() - 2
    os.utime(md_path, (old_mtime, old_mtime))

    time.sleep(1.1)
    sticker_ops.update_sticker(db, stk_id, "updated in db")

    rpt = reconcile_stickers(db, md_path)

    row = db.execute(
        "SELECT desc FROM stickers WHERE id=?", (stk_id,)
    ).fetchone()
    assert row["desc"] == "updated in db"

    md_content = md_path.read_text(encoding="utf-8")
    assert "updated in db" in md_content
