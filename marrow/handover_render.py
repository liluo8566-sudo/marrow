"""Handover renderer. Single writer = sessionend_async (Bug #1).
Phase A: flock-guarded read-modify-write merges multi-session ThisSession
(<2h together, 2h+ pushed to ## Previous Sessions), unions NextSession,
snapshots prior body to audit_log for rollback. Output: DATA_DIR/handover.md.
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
from . import top_sections
from .dashboard import _atomic_write

_TEMPLATE_PATH = Path(__file__).parent / "handover_template.md"

# Sandwich markers from the template
_SEP_OPEN = "<!-- marrow:top:start -->"
_SEP_CLOSE = "<!-- marrow:top:end -->"

_RENDERED_PATH = config.DATA_DIR / "handover.md"

_TS_HEADING_RE = re.compile(r"^###\s+\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*$")
_FOOTER_TS_RE = re.compile(r"<!--\s*handover:\s*ready\s+sid:\S+\s+ts:(\d+)\s*-->")
_TOP_STAMP_RE = re.compile(r"^# Marrow handover — (\d{4}-\d{2}-\d{2} \d{2}:\d{2})")

_WINDOW_SEC = 2 * 3600
_LOCK_RETRIES = 3
_LOCK_BACKOFF = 0.05


def _strip_instruction_lines(text: str) -> str:
    """Remove lines starting with '> ' (system instruction lines). Preserve \
