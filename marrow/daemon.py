"""Marrow MCP server (stdio). Thin protocol shell over repo.py.

12-tool surface (07-06 rebuild): recall / atlas_lookup / event_embed / wish +
8 action-dispatch tools (tl / sticker / sticker_admin / goal / first_tick /
dim / alert / event_clear). The session-start handoff is rendered by the
SessionStart hook. LLMClient wired so provider failures land in alerts.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import config, recall as _recall_mod, repo, storage
from .llm import LLMClient
from .timeutil import utc_iso_to_local_datetime, format_recall_ts

mcp = FastMCP("marrow")

_DB = config.db_path()
llm = LLMClient(
    on_alert=lambda sev, t, m, s: repo.add_alert(sev, t, m, s, db=_DB)
)


@mcp.tool()
def recall(
    query: str,
    limit: int = 10,
    context: bool = False,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Recall past session turns matching a query. Uses vector + FTS5 +
    recency + affect fusion when bge-m3 is loaded; FTS5-only fallback.
    Call when the user references the past.
    Set context=True to attach ±1 adjacent same-session turns to each event row.
    since/until: Melbourne-local YYYY-MM-DD day strings for time-lane filtering.
    Diary: recall(query="diary", since="date", until="date")."""
    # C3 guard: cortex's resumed session loads MCP tools full-env (no
    # isolation, see MAP §6) so it COULD call this tool directly, unlike
    # the passive hook-injection path which already no-ops (hooks.py
    # user_prompt_submit). Matches tl's add/update hard block — cortex
    # gets its own bulletin, never chat memory (MAP §1.2 "total invisibility").
    if os.environ.get("MARROW_CORTEX"):
        raise RuntimeError("recall unavailable in a cortex session (MARROW_CORTEX=1)")
    from .timecue import melb_day_range
    since_utc: str | None = None
    until_utc: str | None = None
    if since:
        since_utc, _ = melb_day_range(since)
    if until:
        _, until_utc = melb_day_range(until)

    conn = storage.connect(_DB)
    try:
        # Empty/whitespace query with window → return digest rows for that window
        if not query.strip() and since_utc and until_utc:
            rows = _recall_mod.fetch_window_digests(conn, since_utc, until_utc, cap=limit)
            return rows

        # MCP manual recall: include all kinds (diary + task explicitly wanted).
        rows = _recall_mod.recall_with_config(
            conn, query, limit=limit, exclude_kinds=(),
            since=since_utc, until=until_utc,
        )
        if context:
            for row in rows:
                kind = row.get("kind") or "event"
                if kind not in ("entity", "milestone", "memes", "diary", "task"):
                    sid = row.get("session_id")
                    eid = row.get("id")
                    if sid and eid:
                        row["_context"] = _recall_mod.fetch_event_context(
                            conn, sid, int(eid), n=1
                        )
    finally:
        conn.close()
    # Best-effort: bump recall_count for injected event-kind rows.
    try:
        event_ids = [
            int(r["id"])
            for r in rows
            if r.get("id") and (r.get("kind") or "event") == "event"
        ]
        if event_ids:
            _recall_mod.bump_recall_counts(event_ids, db=_DB)
    except Exception:
        pass
    # Convert UTC timestamps to Melbourne local time at the read boundary.
    # `when` is computed from the raw UTC string before conversion.
    for row in rows:
        ts = row.get("timestamp")
        if ts:
            row["when"] = format_recall_ts(ts)
            row["timestamp"] = utc_iso_to_local_datetime(ts)
        if "_context" in row:
            for c in row["_context"]:
                cts = c.get("timestamp")
                if cts:
                    c["timestamp"] = utc_iso_to_local_datetime(cts)
    return rows


@mcp.tool()
def atlas_lookup(prefix: str) -> list[dict]:
    """Look up atlas rows by path prefix. Returns description/naming for matched dirs."""
    conn = storage.connect(_DB)
    try:
        from . import atlas
        return atlas.lookup_by_prefix(conn, prefix)
    finally:
        conn.close()


@mcp.tool()
def event_embed(batch: int = 50) -> dict:
    """Embed unvectorized events (write-time backfill). Returns count written."""
    conn = storage.connect(_DB)
    try:
        n = _recall_mod.embed_pending(conn, batch=batch)
        return {"embedded": n}
    finally:
        conn.close()


# ── tl ───────────────────────────────────────────────────────────────────────

_TL_ACTIONS = {"add", "update", "silence", "clear"}


def _tl_where(event_id, sid, before, after):
    if event_id is not None:
        return "role='tl' AND id=?", [event_id]
    if sid:
        return "role='tl' AND session_id=?", [sid]
    clauses = ["role='tl'"]
    params: list = []
    if before:
        clauses.append("timestamp < ?")
        params.append(before)
    if after:
        clauses.append("timestamp >= ?")
        params.append(after)
    return " AND ".join(clauses), params


