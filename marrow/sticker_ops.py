from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

STICKERS_DIR: Path | None = None  # None = resolve at call time from config
_CANVAS = 240
_STICKER_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _resolve_stickers_dir() -> Path:
    """Return STICKERS_DIR if set (e.g. monkeypatched in tests), otherwise
    read from config [paths].stickers_dir, falling back via ny_root."""
    if STICKERS_DIR is not None:
        return Path(STICKERS_DIR).expanduser()
    from . import config as _config
    val = _config.load().get("paths", {}).get("stickers_dir", "")
    if val:
        return Path(val).expanduser()
    from .paths import paths as _mpaths
    if _mpaths.ny_root != Path(""):
        return _mpaths.ny_root / "stickers"
    return Path.home() / ".config" / "marrow" / "stickers"


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


def _standardize_image(path: Path) -> Path:
    """Convert to PNG, preserve original resolution. Skips GIF."""
    if path.suffix.lower() == ".gif":
        return path
    out = path.with_suffix(".png") if path.suffix.lower() in (".jpg", ".jpeg") else path
    try:
        from PIL import Image
        with Image.open(path) as img:
            if img.mode not in ("RGBA", "RGB"):
                img = img.convert("RGBA")
            img.save(out, "PNG")
        if out != path and path.exists():
            path.unlink()
        return out
    except Exception as e:
        logger.warning("sticker standardize failed for %s: %s", path.name, e)
        return out if out.exists() else path


def ingest_sticker(conn, src_path: str, desc: str, source: str = "wechat") -> dict:
    src = Path(src_path).expanduser()
    digest = sha256_file(str(src))
    row = conn.execute(
        "SELECT id, path FROM stickers WHERE sha256 = ? LIMIT 1", (digest,)
    ).fetchone()
    if row:
        return {"duplicate": True, "existing_id": row["id"], "path": row["path"]}

    phash = phash_file(str(src))
    if phash:
        rows = conn.execute(
            "SELECT id, phash, path FROM stickers WHERE phash IS NOT NULL"
        ).fetchall()
        for existing in rows:
            dist = _hamming(phash, existing["phash"])
            if dist is not None and dist <= 8:
                return {
                    "duplicate": True,
                    "existing_id": existing["id"],
                    "near_dup": True,
                    "path": existing["path"],
                }

    stickers_dir = _resolve_stickers_dir()
    thumb_dir = stickers_dir / "_thumb"
    stickers_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    cursor = conn.execute(
        "INSERT INTO stickers(path, sha256, phash, desc, source, updated_at)"
        " VALUES(?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        ("_pending", digest, phash, desc, source),
    )
    stk_id = cursor.lastrowid

    ext = src.suffix
    new_path = stickers_dir / f"stk_{stk_id:03d}{ext}"
    if src.resolve() != new_path.resolve():
        shutil.copy2(src, new_path)
    new_path = _standardize_image(new_path)

    thumb_path = thumb_dir / f"stk_{stk_id:03d}.webp"
    try:
        from PIL import Image
    except ImportError:
        pass
    else:
        with Image.open(new_path) as img:
            img.thumbnail((240, 240))
            img.save(thumb_path, "WEBP")

    try:
        conn.execute(
            "UPDATE stickers SET path = ? WHERE id = ?",
            (str(new_path), stk_id),
        )
        conn.commit()
    except Exception:
        if thumb_path.exists():
            thumb_path.unlink(missing_ok=True)
        raise
    return {"duplicate": False, "id": stk_id, "path": str(new_path), "desc": desc}


