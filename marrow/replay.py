"""Cross-session replay: the LATEST few rounds of OTHER conversations.

Not a delivery queue. Every call asks the same stateless question — "what are
the newest max_turns rounds happening elsewhere?" — and the only state is one
private marker per consumer holding the newest events row id that consumer has
already been shown. Content older than the latest window is never back-filled:
missed is missed.

Consumers own their marker file exclusively (state/replay/<key>) and guard it
with its own non-blocking flock. A busy lock skips the round; nothing is ever
written unlocked and no lock is shared between consumers.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re as _re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config, cortex_bridge, storage, transcript

# No channel is excluded by default: every consumer sees the ONE global latest
# window and drops only its own session_id. cortex.toml [note].shell_replay_exclude
# re-enables per-shell channel exclusion when set; an unknown shell falls back to
# the unqualified set.
_DEFAULT_SHELL_EXCLUDE: dict[str, list[str]] = {"cli": [], "tg": []}
_UNQUALIFIED_EXCLUDE: list[str] = []

_SLASH_HEAD = _re.compile(r"^/[A-Za-z][A-Za-z0-9_:.-]*$")


def marker_path(key: str) -> Path:
    return config.DATA_DIR / "state" / "replay" / f"{key}"


@contextlib.contextmanager
def _marker_lock(key: str):
    """Private non-blocking flock on <marker>.lock. Yields True when held, False
    when another writer for the SAME consumer holds it (skip the round)."""
    p = marker_path(key)
    fd = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if fd is not None:
            os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def load_marker(key: str) -> int | None:
    try:
        return int(marker_path(key).read_text().strip())
    except (OSError, ValueError):
        return None


def save_marker(key: str, row_id: int) -> None:
    p = marker_path(key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(int(row_id)))
    except OSError:
        pass


def local_hm(ts: str, tz) -> str:
    try:
        dt = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%H:%M")
    except ValueError:
        return "??:??"


def truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def _is_slash_command(text: str, max_chars: int) -> bool:
    """True when the WHOLE trimmed message is a slash command (optionally with
    short args) rather than prose that merely contains a '/'. Single line only,
    the head token carries no further '/' (kills /Users/... paths), and the whole
    message stays under max_chars."""
    t = (text or "").strip()
    if not t.startswith("/") or "\n" in t or len(t) > max_chars:
        return False
    return bool(_SLASH_HEAD.match(t.split(" ", 1)[0]))


def _drop_filters(cfg: dict):
    slash_max = None
    if cfg.get("drop_slash_commands", True):
        slash_max = int(cfg.get("slash_command_max_chars", 40))
    pats = []
    for p in (cfg.get("drop_patterns", []) or []):
        try:
            pats.append(_re.compile(p))
        except _re.error:
            continue
    return slash_max, pats


def render(rows, header: str, max_turns: int, per_chars: int,
           max_lines: int = 0) -> str:
    """Group events into turns and render the P3 block ('' when empty).

    A user event opens a turn; consecutive user msgs stay in the same turn; the
    following assistant msgs close it. Overflow beyond max_turns folds to a
    count. max_lines (0 = uncapped) caps the rendered message lines to the
    newest N after turn-capping.

    Pure noise for another session is skipped: bare slash-command turns and rows
    matching [replay].drop_patterns. A slash command drops its WHOLE turn (the
    command line AND the assistant rows answering it), otherwise the reply
    survives as an orphan."""
    tz = config.get_tz()
    slash_max, drop_pats = _drop_filters(
        (config.load().get("replay", {}) or {}))
    turns: list[list[dict]] = []
    slash_turn = False
    for r in rows:
        content = transcript.strip_media_markers(r["content"])
        if not content:
            continue
        if r["role"] == "user":
            slash_turn = (slash_max is not None
                          and _is_slash_command(content, slash_max))
        if slash_turn:
            continue
        if any(p.search(content) for p in drop_pats):
            continue
        item = {
            "channel": r["channel"] or "?",
            "sid4": (r["session_id"] or "")[:4],
            "hm": local_hm(r["timestamp"], tz),
            "role": "N" if r["role"] == "user" else "Y",
            "content": truncate(content, per_chars),
        }
        if r["role"] == "user" and (not turns or turns[-1][-1]["role"] != "N"):
            turns.append([item])
        elif not turns:
            turns.append([item])  # assistant with no opening user row
        else:
            turns[-1].append(item)
    if not turns:
        return ""
    kept = turns[-max_turns:]
    folded = len(turns) - len(kept)
    msg_lines = [
        f"[{it['channel']}·{it['sid4']} {it['hm']}] {it['role']}: {it['content']}"
        for turn in kept for it in turn
    ]
    if max_lines and len(msg_lines) > max_lines:
        msg_lines = msg_lines[-max_lines:]  # newest lines win
    lines = [header, *msg_lines]
    if folded > 0:
        lines.append(f"+{folded} earlier turns")
    return "\n".join(lines)


def shell_exclude_channels(shell: str | None) -> list[str]:
    """The channels a cortex `shell` drops from replay — empty unless cortex.toml
    [note].shell_replay_exclude configures one. Unmapped or unusable shell id ->
    the unqualified exclude list."""
    mapping = cortex_bridge._cortex_toml_section("note", "shell_replay_exclude", None)
    if not isinstance(mapping, dict):
        mapping = _DEFAULT_SHELL_EXCLUDE
    channels = mapping.get(str(shell))
    if channels is None:
        return list(_UNQUALIFIED_EXCLUDE)
    return [str(c) for c in channels if str(c).strip()]


def _latest_rows(conn, sid: str, exclude: list[str], limit: int):
    where = ["role IN ('user','assistant')"]
    params: list = []
    if sid:
        where.append("session_id != ?")
        params.append(sid)
    if exclude:
        where.append("COALESCE(channel,'') NOT IN ("
                     + ",".join("?" * len(exclude)) + ")")
        params += list(exclude)
    try:
        rows = conn.execute(
            "SELECT id, session_id, role, content, timestamp, channel FROM events "
            "WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return list(reversed(rows))  # chronological


def latest_block(key: str, *, sid: str = "", exclude: list[str] | None = None,
                 conn=None) -> str:
    """The core query. Returns the newest max_turns rounds of OTHER sessions that
    this consumer has not been shown yet, or '' when there is nothing newer than
    its marker (or the marker lock is busy).

    First call for a key (no marker) = seed: render the latest window and record
    it, which is also the SessionStart semantics."""
    cfg = (config.load().get("replay", {}) or {})
    if not cfg.get("enabled", True):
        return ""
    max_turns = int(cfg.get("max_turns", 2))
    per_chars = int(cfg.get("per_msg_chars", 250))
    max_lines = int(cfg.get("max_lines", 4))
    header = cfg.get("header", "## Recent replay from other sessions")

    own = conn is None
    if own:
        conn = storage.connect(config.db_path())
    try:
        with _marker_lock(key) as held:
            if not held:
                return ""
            rows = _latest_rows(conn, sid, exclude or [], max_turns * 4)
            if not rows:
                return ""
            newest = rows[-1]["id"]
            marker = load_marker(key)
            if marker is not None:
                if newest <= marker:
                    return ""
                rows = [r for r in rows if r["id"] > marker]
            save_marker(key, newest)
            return render(rows, header, max_turns, per_chars, max_lines)
    finally:
        if own:
            conn.close()


def _last_user_age_min(conn, sid: str) -> float | None:
    """Minutes since this sid's most recent user-role event, or None when there
    is none (first turn). The current turn is not archived yet."""
    try:
        row = conn.execute(
            "SELECT MAX(timestamp) AS ts FROM events "
            "WHERE session_id = ? AND role = 'user'",
            (sid,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row["ts"]:
        return None
    try:
        dt = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def context(sid: str, channel: str, *, transcript_path: str | None = None) -> str:
    """Replay for a hook-driven session (turn_inject and SessionStart share this
    one outlet AND one marker, keyed by sid, so content is never shown twice).
    Every session sees the same global latest window and excludes only its own
    sid; cortex windows are additionally exempt from the idle gate. Channel
    exclusion is opt-in via cortex.toml [note].shell_replay_exclude."""
    if not sid:
        return ""
    cfg = (config.load().get("replay", {}) or {})
    if not cfg.get("enabled", True):
        return ""

    is_cortex = False
    try:
        is_cortex = cortex_bridge.is_cortex_session(transcript_path)
    except Exception:
        pass

    if is_cortex:
        exclude = shell_exclude_channels(cortex_bridge._cortex_shell_id())
        return latest_block(sid, sid=sid, exclude=exclude)

    if channel in (cfg.get("exclude_target_channels", []) or []):
        return ""

    conn = storage.connect(config.db_path())
    try:
        # Idle gate: inject only when her last completed turn in THIS sid is
        # >= idle_gate_min old. Nothing is lost while gated — the next ungated
        # turn still shows the latest window. 0 disables the gate.
        idle_gate_min = float(cfg.get("idle_gate_min", 20))
        if idle_gate_min > 0:
            age = _last_user_age_min(conn, sid)
            if age is not None and age < idle_gate_min:
                return ""
        return latest_block(sid, sid=sid, exclude=list(_UNQUALIFIED_EXCLUDE),
                            conn=conn)
    finally:
        conn.close()
