"""Atomic file-write helper shared by daybrief / inserter / subpages."""
import os
import tempfile


def atomic_write(path: str, data: str, *, prefix: str = ".mrw.") -> None:
    # Resolve symlinks so os.replace lands on the symlink's true target
    # rather than replacing the symlink itself. tempfile dir then sits on
    # the same filesystem as the final file.
    path = os.path.realpath(path)
    # Content-equality guard: skip the os.replace when the on-disk bytes
    # already match. Prevents phantom mtime bumps that otherwise trap the
    # sync loop in a "within epsilon" deadlock (db change never reflected
    # back into md because every prior render already pushed md_mtime
    # within 1s of db_mtime).
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                if f.read() == data:
                    return
        except OSError:
            pass  # fall through to normal write on read error
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=prefix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
