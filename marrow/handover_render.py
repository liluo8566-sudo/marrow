"""Handover renderer. Single writer = sessionend_async (Bug #1).

State-axis handover: 4 sections (Done / Open / Plan / Reference). flat
bullets, oldest top → newest bottom. tombstone filter strips bullets the
user removed from prior handover.md so sonnet's re-emission cannot revive
them. snapshot the prior body to audit_log before overwrite for rollback.

Output: DATA_DIR/handover.md.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import config
from .dashboard import _atomic_write
from .handover_norm import bullet_lines
from .tombstone import (MdIndexTombstoneStore, TombstoneStore,
                        filter_tombstoned, record_user_deletes)

_TEMPLATE_PATH = Path(__file__).parent / "handover_template.md"

# Sandwich markers from the template (top-section host for dashboard sync).
_SEP_OPEN = "<!-- marrow:top:start -->"
_SEP_CLOSE = "<!-- marrow:top:end -->"

_RENDERED_PATH = config.DATA_DIR / "handover.md"

_SECTIONS = ("Done", "Open", "Plan", "Reference")

_LOCK_RETRIES = 3
_LOCK_BACKOFF = 0.05


# ── template helpers ────────────────────────────────────────────────────────

def _strip_instruction_lines(text: str) -> str:
    """Remove `> ` system instruction lines. Preserve trailing newline so \
downstream regex inject points keep their `\\n` anchor."""
    kept = [ln for ln in text.splitlines() if not ln.startswith("> ")]
    out = "\n".join(kept)
    if text.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


def _inject_section(text: str, header: str, body: str) -> str:
    """Replace body under `## <header>` up to next `## ` or HTML comment."""
    if not body:
        return text
    pat = re.compile(
        rf"(^## {re.escape(header)}[ \t]*\n)(.*?)(?=^## |^<!--|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    return pat.sub(lambda m: f"{m.group(1)}{body}\n\n", text, count=1)


def _split_section_body(text: str, header: str) -> str:
    """Extract body under `## <header>`, stripped. Empty string if missing."""
    pat = re.compile(
        rf"^## {re.escape(header)}[ \t]*\n(.*?)(?=^## |^<!--|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pat.search(text)
    if not m:
        return ""
    return m.group(1).strip("\n")


def render_skeleton(conn: sqlite3.Connection) -> str:
    """Build template body without top-section markers, no stamp, current ts."""
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    template = _strip_instruction_lines(template)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    template = template.replace("{{YYYY-MM-DD HH:MM}}", now_str)
    # Strip the entire top section (markers + content between them).
    i = template.find(_SEP_OPEN)
    j = template.find(_SEP_CLOSE)
    if i != -1 and j != -1 and j > i:
        template = template[:i] + template[j + len(_SEP_CLOSE):]
    return template.lstrip("\n")


def _append_stamp(text: str, stamp: str) -> str:
    if not text.endswith("\n"):
        text += "\n"
    return text + stamp + "\n"


def _none_or(body: str) -> str:
    s = (body or "").strip()
    if not s or s.upper() == "N/A":
        return "- N/A"
    return s


# ── audit snapshot ─────────────────────────────────────────────────────────

def _write_snapshot_audit(conn: sqlite3.Connection, sid: str, prior: str) -> None:
    """Persist the pre-overwrite handover.md body to audit_log for rollback."""
    if not prior:
        return
    digest = hashlib.sha256(prior.encode("utf-8")).hexdigest()
    head = prior[:200].replace("\n", "\\n")
    summary = f"sha256={digest} head={head} body={prior}"
    try:
        with conn:
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('handover', ?, 'handover_snapshot', ?)",
                (sid, summary),
            )
    except sqlite3.Error:
        pass


def _load_last_snapshot_body(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE target_table='handover' AND action='handover_snapshot'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row or not row["summary"]:
        return ""
    i = row["summary"].find("body=")
    return row["summary"][i + 5:] if i >= 0 else ""


# ── flock ───────────────────────────────────────────────────────────────────

def _acquire_flock(path: Path):
    """LOCK_EX with 3x 50ms backoff. Returns open fd or None."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    for attempt in range(_LOCK_RETRIES):
        fd = open(path, "r+", encoding="utf-8")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError) as e:
            fd.close()
            if isinstance(e, OSError) and not isinstance(e, BlockingIOError):
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    return None
            if attempt == _LOCK_RETRIES - 1:
                return None
            time.sleep(_LOCK_BACKOFF)
    return None


def _release_flock(fd) -> None:
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