def _tl_clear_dashboard(ids: list[int]) -> None:
    """Strip `<!-- tl:e:<id> -->` lines for the given ids from dashboard.md
    and drop them from the tl-rendered trail's e= list. Mirrors the sid path
    in the old db_clear (daemon.py, pre-rebuild) but scoped to e= ids since
    role='tl' rows always render with a tl:e anchor, never tl:<sid>."""
    if not ids:
        return
    import re
    dash = Path.home() / "Desktop" / "NY" / "dashboard.md"
    if not dash.exists():
        return
    text = dash.read_text(encoding="utf-8")
    id_strs = {str(i) for i in ids}
    for i in ids:
        text = re.sub(rf"[^\n]*<!-- tl:e:{i} -->\n?", "", text)
    m = re.search(r"<!-- tl-rendered:([^ ]+) -->", text)
    if m:
        new_parts = []
        for part in m.group(1).split(";"):
            if part.startswith("e="):
                remaining = [x for x in part[2:].split(",") if x and x not in id_strs]
                if remaining:
                    new_parts.append("e=" + ",".join(remaining))
            else:
                new_parts.append(part)
        if new_parts:
            text = text.replace(m.group(0), f"<!-- tl-rendered:{';'.join(new_parts)} -->")
        else:
            text = text.replace(m.group(0), "")
    dash.write_text(text, encoding="utf-8")
    subprocess.run(["mw", "refresh", "--all"], capture_output=True, text=True)


def _tl_clear(event_id: int | None, sid: str | None,
              before: str | None, after: str | None) -> dict:
    import shutil
    from datetime import datetime, timezone

    selectors = [event_id is not None, bool(sid), bool(before or after)]
    if sum(selectors) == 0:
        return {"ok": False, "error": "one of event_id / sid / before-after required"}
    if sum(selectors) > 1:
        return {"ok": False, "error": "event_id / sid / before-after are mutually exclusive"}

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = f"/tmp/marrow-backup-tlclear-{ts}.db"
    shutil.copy2(str(_DB), backup)

    where, params = _tl_where(event_id, sid, before, after)
    conn = storage.connect(_DB)
    try:
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM events WHERE {where}", params).fetchall()]
        if not ids:
            return {"ok": True, "cleared": 0, "backup": backup}
        placeholders = ",".join("?" * len(ids))
        with conn:
            conn.execute(f"DELETE FROM events WHERE id IN ({placeholders})", ids)
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('events', ?, 'tl_clear', ?)",
                (",".join(str(i) for i in ids),
                 f"selector={'event_id' if event_id is not None else 'sid' if sid else 'range'}"),
            )
    finally:
        conn.close()
    _tl_clear_dashboard(ids)
    return {"ok": True, "cleared": len(ids), "ids": ids, "backup": backup}


@mcp.tool(meta={"anthropic/alwaysLoad": True})
def tl(
    action: str,
    timerange: str | None = None,
    body: str | None = None,
    n_word: str | None = None,
    y_word: str | None = None,
    importance: int | None = None,
    sid: str | None = None,
    event_id: int | None = None,
    before: str | None = None,
    after: str | None = None,
) -> dict:
    """Session timeline. action='add' records a scene live — call when scene
    shifts, emotional turns, or a task completes; 'update' revises a row by
    event_id (task sessions keep one row and update it — hard step in /ho);
    'silence' mutes this session's nudge + self writes (dies with session);
    'clear' deletes timeline rows by event_id / sid / before-after range,
    DB backup first.
    Row format (add/update): HH:mm-HH:mm 【N affect♡Y affect】body [i]
    - e.g. 21:25-21:31 【N愉悦♡Y委屈】翻CC日志找骂人梗，扑空互怼 [3]
    - N = 念念, Y = 阿屿; single-side rows OK. affect = mood phrase, 1-8 chars.
    - i = one composite imp per row: 1-2 routine · 3 medium (~1wk) ·
      4 high (conflict/exam) · 5 milestone. Default 3.
    - body <=30 chars, vivid not work-log; life details in, tech details out.
      No third person — 我/你 only, never 她/他.
    - When: topic/mood change or task done; ~2-3 per session
      (every 1-2h or 10-20 turns).
    Params: add → timerange, body, n_word/y_word (affect phrase per side,
    <=8 chars, no numbers), importance, sid; update → event_id + any of the
    above; silence → sid optional; clear → event_id | sid | before/after."""
    if action not in _TL_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_TL_ACTIONS)}"}

    if action == "add":
        if not timerange or not body:
            return {"ok": False, "error": "add requires timerange and body"}
        conn = storage.connect(_DB)
        try:
            from . import tl_writer
            try:
                return tl_writer.tl_add(
                    conn, timerange, body,
                    n_word=n_word, y_word=y_word,
                    importance=importance, sid=sid,
                )
            except tl_writer.TlError as exc:
                return {"ok": False, "error": str(exc)}
        finally:
            conn.close()

    if action == "update":
        if event_id is None:
            return {"ok": False, "error": "update requires event_id"}
        conn = storage.connect(_DB)
        try:
            from . import tl_writer
            try:
                return tl_writer.tl_update(
                    conn, event_id, timerange=timerange, body=body,
                    n_word=n_word, y_word=y_word,
                    importance=importance,
                )
            except tl_writer.TlError as exc:
                return {"ok": False, "error": str(exc)}
        finally:
            conn.close()

    if action == "silence":
        conn = storage.connect(_DB)
        try:
            _sid = sid
            if not _sid:
                from .timeline import _query_current_sid
                _sid = _query_current_sid(conn)
        finally:
            conn.close()
        if not _sid:
            return {"ok": False, "error": "no active session id"}
        from . import tl_nudge
        tl_nudge.set_silent(_sid)
        return {"ok": True, "sid": _sid, "silent": True}

    # clear
    return _tl_clear(event_id=event_id, sid=sid, before=before, after=after)