trailing newline so downstream regex inject points keep their `\\n` anchor."""
    kept = [ln for ln in text.splitlines() if not ln.startswith("> ")]
    out = "\n".join(kept)
    if text.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


def _replace_top_sections(text: str, rendered: str) -> str:
    """Replace content between _SEP_OPEN and _SEP_CLOSE with rendered block."""
    i = text.find(_SEP_OPEN)
    j = text.find(_SEP_CLOSE)
    if i == -1 or j == -1 or j <= i:
        return rendered + "\n\n" + text
    before = text[:i + len(_SEP_OPEN)]
    after = text[j:]
    return before + "\n" + rendered + "\n" + after


def _inject_section(text: str, header: str, body: str) -> str:
    """Replace body under `## <header>` up to next `## ` or HTML comment."""
    if not body:
        return text
    pat = re.compile(
        rf"(^## {re.escape(header)}[ \t]*\n)(.*?)(?=^## |^<!--|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    return pat.sub(lambda m: f"{m.group(1)}{body}\n\n", text, count=1)


def render_skeleton(conn: sqlite3.Connection) -> str:
    """Build template body with top sections + commits, no stamp, empty bullets."""
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    template = _strip_instruction_lines(template)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    template = template.replace("{{YYYY-MM-DD HH:MM}}", now_str)
    top = top_sections.render_top(conn)
    template = _replace_top_sections(template, top)
    return template


def _append_stamp(text: str, stamp: str) -> str:
    if not text.endswith("\n"):
        text += "\n"
    return text + stamp + "\n"


# ── parse + merge ──────────────────────────────────────────────────────────

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


def _split_timed_segments(body: str, fallback_ts: str) -> list[tuple[str, str]]:
    """Split body by `### [ts]` sub-headings. Legacy untimed body → one fallback segment."""
    body = (body or "").strip()
    if not body or body.lower() == "- none":
        return []
    segments: list[tuple[str, str]] = []
    cur_ts, cur, leading = None, [], []
    for ln in body.splitlines():
        m = _TS_HEADING_RE.match(ln)
        if m:
            if cur_ts is not None and "\n".join(cur).strip():
                segments.append((cur_ts, "\n".join(cur).strip()))
            cur_ts, cur = m.group(1), []
        elif cur_ts is None:
            leading.append(ln)
        else:
            cur.append(ln)
    if cur_ts is not None and "\n".join(cur).strip():
        segments.append((cur_ts, "\n".join(cur).strip()))
    lead = "\n".join(leading).strip()
    if lead:
        segments.insert(0, (fallback_ts, lead))
    return segments


def _parse_footer_ts(text: str) -> int | None:
    m = _FOOTER_TS_RE.search(text)
    return int(m.group(1)) if m else None


def _parse_top_stamp(text: str) -> str | None:
    for line in text.splitlines()[:5]:
        m = _TOP_STAMP_RE.match(line)
        if m:
            return m.group(1)
    return None


def _ts_label_to_epoch(label: str) -> int | None:
    try:
        return int(datetime.strptime(label, "%Y-%m-%d %H:%M").timestamp())
    except ValueError:
        return None


def _now_label(now_epoch: int) -> str:
    return datetime.fromtimestamp(now_epoch).strftime("%Y-%m-%d %H:%M")


def _normalize_bullets(text: str) -> list[str]:
    """Return non-empty lines, stripped. Used for dedup union."""
    return [ln for ln in (text or "").splitlines() if ln.strip()]


def _extract_done_prefixes(this_done: str) -> list[str]:
    """THIS_DONE bullets → 80-char verbatim prefixes for delete match.
    Skip short / N/A stubs to avoid mass deletion."""
    out: list[str] = []
    for ln in _normalize_bullets(this_done):
        body = ln.lstrip("-* ").strip()
        if len(body) < 20 or body.upper() in {"N/A", "NONE"}:
            continue
        out.append(body[:80])
    return out


def _filter_bullets_by_done(body: str, prefixes: list[str]) -> str:
    """Drop bullets whose body starts with any done prefix. Used on flat
    bullet bodies (Next Session)."""
    if not prefixes:
        return body
    kept: list[str] = []
    for ln in _normalize_bullets(body):
        ln_body = ln.lstrip("-* ").strip()
        if any(ln_body.startswith(p) for p in prefixes):
            continue
        kept.append(ln)
    return "\n".join(kept)


def _filter_timed_segments(segs: list[tuple[str, str]],
                           prefixes: list[str]) -> list[tuple[str, str]]:
    """Drop matched bullets inside each timed segment; drop entire segment
    if its bullet body becomes empty after filtering."""
    if not prefixes:
        return segs
    out: list[tuple[str, str]] = []
    for ts, body in segs:
        kept_body = _filter_bullets_by_done(body, prefixes)
        if kept_body.strip():
            out.append((ts, kept_body))
    return out


def _apply_this_done(old_next: str, this_done: str) -> str:
    """Back-compat wrapper: Next-Session-only filter."""
    return _filter_bullets_by_done(old_next, _extract_done_prefixes(this_done))


def _merge_next_session_union(old_next: str, new_next: str) -> str:
    """Union of bullets, dedup by exact-line. Newest (new_next) first."""
    new_lines = _normalize_bullets(new_next)
    old_lines = _normalize_bullets(old_next)
    seen: set[str] = set()
    out: list[str] = []
    for ln in new_lines + old_lines:
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    return "\n".join(out)


def _format_segments(segs: list[tuple[str, str]]) -> str:
    """Render [(ts, body), ...] as `### [ts]\\n<body>` blocks separated by blank line."""
    if not segs:
        return "- None"
    parts = [f"### [{ts}]\n{body}" for ts, body in segs]
    return "\n\n".join(parts)


def _none_or(body: str) -> str:
    s = (body or "").strip()
    if not s or s.upper() == "N/A":
        return "- None"
    return s


def _merge_sections(prior_text: str, this_new: str, next_new: str,
                    now_epoch: int,
                    this_done: str = "") -> tuple[str, str, str]:
    """Compute (this_body, next_body, prev_body) merged with prior file.

    `this_done` (LLM-emitted verbatim copies of prior Next-Session bullets
    cleared THIS session) is applied to prior Next-Session BEFORE union
    with next_new — stops sonnet's ghost carry-over accumulation."""
    new_label = _now_label(now_epoch)
    this_clean = _none_or(this_new)
    next_clean = _none_or(next_new)

    if not prior_text.strip():
        new_seg = [(new_label, this_clean)] if this_clean != "- None" else []
        this_body = _format_segments(new_seg)
        return this_body, next_clean, "- None"

    footer_ts = _parse_footer_ts(prior_text)
    top_label = _parse_top_stamp(prior_text)
    fallback_label = (
        datetime.fromtimestamp(footer_ts).strftime("%Y-%m-%d %H:%M")
        if footer_ts else (top_label or new_label)
    )

    old_this_body = _split_section_body(prior_text, "This Session")
    old_prev_body = _split_section_body(prior_text, "Previous Sessions")
    old_next_body = _split_section_body(prior_text, "Next Session")

    done_prefixes = _extract_done_prefixes(this_done)

    old_this_segs = _split_timed_segments(old_this_body, fallback_label)
    old_prev_segs = _split_timed_segments(old_prev_body, fallback_label)
    old_this_segs = _filter_timed_segments(old_this_segs, done_prefixes)
    old_prev_segs = _filter_timed_segments(old_prev_segs, done_prefixes)

    fresh_this: list[tuple[str, str]] = []
    pushed_prev: list[tuple[str, str]] = []
    for ts, body in old_this_segs:
        ep = _ts_label_to_epoch(ts)
        if ep is None or (now_epoch - ep) <= _WINDOW_SEC:
            fresh_this.append((ts, body))
        else:
            pushed_prev.append((ts, body))

    if this_clean != "- None":
        merged_this = [(new_label, this_clean)] + fresh_this
    else:
        merged_this = fresh_this

    merged_prev = pushed_prev + old_prev_segs
    merged_prev.sort(key=lambda x: _ts_label_to_epoch(x[0]) or 0, reverse=True)

    this_body = _format_segments(merged_this) if merged_this else "- None"
    prev_body = _format_segments(merged_prev) if merged_prev else "- None"
    filtered_old_next = _filter_bullets_by_done(old_next_body, done_prefixes)
    next_body = _merge_next_session_union(filtered_old_next, next_clean)
    next_body = next_body if next_body.strip() else "- None"

    return this_body, next_body, prev_body