# ── tombstone-aware bullet filter ───────────────────────────────────────────

def _apply_tombstones(body: str, tombstones: set[str]) -> str:
    """Walk a section body; drop bullets whose normalized hash is tombstoned.
    Empty result → `- N/A`."""
    if not tombstones:
        return _none_or(body)
    kept = filter_tombstoned(bullet_lines(body), tombstones)
    return "\n".join(kept) if kept else "- N/A"


# ── public render / write ──────────────────────────────────────────────────

def render_full(conn: sqlite3.Connection, sid: str,
                *, done: str, open_: str, plan: str, reference: str,
                tombstones: set[str] | None = None,
                now_epoch: int | None = None) -> str:
    """Compose skeleton + 4 state-axis sections + ready stamp."""
    if now_epoch is None:
        now_epoch = int(time.time())
    tombs = tombstones if tombstones is not None else set()
    bodies = {
        "Done":      _apply_tombstones(done, tombs),
        "Open":      _apply_tombstones(open_, tombs),
        "Plan":      _apply_tombstones(plan, tombs),
        "Reference": _apply_tombstones(reference, tombs),
    }
    text = render_skeleton(conn)
    for header in _SECTIONS:
        text = _inject_section(text, header, bodies[header])
    stamp = f"<!-- handover: ready sid:{sid} ts:{now_epoch} -->"
    return _append_stamp(text, stamp)


def _new_store(conn: sqlite3.Connection) -> TombstoneStore:
    """MdIndex-backed bullet store, bound to handover.md absolute path."""
    return MdIndexTombstoneStore(conn, str(_RENDERED_PATH))


def write_handover_full(conn: sqlite3.Connection, sid: str,
                        *, done: str, open_: str, plan: str,
                        reference: str = "") -> Path:
    """Sessionend single-writer: flock + diff prior → tombstone user-removed
    bullets + filter sections + atomic write. Lock-loss falls back to
    handover.md.partial.<sid> with audit row, never crashes."""
    now_epoch = int(time.time())
    fd = _acquire_flock(_RENDERED_PATH)
    if fd is None:
        store = _new_store(conn)
        partial = _RENDERED_PATH.with_suffix(f".md.partial.{sid}")
        text = render_full(conn, sid, done=done, open_=open_, plan=plan,
                           reference=reference,
                           tombstones=store.list_tombstones(),
                           now_epoch=now_epoch)
        _atomic_write(str(partial), text)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO audit_log (target_table, target_id, action, summary)"
                    " VALUES ('handover', ?, 'handover_lock_failed', ?)",
                    (sid, f"partial={partial.name}"),
                )
        except sqlite3.Error:
            pass
        return partial
    try:
        try:
            prior_text = _RENDERED_PATH.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            prior_text = ""
        store = _new_store(conn)
        # Snapshot = "what marrow last wrote." Compare against `prior_text`
        # (what's on disk right now) to detect Lumi's hand-edits since.
        last_body = _load_last_snapshot_body(conn)
        if last_body and prior_text and last_body.strip() != prior_text.strip():
            record_user_deletes(store, last_body, prior_text)
        text = render_full(conn, sid, done=done, open_=open_, plan=plan,
                           reference=reference,
                           tombstones=store.list_tombstones(),
                           now_epoch=now_epoch)
        # Snapshot the body we are about to write — that becomes the canonical
        # "last auto-write" against which the next turn's user-edit diff runs.
        # Also write a separate row recording the file we just overwrote, so
        # rollback / audit can still find the pre-overwrite state.
        _write_snapshot_audit(conn, sid, text)
        if prior_text and prior_text.strip() != text.strip():
            _write_overwritten_audit(conn, sid, prior_text)
        _atomic_write(str(_RENDERED_PATH), text)
    finally:
        _release_flock(fd)
    return _RENDERED_PATH


def _write_overwritten_audit(conn: sqlite3.Connection, sid: str,
                              prior: str) -> None:
    """Record the body we just overwrote, separate from the canonical snapshot.
    `_load_last_snapshot_body` reads only `handover_snapshot` rows; this row
    uses a different action so rollback can find the pre-overwrite text
    without polluting the diff baseline."""
    if not prior:
        return
    digest = hashlib.sha256(prior.encode("utf-8")).hexdigest()
    head = prior[:200].replace("\n", "\\n")
    summary = f"sha256={digest} head={head} body={prior}"
    try:
        with conn:
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('handover', ?, 'handover_overwritten', ?)",
                (sid, summary),
            )
    except sqlite3.Error:
        pass