# ── sticker ──────────────────────────────────────────────────────────────────

_STICKER_ACTIONS = {"search", "pick"}


def _sticker_search(query: str, limit: int, animated: bool) -> list[dict]:
    if not query.strip():
        return []
    conn = storage.connect(_DB)
    try:
        try:
            from .recall import _blob_to_vec, _ensure_embedder, _vec_to_blob
            _ = _blob_to_vec
            emb = _ensure_embedder()
            if emb is not None:
                query_vec = emb.embed([query])[0]
                hits = conn.execute(
                    "SELECT rowid, distance FROM stickers_vec "
                    "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (_vec_to_blob(query_vec), limit),
                ).fetchall()
                rows = []
                for hit in hits:
                    row = conn.execute(
                        "SELECT id, desc, path, source FROM stickers WHERE id = ?",
                        (hit["rowid"],),
                    ).fetchone()
                    if row:
                        if not animated and str(row["path"]).endswith(".gif"):
                            continue
                        rows.append(dict(row))
                if rows:
                    return rows
        except Exception:
            pass

        terms = [t.strip() for t in query.split() if t.strip()]
        if not terms:
            return []
        where = " OR ".join("desc LIKE ?" for _ in terms)
        gif_clause = " AND path NOT LIKE '%.gif'" if not animated else ""
        params = [f"%{t}%" for t in terms]
        params.append(limit)
        rows = conn.execute(
            f"SELECT id, desc, path, source FROM stickers"
            f" WHERE ({where}){gif_clause} ORDER BY last_used DESC NULLS LAST"
            f" LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _sticker_pick(sticker_id: int) -> dict:
    conn = storage.connect(_DB)
    try:
        conn.execute(
            "UPDATE stickers SET last_used = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            " WHERE id = ?", (sticker_id,),
        )
        conn.commit()
        return {"ok": True, "id": sticker_id}
    finally:
        conn.close()


@mcp.tool()
def sticker(
    action: str,
    query: str | None = None,
    limit: int = 5,
    animated: bool = True,
    sticker_id: int | None = None,
) -> dict | list[dict]:
    """Sticker library. action='search': find stickers by vibe/emotion
    keywords (e.g. '爱你' '委屈' '哭'), params query, limit, animated
    (false excludes GIFs). action='pick': log a send by sticker_id — bumps
    last_used, call after sending. Send via <image path="..."/> or
    <gif path="..."/>. [channel: wx/tg only — never cli.]"""
    if action not in _STICKER_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_STICKER_ACTIONS)}"}
    if action == "search":
        return _sticker_search(query or "", limit, animated)
    if sticker_id is None:
        return {"ok": False, "error": "pick requires sticker_id"}
    return _sticker_pick(sticker_id)


# ── sticker_admin ────────────────────────────────────────────────────────────

_STICKER_ADMIN_ACTIONS = {"ingest", "update", "delete", "pending"}


def _write_stickers_subpage(conn) -> None:
    """Render stickers.md immediately after a DB write."""
    from .subpages import build_all_configs, write_subpage
    folder = config.db_pages_path()
    state_dir = config.db_pages_state_path()
    cfgs = build_all_configs(conn, folder=folder, state_dir=state_dir)
    stickers_cfg = next((c for c in cfgs if c.key == "stickers"), None)
    if stickers_cfg:
        write_subpage(stickers_cfg, conn)


