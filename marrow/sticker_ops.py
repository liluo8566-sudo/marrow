from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

STICKERS_DIR = Path.home() / "Desktop/NY/stickers"
_SIPS = "/usr/bin/sips"
_CANVAS = 200


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with Path(path).expanduser().open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def phash_file(path: str) -> str | None:
    try:
        import imagehash
        from PIL import Image
    except ImportError:
        return None
    with Image.open(Path(path).expanduser()) as img:
        return str(imagehash.phash(img))


def _hamming(a: str, b: str) -> int | None:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except (TypeError, ValueError):
        return None


def _standardize_image(path: Path) -> bool:
    """Fit to _CANVAS x _CANVAS square (aspect-preserved, white pad). Skips GIF."""
    if path.suffix.lower() == ".gif":
        return False
    try:
        subprocess.run(
            [_SIPS, "-Z", str(_CANVAS), str(path), "--out", str(path)],
            check=True, timeout=15, capture_output=True,
        )
        subprocess.run(
            [_SIPS, "-p", str(_CANVAS), str(_CANVAS),
             "--padColor", "FFFFFF", str(path), "--out", str(path)],
            check=True, timeout=15, capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning("sticker standardize failed for %s: %s", path.name, e)
        return False


def ingest_sticker(conn, src_path: str, desc: str, source: str = "wechat") -> dict:
    src = Path(src_path).expanduser()
    digest = sha256_file(str(src))
    row = conn.execute(
        "SELECT id FROM stickers WHERE sha256 = ? LIMIT 1", (digest,)
    ).fetchone()
    if row:
        return {"duplicate": True, "existing_id": row["id"]}

    phash = phash_file(str(src))
    if phash:
        rows = conn.execute(
            "SELECT id, phash FROM stickers WHERE phash IS NOT NULL"
        ).fetchall()
        for existing in rows:
            dist = _hamming(phash, existing["phash"])
            if dist is not None and dist <= 8:
                return {
                    "duplicate": True,
                    "existing_id": existing["id"],
                    "near_dup": True,
                }

    stickers_dir = STICKERS_DIR.expanduser()
    thumb_dir = stickers_dir / "_thumb"
    stickers_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    cursor = conn.execute(
        "INSERT INTO stickers(path, sha256, phash, desc, source)"
        " VALUES(?,?,?,?,?)",
        ("_pending", digest, phash, desc, source),
    )
    stk_id = cursor.lastrowid

    ext = src.suffix
    new_path = stickers_dir / f"stk_{stk_id:03d}{ext}"
    shutil.copy2(src, new_path)
    _standardize_image(new_path)

    try:
        from PIL import Image
    except ImportError:
        pass
    else:
        thumb_path = thumb_dir / f"stk_{stk_id:03d}.webp"
        with Image.open(new_path) as img:
            img.thumbnail((240, 240))
            img.save(thumb_path, "WEBP")

    conn.execute(
        "UPDATE stickers SET path = ? WHERE id = ?",
        (str(new_path), stk_id),
    )
    conn.commit()
    return {"duplicate": False, "id": stk_id, "path": str(new_path), "desc": desc}


def _stickers_md_path() -> Path:
    from . import config
    return Path(config.db_pages_path()) / "stickers.md"


def _patch_md_line(sticker_id: int, desc: str) -> bool:
    md = _stickers_md_path()
    if not md.exists():
        return False
    anchor = f"<!-- id:{sticker_id} -->"
    pattern = re.compile(
        rf"^(- stk_\d+\s+).+?(\s*{re.escape(anchor)})$", re.MULTILINE
    )
    text = md.read_text()
    new_text, n = pattern.subn(rf"\g<1>{desc} \2", text)
    if n:
        md.write_text(new_text)
    return n > 0


def update_sticker(conn, sticker_id: int, desc: str) -> dict:
    row = conn.execute("SELECT id FROM stickers WHERE id = ?", (sticker_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "not_found"}
    conn.execute("UPDATE stickers SET desc = ? WHERE id = ?", (desc, sticker_id))
    conn.commit()
    _patch_md_line(sticker_id, desc)
    return {"ok": True, "id": sticker_id, "desc": desc}


def delete_sticker(conn, sticker_id: int) -> dict:
    row = conn.execute("SELECT path FROM stickers WHERE id = ?", (sticker_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "not_found"}
    path = row["path"]
    conn.execute("DELETE FROM stickers WHERE id = ?", (sticker_id,))
    conn.commit()
    p = Path(path)
    if p.exists():
        p.unlink()
    thumb = p.parent / "_thumb" / (p.stem + ".webp")
    if thumb.exists():
        thumb.unlink()
    return {"ok": True, "id": sticker_id, "deleted_path": path}
