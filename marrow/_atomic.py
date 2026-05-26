"""Atomic file-write helper shared by dashboard / inserter / subpages."""
import os
import tempfile


def atomic_write(path: str, data: str, *, prefix: str = ".mrw.") -> None:
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
