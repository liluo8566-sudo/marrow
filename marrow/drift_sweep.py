"""drift_sweep — detect and apply reference updates when files are moved/renamed.

Trigger A: watchdog on_moved (same root) — src+dest captured directly.
Trigger B: cross-root mv inferred from deleted+created with same basename+size.
Trigger C: dangling delete — refs>0 write report, refs=0 silent drop.
Trigger D: CLI `mw drift scan <old> <new>` manual one-shot.

Per-op pending: each (src,dest) emits its own pending + alert (no merged batch).
30s window only dedups identical (src,dest) repeats. Safe refs (text exts,
path-shaped tokens, outside backup/venv) auto-apply with info alert; unsafe
refs stay pending with warn alert plus `mw drift apply <pid>` / `reject <pid>`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from marrow.paths import paths

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTHORIZED_ROOTS: list[Path] = [
    Path.home() / "CC-Lab",
    Path.home() / ".config",
    Path.home() / ".claude",
    Path.home() / "Toolkit",
    Path.home() / "Desktop" / "NY",
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Study",
]

BINARY_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".pdf", ".db", ".sqlite",
    ".sqlite-wal", ".sqlite-shm", ".pyc", ".zip", ".whl", ".tar",
    ".gz", ".dmg", ".so", ".dylib", ".o",
}

# Under ~/.claude only these top-level names are swept; everything else
# (projects/, image-cache/, file-history/, ...) is blacklisted.
CLAUDE_WHITELIST: set[str] = {
    "CLAUDE.md", "rules", "commands", "skills", "agents",
    "output-styles", "hooks", "keybindings.json", "settings.json",
}

# Under ~/.config most subdirs are user-managed config worth indexing;
# only blacklist ones with credentials or high-cardinality chat dumps.
CONFIG_BLACKLIST: set[str] = {"wechat-claude-bridge"}

_CLAUDE_ROOT = Path.home() / ".claude"
_CONFIG_ROOT = Path.home() / ".config"


def _claude_scope_ok(path: Path) -> bool:
    """Return True if path is allowed to be scanned.

    Paths NOT under ~/.claude always pass.
    Paths under ~/.claude pass only if their first segment after ~/.claude is
    in CLAUDE_WHITELIST (or they ARE ~/.claude itself).
    """
    try:
        rel = path.relative_to(_CLAUDE_ROOT)
    except ValueError:
        return True  # not under ~/.claude — allowed
    parts = rel.parts
    if not parts:
        return True  # ~/.claude itself — allowed
    return parts[0] in CLAUDE_WHITELIST

# Files we skip during ref scan / apply: binaries + append-only history +
# transient tool/editor lock/tmp/swap artefacts. cc session jsonl + log files
# quote old paths from past sessions as historical record; rewriting those
# would corrupt history without fixing any real reference.
SKIP_SCAN_EXTS = BINARY_EXTS | {
    ".jsonl", ".log", ".lock", ".swp", ".swo", ".tmp",
}

# Ref-scan exclude: drift_sweep skips these when looking for path references.
# Keep narrow — only directories that genuinely cannot contain user-managed
# references (build artifacts, VCS metadata, virtualenvs, prior backups).
EXCLUDE_DIRS_SCAN = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".DS_Store", "logs", "archives", "archive", "drift_backup",
    "drift_pending",
}

# Watcher pre-enqueue noise filter (path-segment match). Any event whose
# src/dest contains one of these parts is dropped before reaching
# DriftWatcher. Catches editor/git/test/venv churn that floods the queue.
NOISE_DIRS: set[str] = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    "drift_pending", "drift_backup", "logs", "archives", "archive",
}

# Atomic-write artefact patterns. Different tools use different schemes:
#   pytest: foo.py.tmp.13018.c7a3adecaf9d
#   marrow: .mrw.oap8rcgu  (hidden prefix)
#   python: atlas.cpython-313.pyc.4446290304   (numeric tail after real ext)
_ATOMIC_TMP_RE = re.compile(r"\.tmp\.\d+\.[0-9a-fA-F]+$")
_MRW_PREFIX_RE = re.compile(r"^\.mrw\.")
_NUMERIC_TAIL_RE = re.compile(r"\.\d+$")

# Suffix-substring exclusions for the ref-scan filename gate (in addition
# to exact-suffix SKIP_SCAN_EXTS). Catches `.bak-20260518-220058` and
# `.venv*.bak/...` artefacts that exact-suffix matching misses.
SKIP_SCAN_SUFFIX_PARTS: tuple[str, ...] = (".bak",)

# Dir-tree exclude: cosmetic — additionally hide cc / marrow runtime state
# whose contents are high-cardinality session/UUID noise that adds nothing
# to a structural overview. Ref scan still walks these (above set is narrower).
EXCLUDE_DIRS_TREE = EXCLUDE_DIRS_SCAN | {
    "file-history", "projects", "cache", "backups", "todos", "shell-snapshots",
    "session-env", "image-cache", "paste-cache", "jobs", "ide", "downloads",
    "tasks", "telemetry",
}

_BATCH_WINDOW_S = 30.0
_PENDING_TTL_S = 1800  # 30 min

# Safe-classify: which file extensions can be auto-applied without binary risk.
SAFE_AUTOAPPLY_EXTS: set[str] = {
    ".md", ".py", ".json", ".sh", ".toml", ".plist", ".yaml", ".yml", ".txt",
}

# Path-segment denylist for safe-classify: if the ref's file path includes
# any of these parts (or matches a glob prefix), it is NOT safe to auto-apply.
UNSAFE_PATH_PARTS: set[str] = {
    ".git", "__pycache__", "drift_pending", "drift_backup",
}
UNSAFE_PATH_PREFIX_PARTS: tuple[str, ...] = (".bak", ".venv")


def _path_excluded(p: str | Path) -> bool:
    """True if `p` lives under a NOISE_DIRS segment OR looks like an atomic-write
    artefact. Applied at the watcher edge before DriftWatcher sees the event."""
    try:
        parts = Path(p).parts
    except Exception:
        return False
    for part in parts:
        if part in NOISE_DIRS:
            return True
        # Glob-style: `.venv.py314.bak`, `.bak-20260518-220058` etc.
        if part.startswith(".venv") and part.endswith(".bak"):
            return True
    name = Path(p).name
    return _is_atomic_write_artifact(name)


def _is_atomic_write_artifact(name: str) -> bool:
    """True if `name` matches a known editor/test/runtime atomic-write scheme.

    Catches:
      - pytest:  foo.py.tmp.13018.c7a3adecaf9d
      - marrow:  .mrw.oap8rcgu  (hidden prefix)
      - python:  atlas.cpython-313.pyc.4446290304  (numeric tail after real ext)
    """
    if _MRW_PREFIX_RE.match(name):
        return True
    if _ATOMIC_TMP_RE.search(name):
        return True
    # Numeric tail after a real-looking extension: foo.pyc.4446290304
    m = _NUMERIC_TAIL_RE.search(name)
    if m:
        stripped = name[: m.start()]
        # Stripped form must have a recognisable ext (e.g. `.pyc`, `.so`).
        ext = Path(stripped).suffix.lower()
        if ext and ext in (BINARY_EXTS | {".jsonl", ".log", ".lock"}):
            return True
    return False


def _classify_refs(refs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition refs into (safe_to_auto_apply, unsafe_needs_review).

    A ref is safe iff ALL of:
      - file ext is in SAFE_AUTOAPPLY_EXTS
      - file path has NO UNSAFE_PATH_PARTS segment
      - no path segment starts with UNSAFE_PATH_PREFIX_PARTS (.bak, .venv)
      - matched text contains '/' (path-shaped slash token, not bare word)
    """
    safe: list[dict] = []
    unsafe: list[dict] = []
    for r in refs:
        fpath = Path(r.get("file", ""))
        text = r.get("text", "") or ""
        if fpath.suffix.lower() not in SAFE_AUTOAPPLY_EXTS:
            unsafe.append(r); continue
        bad_part = False
        for part in fpath.parts:
            if part in UNSAFE_PATH_PARTS:
                bad_part = True; break
            if part.startswith(UNSAFE_PATH_PREFIX_PARTS):
                bad_part = True; break
        if bad_part:
            unsafe.append(r); continue
        if "/" not in text:
            unsafe.append(r); continue
        safe.append(r)
    return safe, unsafe