def _sticker_ingest(image_path: str, desc: str, source: str) -> dict:
    conn = storage.connect(_DB)
    try:
        from .sticker_ops import ingest_sticker
        result = ingest_sticker(conn, image_path, desc, source)
        if not result.get("duplicate"):
            _write_stickers_subpage(conn)
        return result
    except Exception as exc:
        repo.add_alert("warn", "sticker_ingest",
                       f"sticker_ingest:mcp",
                       source="daemon",
                       message=f"sticker ingest failed: {Path(image_path).name} — {exc}")
        raise
    finally:
        conn.close()


def _sticker_update(sticker_id: int, desc: str) -> dict:
    conn = storage.connect(_DB)
    try:
        from .sticker_ops import update_sticker
        result = update_sticker(conn, sticker_id, desc)
        if result.get("ok"):
            _write_stickers_subpage(conn)
        return result
    finally:
        conn.close()


def _sticker_delete(sticker_id: int) -> dict:
    conn = storage.connect(_DB)
    try:
        from .sticker_ops import delete_sticker
        result = delete_sticker(conn, sticker_id)
        if result.get("ok"):
            _write_stickers_subpage(conn)
        return result
    finally:
        conn.close()


def _sticker_list_pending() -> list[dict]:
    conn = storage.connect(_DB)
    try:
        rows = conn.execute(
            "SELECT id, path, desc FROM stickers"
            " WHERE desc IS NULL OR desc = '' OR desc = '(pending)'"
            " ORDER BY id"
        ).fetchall()
        return [{"id": r["id"], "path": r["path"], "desc": r["desc"]} for r in rows]
    finally:
        conn.close()


@mcp.tool()
def sticker_admin(
    action: str,
    image_path: str | None = None,
    desc: str | None = None,
    source: str = "wechat",
    sticker_id: int | None = None,
) -> dict | list[dict]:
    """Sticker library management. action='ingest' adds an image (hash dedup,
    thumbnail, renders stickers.md; params image_path, desc, source);
    'update' rewrites a desc (sticker_id, desc); 'delete' removes
    file+thumb+DB row+md line (sticker_id); 'pending' lists stickers with
    missing/placeholder descriptions."""
    if action not in _STICKER_ADMIN_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_STICKER_ADMIN_ACTIONS)}"}
    if action == "ingest":
        if not image_path or desc is None:
            return {"ok": False, "error": "ingest requires image_path and desc"}
        return _sticker_ingest(image_path, desc, source)
    if action == "update":
        if sticker_id is None or desc is None:
            return {"ok": False, "error": "update requires sticker_id and desc"}
        return _sticker_update(sticker_id, desc)
    if action == "delete":
        if sticker_id is None:
            return {"ok": False, "error": "delete requires sticker_id"}
        return _sticker_delete(sticker_id)
    return _sticker_list_pending()


# ── goal ─────────────────────────────────────────────────────────────────────

_GOAL_ACTIONS = {"set", "list", "delete"}


@mcp.tool()
def goal(
    action: str,
    key: str | None = None,
    value: str | None = None,
    unit: str | None = None,
) -> dict | list[dict]:
    """Track-zone goals, read by cortex each tick. action='set' the moment she
    states or changes one — 'sleep goal 8h' → set key='sleep' value='8'
    unit='h'; 'list' all; 'delete' by key when dropped or achieved."""
    if action not in _GOAL_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_GOAL_ACTIONS)}"}

    if action == "set":
        key = (key or "").strip()
        value = (value or "").strip()
        if not key:
            return {"ok": False, "error": "key required"}
        if not value:
            return {"ok": False, "error": "value required"}
        conn = storage.connect(_DB)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO goals (key, value, unit, updated_at)"
                    " VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
                    " ON CONFLICT(key) DO UPDATE SET"
                    " value=excluded.value, unit=excluded.unit,"
                    " updated_at=excluded.updated_at",
                    (key, value, unit),
                )
            return {"ok": True, "key": key, "value": value, "unit": unit}
        finally:
            conn.close()

    if action == "list":
        conn = storage.connect(_DB)
        try:
            rows = conn.execute(
                "SELECT key, value, unit, updated_at FROM goals ORDER BY key"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # delete
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "key required"}
    conn = storage.connect(_DB)
    try:
        with conn:
            cur = conn.execute("DELETE FROM goals WHERE key=?", (key,))
        return {"ok": True, "key": key, "deleted": cur.rowcount > 0}
    finally:
        conn.close()


# ── wish ─────────────────────────────────────────────────────────────────────