# ── audit snapshot ─────────────────────────────────────────────────────────

def _write_snapshot_audit(conn: sqlite3.Connection, sid: str, prior: str) -> None:
    """Persist the pre-overwrite handover.md body to audit_log."""
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


# ── flock-guarded write ────────────────────────────────────────────────────

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


# ── public render / write ──────────────────────────────────────────────────

def render_full(conn: sqlite3.Connection, sid: str,
                this_session: str, next_session: str,
                *, reference: str = "",
                prior_text: str = "", now_epoch: int | None = None,
                this_done: str = "") -> str:
    """Compose skeleton + merged ThisSession/Previous/Next + Reference \
+ ready stamp."""
    if now_epoch is None:
        now_epoch = int(time.time())
    this_body, next_body, prev_body = _merge_sections(
        prior_text, this_session, next_session, now_epoch,
        this_done=this_done)
    ref_body = (reference or "").strip() or "- N/A"
    text = render_skeleton(conn)
    text = _inject_section(text, "Previous Sessions", prev_body)
    text = _inject_section(text, "This Session", this_body)
    text = _inject_section(text, "Next Session", next_body)
    text = _inject_section(text, "Reference", ref_body)
    stamp = f"<!-- handover: ready sid:{sid} ts:{now_epoch} -->"
    return _append_stamp(text, stamp)


def write_handover_full(conn: sqlite3.Connection, sid: str,
                        this_session: str, next_session: str,
                        *, reference: str = "",
                        this_done: str = "") -> Path:
    """Sessionend_async single-writer: flock + merge + atomic write. Lock-loss
    falls back to handover.md.partial.<sid> with audit row, never crashes."""
    now_epoch = int(time.time())
    fd = _acquire_flock(_RENDERED_PATH)
    if fd is None:
        partial = _RENDERED_PATH.with_suffix(f".md.partial.{sid}")
        text = render_full(conn, sid, this_session, next_session,
                           reference=reference,
                           prior_text="", now_epoch=now_epoch,
                           this_done=this_done)
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
        _write_snapshot_audit(conn, sid, prior_text)
        text = render_full(conn, sid, this_session, next_session,
                           reference=reference,
                           prior_text=prior_text, now_epoch=now_epoch,
                           this_done=this_done)
        _atomic_write(str(_RENDERED_PATH), text)
    finally:
        _release_flock(fd)
    return _RENDERED_PATH