# ---------------------------------------------------------------------------
# Path-shaped match detection
# ---------------------------------------------------------------------------

_PATH_RE = re.compile(
    r'(?<![/\w])'            # no leading path char
    r'('
    r'(?:[^\s"\'`]*[/][^\s"\'`]*)'     # contains /
    r'|(?:[^\s"\'`]+\.[a-zA-Z0-9]{1,10})'  # has extension
    r')'
    r'(?![/\w])'             # no trailing path char
)

_QUOTE_RE = re.compile(r'["\'\`]([^"\'\`]+)["\'\`]')


def _is_path_shaped(token: str) -> bool:
    """Return True if the token looks like a file path, not plain prose."""
    if "/" in token:
        return True
    if re.search(r'\.[a-zA-Z0-9]{1,10}$', token):
        return True
    return False


def _rg_binary() -> str | None:
    """Return path to rg binary, or None if not available as an executable."""
    import shutil
    return shutil.which("rg")


def _find_refs_rg(old_name: str, rg_bin: str, roots: list[Path]) -> list[dict] | None:
    """Run ripgrep search. Returns parsed refs list or None on failure."""
    args = [rg_bin, "--line-number", "--column", "--no-heading",
            "--color=never", "-e", old_name]
    for d in EXCLUDE_DIRS_SCAN:
        args += ["--glob", f"!{d}/**"]
    # Exclude any path segment that *contains* `.bak` (catches
    # `.bak-20260518-220058`, `.venv.py314.bak/`, `foo.db.bak` etc.).
    for part in SKIP_SCAN_SUFFIX_PARTS:
        args += ["--glob", f"!**/*{part}*/**", "--glob", f"!**/*{part}*"]
    for ext in SKIP_SCAN_EXTS:
        args += ["--glob", f"!*{ext}"]
    # For ~/.claude, only pass whitelisted sub-dirs as individual roots so
    # rg never descends into blacklisted siblings. For ~/.config, expand
    # to all sub-entries minus CONFIG_BLACKLIST (credentials, chat dumps).
    expanded: list[Path] = []
    for r in roots:
        if not r.exists():
            continue
        if r == _CLAUDE_ROOT:
            for name in CLAUDE_WHITELIST:
                child = r / name
                if child.exists():
                    expanded.append(child)
        elif r == _CONFIG_ROOT:
            try:
                for child in r.iterdir():
                    if child.name in CONFIG_BLACKLIST:
                        continue
                    expanded.append(child)
            except OSError:
                continue
        else:
            expanded.append(r)
    args += [str(r) for r in expanded]

    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        output = r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    refs: list[dict] = []
    for line in output.splitlines():
        parts = line.split(":", 3)
        if len(parts) < 4:
            continue
        fpath, lineno, col, text = parts[0], parts[1], parts[2], parts[3]
        if not _path_in_line(old_name, text):
            continue
        refs.append({"file": fpath, "line": int(lineno), "col": int(col), "text": text})
    return refs