def update_sticker(conn, sticker_id: int, desc: str) -> dict:
    row = conn.execute("SELECT id, desc FROM stickers WHERE id = ?", (sticker_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "not_found"}
    old_desc = row["desc"]
    conn.execute(
        "UPDATE stickers SET desc = ?,"
        " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        " WHERE id = ?",
        (desc, sticker_id),
    )
    conn.commit()
    return {"ok": True, "id": sticker_id, "desc": desc, "old_desc": old_desc}


def _unlink_sticker_files(path: str) -> None:
    """Remove a sticker's image + thumbnail from disk (missing-safe)."""
    p = Path(path)
    if p.exists():
        p.unlink()
    thumb = p.parent / "_thumb" / (p.stem + ".webp")
    if thumb.exists():
        thumb.unlink()


def _trash_one(p: Path) -> None:
    """Move a single file to the user Trash; fall back to a /tmp move if
    /usr/bin/trash is unavailable or fails. Missing-safe."""
    import subprocess
    import time

    if not p.exists():
        return
    try:
        result = subprocess.run(["/usr/bin/trash", str(p)],
                                capture_output=True, text=True)
        if result is not None and result.returncode == 0:
            return
    except OSError:
        pass
    if not p.exists():
        return  # trash succeeded despite a non-zero/missing result report
    dest = Path("/tmp") / f"marrow-sticker-trash-{int(time.time())}-{p.name}"
    p.rename(dest)


def _trash_sticker_files(path: str) -> None:
    """Move a sticker's image + thumbnail to Trash instead of hard-deleting."""
    p = Path(path)
    _trash_one(p)
    thumb = p.parent / "_thumb" / (p.stem + ".webp")
    _trash_one(thumb)


def delete_sticker(conn, sticker_id: int) -> dict:
    row = conn.execute("SELECT path, desc FROM stickers WHERE id = ?", (sticker_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "not_found"}
    path = row["path"]
    desc = row["desc"]
    conn.execute("DELETE FROM stickers WHERE id = ?", (sticker_id,))
    conn.commit()
    _trash_sticker_files(path)
    return {"ok": True, "id": sticker_id, "desc": desc, "deleted_path": path}


def sweep_orphans(conn) -> list[int]:
    """Remove DB entries whose sticker file no longer exists on disk."""
    rows = conn.execute("SELECT id, path FROM stickers").fetchall()
    removed = []
    for r in rows:
        if not Path(r["path"]).exists():
            conn.execute("DELETE FROM stickers WHERE id = ?", (r["id"],))
            thumb = Path(r["path"]).parent / "_thumb" / (Path(r["path"]).stem + ".webp")
            if thumb.exists():
                thumb.unlink()
            removed.append(r["id"])
    if removed:
        conn.commit()
        from . import config as _config
        _purge_md_lines(_config.db_pages_path(), removed)
    return removed


def _purge_md_lines(pages_root: str, ids: list[int]) -> None:
    """Remove anchored lines for given ids directly from stickers.md.

    The inserter never deletes md content (by design), so sweep_orphans
    must strip orphan lines itself rather than relying on write_subpage.
    """
    md = Path(pages_root) / "stickers.md"
    if not md.exists():
        return
    import re as _re
    text = md.read_text(encoding="utf-8")
    id_set = set(ids)
    new_lines = []
    for line in text.splitlines(True):
        m = _re.search(r"<!-- id:(\d+) -->", line)
        if m and int(m.group(1)) in id_set:
            continue
        new_lines.append(line)
    md.write_text("".join(new_lines), encoding="utf-8")


def sweep_file_orphans(conn) -> list[int]:
    """Re-register stk_NNN files on disk that have no DB row."""
    stickers_dir = _resolve_stickers_dir()
    if not stickers_dir.exists():
        return []
    db_ids = {r["id"] for r in conn.execute("SELECT id FROM stickers").fetchall()}
    db_phashes = {}
    for r in conn.execute("SELECT id, phash FROM stickers WHERE phash IS NOT NULL"):
        db_phashes[r["phash"]] = r["id"]

    registered = []
    for f in sorted(stickers_dir.iterdir()):
        if f.is_dir() or f.suffix.lower() not in _STICKER_EXTS:
            continue
        m = re.match(r"stk_(\d{3,})", f.stem)
        if not m:
            continue
        stk_id = int(m.group(1))
        if stk_id in db_ids:
            continue
        ph = phash_file(str(f))
        if ph and ph in db_phashes:
            f.unlink()
            logger.info("sweep_file_orphans: deleted dup %s (matches id=%d)", f.name, db_phashes[ph])
            continue
        sha = sha256_file(str(f))
        conn.execute(
            "INSERT INTO stickers(id, path, sha256, phash, desc, source, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (stk_id, str(f), sha, ph, "(pending)", "finder"),
        )
        registered.append(stk_id)

    if registered:
        conn.commit()
    return registered