def _wishlist_path() -> Path:
    cortex_cfg = config.load().get("cortex", {})
    wp = (cortex_cfg.get("wishlist_path") or "").strip()
    if wp:
        return Path(wp).expanduser()
    home = cortex_cfg.get("home") or "~/.config/marrow/cortex"
    return Path(home).expanduser() / "wishlist.md"


_WISHLIST_HEADER = (
    "# Wishlist\n\n"
    "> Owed treats, wants, self-rewards. Append-only — hand edits are sacred.\n\n"
)


@mcp.tool(meta={"anthropic/alwaysLoad": True})
def wish(text: str) -> dict:
    """Append her want / owed treat / self-reward verbatim to the wishlist md
    the moment she names one. Returns the md path — to change or remove
    entries, edit that file directly. Cortex reads the md."""
    import fcntl
    from datetime import datetime

    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "text required"}
    path = _wishlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    date = datetime.now(config.get_tz()).strftime("%Y-%m-%d")
    line = f"- {date} {text}\n"
    lock_path = str(path) + ".lock"
    lf = open(lock_path, "a")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        existing = path.read_text(encoding="utf-8") if path.exists() else _WISHLIST_HEADER
        from ._atomic import atomic_write
        atomic_write(str(path), existing + line)
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        lf.close()
    return {"ok": True, "path": str(path), "line": line.strip()}


# ── first_tick ───────────────────────────────────────────────────────────────

_FIRST_TICK_ACTIONS = {"tick", "untick", "list"}