def _find_refs_python(old_name: str, roots: list[Path]) -> list[dict]:
    """Pure-Python fallback: walk roots and grep for old_name in text files."""
    refs: list[dict] = []
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            cur = Path(dirpath)
            # Under ~/.claude: prune blacklisted top-level dirs immediately
            if cur == _CLAUDE_ROOT:
                dirnames[:] = [d for d in dirnames if d in CLAUDE_WHITELIST
                               and d not in EXCLUDE_DIRS_SCAN]
                continue
            # Under ~/.config: prune top-level credentials / chat dumps
            if cur == _CONFIG_ROOT:
                dirnames[:] = [d for d in dirnames
                               if d not in CONFIG_BLACKLIST
                               and d not in EXCLUDE_DIRS_SCAN]
                continue
            # Prune excluded dirs in-place; also drop any dir whose name
            # contains `.bak` (e.g. `.venv.py314.bak`, `.bak-2025…`).
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDE_DIRS_SCAN
                and not any(p in d for p in SKIP_SCAN_SUFFIX_PARTS)
            ]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                if not _claude_scope_ok(fpath):
                    continue
                if fpath.suffix.lower() in SKIP_SCAN_EXTS:
                    continue
                # Substring-style suffix skip (.bak-<ts>, .bak.NN etc.).
                if any(p in fname for p in SKIP_SCAN_SUFFIX_PARTS):
                    continue
                if _is_atomic_write_artifact(fname):
                    continue
                try:
                    if fpath.stat().st_size > 10 * 1024 * 1024:
                        continue
                    text_content = fpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for lineno, line in enumerate(text_content.splitlines(), 1):
                    if old_name in line and _path_in_line(old_name, line):
                        col = line.index(old_name) + 1
                        refs.append({
                            "file": str(fpath),
                            "line": lineno,
                            "col": col,
                            "text": line,
                        })
    return refs


def find_refs(old_name: str, roots: list[Path] | None = None) -> list[dict]:
    """Search all authorized roots for path-shaped occurrences of old_name.

    Tries ripgrep first; falls back to pure-Python walk if rg binary unavailable.
    Returns list of {file, line, col, text}.
    """
    if roots is None:
        roots = AUTHORIZED_ROOTS
    rg_bin = _rg_binary()
    if rg_bin:
        result = _find_refs_rg(old_name, rg_bin, roots)
        if result is not None:
            return result
    return _find_refs_python(old_name, roots)


def _path_in_line(name: str, text: str) -> bool:
    """Check that `name` in `text` appears in a path-shaped context."""
    # Check quoted occurrences first
    for m in _QUOTE_RE.finditer(text):
        if name in m.group(1) and _is_path_shaped(m.group(1)):
            return True
    # Check unquoted tokens with / or extension
    for token in re.split(r'\s+', text):
        if name in token and _is_path_shaped(token):
            return True
    return False


# ---------------------------------------------------------------------------
# Pending queue storage
# ---------------------------------------------------------------------------

def _pending_dir() -> Path:
    d = Path(str(paths.drift_pending_dir))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backup_dir() -> Path:
    d = Path(str(paths.drift_backup_dir))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_id(src: str, dest: str) -> str:
    raw = f"{src}:{dest}:{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def write_pending(src: str, dest: str, refs: list[dict]) -> str:
    """Write a drift pending JSON and return its id."""
    pid = _make_id(src, dest)
    preview_lines = [r["text"][:120] for r in refs[:5]]
    payload = {
        "id": pid,
        "src": src,
        "dest": dest,
        "refs": refs,
        "diff_preview": preview_lines,
        "created_at": time.time(),
    }
    pending_path = _pending_dir() / f"{pid}.json"
    pending_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return pid


def load_pending(pid: str) -> dict | None:
    p = _pending_dir() / f"{pid}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def delete_pending(pid: str) -> None:
    p = _pending_dir() / f"{pid}.json"
    p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Alert emission
# ---------------------------------------------------------------------------

def _emit_alert(message: str, source: str = "drift_sweep",
                severity: str = "warn") -> None:
    """Write to alerts table via repo.add_alert. Tolerate missing DB."""
    try:
        from marrow import repo
        repo.add_alert(severity, "drift_sweep", message, source)
    except Exception:
        pass  # standalone / test context without DB — silently skip