@mcp.tool()
def first_tick(
    action: str,
    item: str | None = None,
    note: str | None = None,
    sid: str | None = None,
) -> dict | list[dict]:
    """Cortex-nag acknowledgement. action='tick' the moment you start handling
    an item cortex surfaced — stops repeat-nagging across sessions (item,
    note); 'untick' reverses a wrong ack (item); 'list' shows current acks."""
    if action not in _FIRST_TICK_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_FIRST_TICK_ACTIONS)}"}

    if action == "list":
        conn = storage.connect(_DB)
        try:
            rows = conn.execute(
                "SELECT item, seen_at, sid, note FROM ct_first_tick ORDER BY seen_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    item = (item or "").strip()
    if not item:
        return {"ok": False, "error": "item required"}

    if action == "untick":
        conn = storage.connect(_DB)
        try:
            with conn:
                cur = conn.execute("DELETE FROM ct_first_tick WHERE item=?", (item,))
            return {"ok": cur.rowcount > 0, "item": item}
        finally:
            conn.close()

    # tick
    note = (note or "").strip() or None
    conn = storage.connect(_DB)
    try:
        if not sid:
            from .timeline import _query_current_sid
            sid = _query_current_sid(conn)
        with conn:
            conn.execute(
                "INSERT INTO ct_first_tick (item, seen_at, sid, note)"
                " VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?)"
                " ON CONFLICT(item) DO UPDATE SET"
                " seen_at=excluded.seen_at, sid=excluded.sid, note=excluded.note",
                (item, sid, note),
            )
        return {"ok": True, "item": item, "sid": sid, "note": note}
    finally:
        conn.close()


# ── dim ──────────────────────────────────────────────────────────────────────

_DIM_ACTIONS = {"upsert", "query", "delete"}
_DIM_KINDS = {"person", "pref", "place", "meme", "milestone"}
_DIM_MEME_TYPES = {"paw", "fact", "news", "event", "others"}
_DIM_MD_FILES = {
    "person": "profile.md", "pref": "profile.md", "place": "profile.md",
    "meme": "memes.md",
    "milestone": "milestone.md",
}


def _dim_upsert_entity(kind: str, name: str, fact: str | None,
                       aliases: list[str] | None) -> dict:
    from .candidates import _merge_aliases_into, match_entity
    import json

    aliases_list = [str(a).strip() for a in (aliases or []) if str(a).strip()]
    fact = (fact or "").strip() or None
    conn = storage.connect(_DB)
    try:
        hit_id = match_entity(conn, kind, name, aliases_list, source="daemon.dim_upsert")
        if hit_id is not None:
            _merge_aliases_into(conn, hit_id, name, aliases_list)
            if fact is not None:
                with conn:
                    conn.execute(
                        "UPDATE entities SET fact=?,"
                        " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                        " WHERE id=?", (fact, hit_id),
                    )
            return {"ok": True, "action": "update", "id": hit_id, "kind": kind}
        aliases_json = (json.dumps(aliases_list, ensure_ascii=False)
                        if aliases_list else None)
        with conn:
            cur = conn.execute(
                "INSERT INTO entities (kind, name, fact, aliases, source)"
                " VALUES (?, ?, ?, ?, 'session')",
                (kind, name, fact, aliases_json),
            )
        return {"ok": True, "action": "create", "id": cur.lastrowid, "kind": kind}
    finally:
        conn.close()


def _dim_upsert_meme(name: str, fact: str | None, meme_type: str | None,
                     context: str | None) -> dict:
    from . import memes_dedup

    key = name
    value = (fact or "").strip() or None
    vtype_given = (meme_type or "").strip() or None
    if vtype_given and vtype_given not in _DIM_MEME_TYPES:
        return {"ok": False, "error": f"meme_type must be one of {sorted(_DIM_MEME_TYPES)}"}
    context = (context or "").strip() or None
    conn = storage.connect(_DB)
    try:
        # Lookup by key alone (not type+key) — "meme_type omit -> auto" means
        # an update call needn't repeat the type it was created with; the
        # existing row's type carries over unless a new one is explicitly passed.
        existing = conn.execute(
            "SELECT id, type FROM memes WHERE key=? LIMIT 1", (key,),
        ).fetchone()
        if existing:
            new_type = vtype_given or existing["type"]
            with conn:
                conn.execute(
                    "UPDATE memes SET type=?, value=COALESCE(?, value),"
                    " context=COALESCE(?, context), pinned=1,"
                    " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (new_type, value, context, existing["id"]),
                )
            return {"ok": True, "action": "update", "id": existing["id"], "kind": "meme"}
        vtype = vtype_given or "others"
        reason = memes_dedup.string_dup_reason(conn, key)
        if reason is not None:
            return {"ok": False, "error": f"dedup reject: {reason}"}
        cos = memes_dedup.cosine_dup_score(conn, key)
        if cos is not None and cos >= memes_dedup.cosine_dup_threshold():
            return {"ok": False, "error": "dedup reject: cosine_dup"}
        with conn:
            # updated_at set explicitly (NOT NULL-by-omission) — a NULL
            # updated_at + a later md touch makes reconcile hard-delete the
            # row (verified live 07-06); every dim-added meme is pinned.
            cur = conn.execute(
                "INSERT INTO memes (type, key, value, context, pinned,"
                " source_hash, updated_at)"
                " VALUES (?, ?, ?, ?, 1, 'dim_upsert',"
                " strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (vtype, key, value, context),
            )
        return {"ok": True, "action": "create", "id": cur.lastrowid, "kind": "meme"}
    finally:
        conn.close()


def _dim_upsert_milestone(name: str, fact: str | None, date: str | None,
                          scope: str = "me") -> dict:
    import hashlib
    import re as _re

    if not date or not _re.match(r"^\d{4}(-\d{2}-\d{2})?$", date):
        return {"ok": False, "error": "date required (YYYY-MM-DD or YYYY)"}
    if scope not in {"us", "me"}:
        scope = "me"
    desc = (fact or "").strip() or None
    conn = storage.connect(_DB)
    try:
        existing = conn.execute(
            "SELECT id FROM milestones WHERE scope=? AND title=? AND date=?",
            (scope, name, date),
        ).fetchone()
        if existing:
            with conn:
                conn.execute(
                    "UPDATE milestones SET description=COALESCE(?, description),"
                    " pinned=1, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE id=?", (desc, existing["id"]),
                )
            return {"ok": True, "action": "update", "id": existing["id"], "kind": "milestone"}
        src = "\x1f".join([scope, date, name, desc or ""])
        h = hashlib.sha256(src.encode()).hexdigest()
        with conn:
            cur = conn.execute(
                "INSERT INTO milestones (scope, date, title, description,"
                " source_hash, pinned) VALUES (?, ?, ?, ?, ?, 1)",
                (scope, date, name, desc, h),
            )
        return {"ok": True, "action": "create", "id": cur.lastrowid, "kind": "milestone"}
    finally:
        conn.close()


def _dim_upsert(kind: str | None, name: str | None, fact: str | None,
                aliases: list[str] | None, meme_type: str | None,
                context: str | None, date: str | None, scope: str) -> dict:
    kind = (kind or "").strip()
    if kind not in _DIM_KINDS:
        return {"ok": False, "error": f"kind must be one of {sorted(_DIM_KINDS)}"}
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    if kind in ("person", "pref", "place"):
        return _dim_upsert_entity(kind, name, fact, aliases)
    if kind == "meme":
        return _dim_upsert_meme(name, fact, meme_type, context)
    return _dim_upsert_milestone(name, fact, date, scope)


def _dim_query(kind: str | None, name: str | None) -> list[dict]:
    if kind and kind not in _DIM_KINDS:
        return []
    kinds = [kind] if kind else sorted(_DIM_KINDS)
    needle = f"%{name.strip()}%" if name and name.strip() else None
    conn = storage.connect(_DB)
    try:
        results: list[dict] = []
        for k in kinds:
            if k in ("person", "pref", "place"):
                sql = "SELECT id, kind, name, fact, aliases FROM entities_live WHERE kind=?"
                params: list = [k]
                if needle:
                    sql += " AND (name LIKE ? OR aliases LIKE ?)"
                    params += [needle, needle]
                for r in conn.execute(sql, params).fetchall():
                    results.append(dict(r))
            elif k == "meme":
                sql = "SELECT id, type, key, value, context, pinned, status FROM memes"
                params = []
                if needle:
                    sql += " WHERE (key LIKE ? OR value LIKE ?)"
                    params += [needle, needle]
                for r in conn.execute(sql, params).fetchall():
                    d = dict(r)
                    d["kind"] = "meme"
                    d["name"] = d.pop("key")
                    d["fact"] = d.pop("value")
                    results.append(d)
            elif k == "milestone":
                sql = "SELECT id, scope, date, title, description, pinned FROM milestones"
                params = []
                if needle:
                    sql += " WHERE (title LIKE ? OR description LIKE ?)"
                    params += [needle, needle]
                for r in conn.execute(sql, params).fetchall():
                    d = dict(r)
                    d["kind"] = "milestone"
                    d["name"] = d.pop("title")
                    d["fact"] = d.pop("description")
                    results.append(d)
        return results
    finally:
        conn.close()


def _dim_remove_md_block(path: Path, kind: str, item_id: int) -> None:
    """Strip the anchored line (single-line kinds) or the 2-line head+anchor
    block (milestone) for item_id from `path`. Best-effort — no-op if the
    file or anchor isn't there (e.g. never synced to md yet)."""
    if not path.exists():
        return
    import re

    text = path.read_text(encoding="utf-8")
    anchor = f"<!-- id:{item_id} -->"
    if kind == "milestone":
        pattern = re.compile(
            r"[ \t]*#####[^\n]*\n[^\n]*" + re.escape(anchor) + r"[^\n]*\n?"
        )
    else:
        pattern = re.compile(r"[^\n]*" + re.escape(anchor) + r"[^\n]*\n?")
    new_text = pattern.sub("", text, count=1)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")


def _dim_delete(kind: str | None, item_id: int | None) -> dict:
    if kind not in _DIM_KINDS:
        return {"ok": False, "error": f"kind must be one of {sorted(_DIM_KINDS)}"}
    if item_id is None:
        return {"ok": False, "error": "id required"}
    table = {"person": "entities", "pref": "entities", "place": "entities",
             "meme": "memes", "milestone": "milestones"}[kind]
    conn = storage.connect(_DB)
    try:
        row = conn.execute(f"SELECT id FROM {table} WHERE id=?", (item_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": f"{kind} id={item_id} not found"}
        path = Path(config.db_pages_path()) / _DIM_MD_FILES[kind]
        with conn:
            conn.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
            from .md_index import MdIndex
            store = MdIndex(conn)
            # record_block first (ON CONFLICT upsert) so the tombstone UPDATE
            # always lands even if this row was never synced to md yet —
            # "no zombies" means no un-tombstoned resurrection either.
            store.record_block(str(path), str(item_id), "deleted")
            store.tombstone(str(path), str(item_id))
    finally:
        conn.close()
    _dim_remove_md_block(path, kind, item_id)
    subprocess.run(["mw", "refresh", "--all"], capture_output=True, text=True)
    return {"ok": True, "kind": kind, "id": item_id, "deleted": True}


@mcp.tool()
def dim(
    action: str,
    kind: str | None = None,
    name: str | None = None,
    fact: str | None = None,
    aliases: list[str] | None = None,
    meme_type: str | None = None,
    context: str | None = None,
    date: str | None = None,
    scope: str = "me",
    id: int | None = None,
) -> dict | list[dict]:
    """Dims read/write/delete. kind='person'|'pref'|'place' (entities) |
    'meme' (memes) | 'milestone' (milestones).
    - 'upsert': create or update when recall misses something that clearly
      exists, or a hit shows stale fields. Entities: name+fact+aliases
      (dedup gate merges alias/name/cosine matches). Memes: name=key (the
      trigger phrase), fact=value (what it means), meme_type
      paw/fact/news/event/others (paw = couple lore; omit → auto), context =
      source quote; hand-added rows are pinned + permanent. Milestones:
      name+fact+date.
    - 'query': by kind and/or name match, returns rows with id — use to
      verify a write landed.
    - 'delete': by id (query first). Removes DB row + md line + tombstone
      in one pass, no zombies."""
    if action not in _DIM_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_DIM_ACTIONS)}"}
    if action == "upsert":
        return _dim_upsert(kind, name, fact, aliases, meme_type, context, date, scope)
    if action == "query":
        return _dim_query(kind, name)
    return _dim_delete(kind, id)


# ── alert ────────────────────────────────────────────────────────────────────

_ALERT_ACTIONS = {"list", "resolve"}


@mcp.tool()
def alert(action: str, alert_id: int | None = None) -> dict | list[dict]:
    """action='list' unresolved alerts; 'resolve' by alert_id when
    sessionstart shows one — auto-refreshes dashboard, restarts watcher if
    code changed."""
    if action not in _ALERT_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_ALERT_ACTIONS)}"}
    if action == "list":
        conn = storage.connect(_DB)
        try:
            rows = conn.execute(
                "SELECT id, severity, message, source, created_at"
                " FROM alerts WHERE resolved = 0"
                " ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    # resolve
    if alert_id is None:
        return {"ok": False, "error": "resolve requires alert_id"}
    result = subprocess.run(
        ["mw", "resolve", str(alert_id)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() or "resolve failed"}
    return {"ok": True, "id": alert_id}


# ── event_clear ──────────────────────────────────────────────────────────────

def _time_where(col, before, after):
    clauses, params = [], []
    if before:
        clauses.append(f"{col} < ?")
        params.append(before)
    if after:
        clauses.append(f"{col} >= ?")
        params.append(after)
    if clauses:
        return " WHERE " + " AND ".join(clauses), params
    return "", []


def _do_event_clear(before: str | None, after: str | None, last: int | None) -> dict:
    import re
    import shutil
    from datetime import datetime, timezone

    time_filtered = bool(before or after)
    if time_filtered and last:
        return {"ok": False, "error": "before/after and last are mutually exclusive"}

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = f"/tmp/marrow-backup-purge-{ts}.db"
    shutil.copy2(str(_DB), backup)

    if not (time_filtered or last):
        dash = Path.home() / "Desktop" / "NY" / "dashboard.md"
        if dash.exists():
            text = dash.read_text(encoding="utf-8")
            text = re.sub(
                r"(<!-- id:dashboard\.timeline -->)\n## Timeline\n.*?(?=\n<!-- id:)",
                r"\1\n## Timeline\n_none_\n", text, flags=re.DOTALL)
            dash.write_text(text, encoding="utf-8")

    conn = storage.connect(_DB)
    counts = {}
    try:
        if last:
            sids = [r[0] for r in conn.execute(
                "SELECT DISTINCT session_id FROM events WHERE id IN "
                "(SELECT id FROM events ORDER BY timestamp DESC LIMIT ?)", [last]).fetchall()]
            conn.execute(
                "DELETE FROM events WHERE id IN "
                "(SELECT id FROM events ORDER BY timestamp DESC LIMIT ?)", [last])
            if sids:
                conn.executemany(
                    "DELETE FROM audit_log WHERE action='sessionend_extract' AND target_id=?",
                    [(s,) for s in sids])
            counts["events"] = last
        elif time_filtered:
            where, params = _time_where("timestamp", before, after)
            sids = [r[0] for r in conn.execute(
                "SELECT DISTINCT session_id FROM events" + where, params).fetchall()]
            conn.execute("DELETE FROM events" + where, params)
            if sids:
                conn.executemany(
                    "DELETE FROM audit_log WHERE action='sessionend_extract' AND target_id=?",
                    [(s,) for s in sids])
        else:
            triggers = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='events'"
            ).fetchall()
            for t in triggers:
                conn.execute(f"DROP TRIGGER IF EXISTS {t['name']}")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM event_tombstones")
            conn.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
            conn.execute("DELETE FROM events_vec")
            conn.execute("DELETE FROM audit_log WHERE action='sessionend_extract'")
            for t in triggers:
                conn.execute(t["sql"])
        conn.commit()
    finally:
        conn.close()

    subprocess.run(["mw", "refresh", "--all"], capture_output=True, text=True)
    result = {"ok": True, "purged": ["events"], "backup": backup}
    if before:
        result["before"] = before
    if after:
        result["after"] = after
    if last:
        result["last"] = last
    if counts:
        result["counts"] = counts
    return result


@mcp.tool()
def event_clear(before: str = "", after: str = "", last: int = 0) -> dict:
    """Delete raw events (recall corpus): events+FTS+vectors+tombstones.
    Filters: before/after (ISO or YYYY-MM-DD) = period; last=N = most
    recent; no filter = everything. DB backup first. Timeline rows →
    tl 'clear'; dims → dim 'delete'."""
    return _do_event_clear(before or None, after or None, last or None)


def main() -> None:
    storage.init_db(_DB).close()
    mcp.run()


if __name__ == "__main__":
    main()