# ---------------------------------------------------------------------------
# Dir tree refresh
# ---------------------------------------------------------------------------

def refresh_dir_tree(roots: list[Path] | None = None) -> None:
    """Regenerate ~/.config/marrow/dir_tree.md (dirs-only, max-depth=2)."""
    if roots is None:
        roots = AUTHORIZED_ROOTS
    lines: list[str] = ["# dir_tree", ""]

    for root in roots:
        if not root.exists():
            continue
        lines.append(f"## {root}")
        lines.extend(_tree_lines(root, "", depth=0, max_depth=2))
        lines.append("")

    out = Path(str(paths.dir_tree_md))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


def _tree_lines(path: Path, prefix: str, depth: int, max_depth: int) -> list[str]:
    """Dirs-only overview tree. Files are omitted; grep finds files, this is
    a structural skeleton ("which kind of stuff lives where")."""
    if depth >= max_depth:
        return []
    try:
        entries = sorted(p for p in path.iterdir() if p.is_dir())
    except PermissionError:
        return []
    entries = [e for e in entries if _include_entry(e)]
    result: list[str] = []
    for i, entry in enumerate(entries):
        connector = "└── " if i == len(entries) - 1 else "├── "
        result.append(f"{prefix}{connector}{entry.name}/")
        ext_prefix = prefix + ("    " if i == len(entries) - 1 else "│   ")
        result.extend(_tree_lines(entry, ext_prefix, depth + 1, max_depth))
    return result


def _include_entry(p: Path) -> bool:
    if p.name in EXCLUDE_DIRS_TREE:
        return False
    if not _claude_scope_ok(p):
        return False
    if p.is_file():
        if p.suffix.lower() in SKIP_SCAN_EXTS:
            return False
        try:
            if p.stat().st_size > 10 * 1024 * 1024:
                return False
        except OSError:
            return False
    return True


# ---------------------------------------------------------------------------
# Apply / Reject
# ---------------------------------------------------------------------------

def _is_in_git_repo(path: Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=str(path.parent),
        )
        return r.returncode == 0
    except Exception:
        return False


def _atomic_replace(file_path: Path, old: str, new: str) -> bool:
    """Replace all path-shaped occurrences of old with new. Return True if changed."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    new_text = _replace_path_refs(text, old, new)
    if new_text == text:
        return False
    tmp = file_path.with_suffix(file_path.suffix + ".drifttmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(file_path)
    return True


def _replace_path_refs(text: str, old: str, new: str) -> str:
    """Replace `old` with `new` only where it appears in a path-shaped context."""
    result = []
    i = 0
    while True:
        idx = text.find(old, i)
        if idx == -1:
            result.append(text[i:])
            break
        # Check word boundary on left
        left_ok = idx == 0 or not (text[idx - 1].isalnum() or text[idx - 1] in "_-")
        # Check word boundary on right
        end = idx + len(old)
        right_ok = end == len(text) or not (text[end].isalnum() or text[end] in "_-")
        # Check path context — look at surrounding token
        line_start = text.rfind("\n", 0, idx) + 1
        line_end = text.find("\n", idx)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        in_path_ctx = _path_in_line(old, line)
        if left_ok and right_ok and in_path_ctx:
            result.append(text[i:idx])
            result.append(new)
            i = end
        else:
            result.append(text[i:idx + 1])
            i = idx + 1
    return "".join(result)


def apply_confirm(pid: str, roots: list[Path] | None = None) -> dict[str, Any]:
    """Apply a pending drift operation. Returns summary dict."""
    data = load_pending(pid)
    if data is None:
        return {"ok": False, "error": f"no pending: {pid}"}

    src, dest = data["src"], data["dest"]
    refs = data.get("refs", [])
    backup_base = _backup_dir() / pid
    changed_files: list[str] = []
    errors: list[str] = []

    old_name = Path(src).name
    new_name = Path(dest).name
    files_with_refs = list({r["file"] for r in refs})
    for fstr in files_with_refs:
        fpath = Path(fstr)
        if not fpath.exists():
            continue
        if _is_in_git_repo(fpath):
            subprocess.run(["git", "add", str(fpath)], capture_output=True)
        else:
            try:
                backup_target = backup_base / fpath.name
                backup_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fpath, backup_target)
            except OSError as e:
                errors.append(f"backup {fpath}: {e}")
        if _atomic_replace(fpath, old_name, new_name):
            changed_files.append(str(fpath))

    delete_pending(pid)
    refresh_dir_tree(roots)
    return {"ok": True, "changed": changed_files, "errors": errors}


def apply_reject(pid: str) -> dict[str, Any]:
    """Discard a pending drift op without touching files."""
    data = load_pending(pid)
    if data is None:
        return {"ok": False, "error": f"no pending: {pid}"}
    delete_pending(pid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Scan and queue (core trigger logic)
# ---------------------------------------------------------------------------

def _fmt_files(files: list[str], cap: int = 5) -> str:
    """Render a short comma-joined preview of basenames for alert messages."""
    names = [Path(f).name for f in files]
    head = names[:cap]
    extra = len(names) - cap
    out = ", ".join(head)
    if extra > 0:
        out += f", ...+{extra}"
    return out


def _scan_and_queue(src: str, dest: str, roots: list[Path] | None = None) -> str | None:
    """Ripgrep for old basename. If all refs are safe → auto-apply + info alert.
    If any unsafe → keep pending + warn alert with reviewer hint. Returns pid
    (or None if no refs)."""
    old_name = Path(src).name
    new_name = Path(dest).name
    if not old_name:
        return None
    refs = find_refs(old_name, roots)
    if not refs:
        return None  # no refs → silent drop
    safe, unsafe = _classify_refs(refs)

    if unsafe:
        # Keep all refs in pending so the user can review the whole picture.
        pid = write_pending(src, dest, refs)
        unsafe_files = sorted({r["file"] for r in unsafe})
        _emit_alert(
            f"drift review: {old_name} → {new_name} · "
            f"{len(safe)} safe · {len(unsafe)} unsafe "
            f"({_fmt_files(unsafe_files)}) · "
            f"mw drift apply {pid} | reject {pid}",
            severity="warn",
        )
        refresh_dir_tree(roots)
        return pid

    # All refs safe → write pending then apply immediately.
    pid = write_pending(src, dest, refs)
    safe_files = sorted({r["file"] for r in safe})
    apply_confirm(pid, roots=roots)
    _emit_alert(
        f"drift applied: {old_name} → {new_name} in "
        f"{len(safe_files)} files ({_fmt_files(safe_files)})",
        severity="info",
    )
    return pid


def handle_move(src: str, dest: str, roots: list[Path] | None = None) -> str | None:
    """Trigger A/D: explicit move/rename detected. Returns pending id."""
    return _scan_and_queue(src, dest, roots)


def handle_dangling_delete(src: str, roots: list[Path] | None = None) -> str | None:
    """Trigger C: deleted file with no matching create. Write report if refs>0."""
    old_name = Path(src).name
    refs = find_refs(old_name, roots)
    if not refs:
        return None  # refs=0 → silent drop
    # Write report but use dest='' to signal no auto-replace
    pid = write_pending(src, "", refs)
    n_files = len({r["file"] for r in refs})
    _emit_alert(
        f"drift dangling: {old_name} deleted "
        f"[{len(refs)} refs in {n_files} files] — manual review needed",
        severity="warn",
    )
    return pid


# ---------------------------------------------------------------------------
# Watcher integration — basename cache + cross-root inference
# ---------------------------------------------------------------------------

class DriftWatcher:
    """Tracks file moves across roots. Attach to watchdog observer externally."""

    def __init__(self, roots: list[Path] | None = None,
                 batch_window: float = _BATCH_WINDOW_S,
                 ttl: float = _PENDING_TTL_S) -> None:
        self._roots = roots or AUTHORIZED_ROOTS
        self._batch_window = batch_window
        self._ttl = ttl
        # basename → {path, size, mtime, hash}
        self._cache: dict[str, dict] = {}
        # deleted queue: basename → {path, size, ts}
        self._deleted: dict[str, dict] = {}
        self._lock = threading.Lock()
        # batch accumulator
        self._batch: list[tuple[str, str]] = []
        self._batch_timer: threading.Timer | None = None

    def on_moved(self, src: str, dest: str) -> None:
        """Trigger A: same-root rename/move."""
        if (Path(src).suffix.lower() in SKIP_SCAN_EXTS
                or Path(dest).suffix.lower() in SKIP_SCAN_EXTS):
            return
        if _path_excluded(src) or _path_excluded(dest):
            return
        with self._lock:
            self._cache[Path(dest).name] = self._stat(dest)
            self._deleted.pop(Path(src).name, None)
            self._queue_batch(src, dest)

    def on_deleted(self, path: str) -> None:
        if Path(path).suffix.lower() in SKIP_SCAN_EXTS:
            return
        if _path_excluded(path):
            return
        st = self._stat_safe(path)
        with self._lock:
            self._deleted[Path(path).name] = {
                "path": path, "size": st.get("size", -1), "ts": time.time()
            }

    def on_created(self, path: str) -> None:
        if Path(path).suffix.lower() in SKIP_SCAN_EXTS:
            return
        if _path_excluded(path):
            return
        p = Path(path)
        name = p.name
        with self._lock:
            # Prune stale TTL entries
            now = time.time()
            self._deleted = {
                k: v for k, v in self._deleted.items()
                if now - v["ts"] < self._ttl
            }
            deleted_entry = self._deleted.pop(name, None)
        st = self._stat(path)
        if deleted_entry and deleted_entry["size"] == st.get("size", -2):
            # Trigger B: cross-root move inferred
            with self._lock:
                self._queue_batch(deleted_entry["path"], path)
        with self._lock:
            self._cache[name] = st

    def _queue_batch(self, src: str, dest: str) -> None:
        """Add op to batch; start/reset 30s timer."""
        self._batch.append((src, dest))
        if self._batch_timer is not None:
            self._batch_timer.cancel()
        t = threading.Timer(self._batch_window, self._flush_batch)
        t.daemon = True
        self._batch_timer = t
        t.start()

    def _flush_batch(self) -> None:
        """Emit one pending + one alert per distinct (src, dest) — no merging.

        The 30s window only dedups identical repeats; different ops each get
        their own alert so the user sees "what renamed to what" per line.
        """
        with self._lock:
            ops = list(self._batch)
            self._batch.clear()
            self._batch_timer = None
        if not ops:
            return
        seen: set[tuple[str, str]] = set()
        for src, dest in ops:
            key = (src, dest)
            if key in seen:
                continue
            seen.add(key)
            handle_move(src, dest, self._roots)

    def flush_dangling(self) -> None:
        """Process TTL-expired deleted entries as dangling deletes."""
        now = time.time()
        with self._lock:
            expired = {
                k: v for k, v in self._deleted.items()
                if now - v["ts"] >= self._ttl
            }
            for k in expired:
                del self._deleted[k]
        for entry in expired.values():
            handle_dangling_delete(entry["path"], self._roots)

    def _stat(self, path: str) -> dict:
        try:
            st = os.stat(path)
            return {"size": st.st_size, "mtime": st.st_mtime}
        except OSError:
            return {"size": -1, "mtime": -1}

    def _stat_safe(self, path: str) -> dict:
        return self._stat(path)
