"""Marrow MCP server (stdio). Thin protocol shell over repo.py.

Core tool surface: recall / atlas_lookup / event_embed + action-dispatch tools
(tl / sticker / sticker_admin / dim / alert / event_clear). The session-start
handoff is rendered by the SessionStart hook. LLMClient wired so provider
failures land in alerts.

The optional cortex organs (wish / first / goal + cortex-session lie_down /
wait / say) live in cortex_bridge and install via cortex_bridge.register() only
when [cortex].enabled — a clean marrow shows none of them.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import config, cortex_bridge, outbox as _outbox, recall as _recall_mod, repo, storage
from .llm import LLMClient
from .timeutil import utc_iso_to_local_datetime, reltime_short

mcp = FastMCP("marrow")

def marrow_tool():
    """All marrow tools inject fully at session start (alwaysLoad).
    New tools MUST use this decorator, never bare @mcp.tool()."""
    return mcp.tool(meta={"anthropic/alwaysLoad": True})

_DB = config.db_path()
llm = LLMClient(
    on_alert=lambda sev, t, m, s: repo.add_alert(sev, t, m, s, db=_DB)
)

# Cortex organs (wish / first / goal + cortex-session lie_down / wait / say)
# install here only when [cortex].enabled; a clean marrow shows none of them.
cortex_bridge.register(marrow_tool, _DB)


@marrow_tool()
def recall(
    query: Annotated[str, Field(description="Search text; matched over the event corpus via fused semantic+FTS+recency. Empty/whitespace query with since+until returns that window's digest rows instead. query='diary' + since/until returns diary rows for the window.")],
    limit: Annotated[int, Field(ge=1, description="Max rows returned (default 10); also caps window-digest rows when query is empty.")] = 10,
    context: Annotated[bool, Field(description="When true, attaches ±1 adjacent same-session turns as _context to each non-dim event/task row (default false).")] = False,
    since: Annotated[str | None, Field(description="Lower time bound as a configured-local-timezone day string YYYY-MM-DD; converted to that day's start. Optional.")] = None,
    until: Annotated[str | None, Field(description="Upper time bound as a configured-local-timezone day string YYYY-MM-DD; converted to that day's end. Optional.")] = None,
) -> list[dict]:
    """Recall events from db. Call when the user mentions the past that you don't know.
    e.g. 你记得我上周说xxx？"""
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
    # Convert UTC timestamps to configured local timezone at the read boundary.
    # `when` is computed from the raw UTC string before conversion.
    # `_context` rows come straight from fetch_event_context (raw DB content —
    # unlike the main rows, which recall_with_config -> recall_fusion already
    # shapes via its own row passthrough, recall.py ~line 1817-1818). Mirror
    # that same shaping here so context turns don't leak the wx time-anchor
    # prefix or bare image/file tags into the MCP tool result.
    import re
    _ctx_time_prefix = re.compile(r"^\[time:[^\]]+\]\s*")
    _ctx_media_tag = re.compile(r'\s*<(?:image|file)\s+path="[^"]*?"[^>]*>\s*')
    for row in rows:
        ts = row.get("timestamp")
        if ts:
            row["when"] = reltime_short(ts)
            row["timestamp"] = utc_iso_to_local_datetime(ts)
        if "_context" in row:
            for c in row["_context"]:
                cts = c.get("timestamp")
                if cts:
                    c["timestamp"] = utc_iso_to_local_datetime(cts)
                content = _ctx_time_prefix.sub("", c.get("content") or "")
                c["content"] = _ctx_media_tag.sub(" ", content).strip()
    return rows


@marrow_tool()
def atlas_lookup(prefix: Annotated[str, Field(description="Filesystem path prefix (expanduser+resolved to absolute); returns atlas rows whose path equals it or sits under it as a path component, each with description + naming_hint + depth.")]) -> list[dict]:
    """Look up atlas rows by path prefix — call before creating or naming files when location/naming is uncertain."""
    conn = storage.connect(_DB)
    try:
        from . import atlas
        return atlas.lookup_by_prefix(conn, prefix)
    finally:
        conn.close()


@marrow_tool()
def event_embed(batch: Annotated[int, Field(ge=1, description="Max number of unvectorized events to embed this call (default 50); returns {embedded: count actually written}.")] = 50) -> dict:
    """Embed unvectorized events (write-time backfill)."""
    conn = storage.connect(_DB)
    try:
        n = _recall_mod.embed_pending(conn, batch=batch)
        return {"embedded": n}
    finally:
        conn.close()


# ── tl ───────────────────────────────────────────────────────────────────────

_TL_ACTIONS = {"add", "update", "clear", "query"}


def _like_escape(s: str) -> str:
    """Escape LIKE wildcards so user text matches literally, not as a pattern."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _tl_resolve(conn, match: str | None, date: str | None) -> list[dict]:
    """Find role='tl' rows by content substring (`match`) and/or configured-local-timezone
    day (`date`, YYYY-MM-DD). Returns [{event_id, line}] rendered with configured local timezone
    hh:mm, newest first, capped at 20. Raises ValueError on a malformed date."""
    clauses = ["role='tl'"]
    params: list = []
    if match:
        clauses.append("content LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_escape(match)}%")
    if date:
        from .timecue import melb_day_range
        since_utc, until_utc = melb_day_range(date)  # ValueError on bad date
        clauses.append("COALESCE(ts_start, timestamp) >= ?"
                       " AND COALESCE(ts_start, timestamp) < ?")
        params.extend([since_utc, until_utc])
    rows = conn.execute(
        "SELECT id, ts_start, ts_end, timestamp, content FROM events"
        f" WHERE {' AND '.join(clauses)}"
        " ORDER BY COALESCE(ts_start, timestamp) DESC LIMIT 20", params
    ).fetchall()
    from .timeline import _hhmm_local
    from . import tl_writer
    out = []
    for r in rows:
        ts_start = r["ts_start"] or r["timestamp"]
        hhmm_start = _hhmm_local(ts_start)
        hhmm_end = _hhmm_local(r["ts_end"]) if r["ts_end"] else None
        out.append({
            "event_id": r["id"],
            "line": tl_writer.render_line(hhmm_start, hhmm_end, r["content"]),
        })
    return out


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

    where, params = _tl_where(event_id, sid, before, after)
    conn = storage.connect(_DB)
    try:
        rows = conn.execute(
            f"SELECT id, ts_start, ts_end, timestamp, content FROM events"
            f" WHERE {where}", params).fetchall()
        if not rows:
            return {"ok": True, "cleared": 0}

        from .timeline import _hhmm_local
        from . import tl_writer
        lines = []
        for r in rows:
            ts_start = r["ts_start"] or r["timestamp"]
            hhmm_start = _hhmm_local(ts_start)
            hhmm_end = _hhmm_local(r["ts_end"]) if r["ts_end"] else None
            lines.append(tl_writer.render_line(hhmm_start, hhmm_end, r["content"]))
        ids = [r["id"] for r in rows]

        backup = None
        if len(ids) > 1:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            backup = f"/tmp/marrow-backup-tlclear-{ts}.db"
            shutil.copy2(str(_DB), backup)

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

    result = {"ok": True, "cleared": len(ids), "ids": ids}
    if len(lines) > 20:
        result["deleted"] = lines[:20]
        result["truncated"] = True
    else:
        result["deleted"] = lines
    if backup is not None:
        result["backup"] = backup
    return result


def tl(
    action: Annotated[str, Field(description="add / update / clear / query. update: only provided fields change. clear: 1 row = no DB backup, deleted line returned; 2+ rows = DB backup + deleted lines capped at 20.")],
    timerange: Annotated[str | None, Field(description="'HH:mm-HH:mm'.")] = None,
    body: Annotated[str | None, Field(description="Plain text <=30 chars. Real-world task/event + shared activities, vivid not work-log — life details in, tech details out (meals, chat topics, plays, tiny/silly/funny moments). 以assistant第一人称描述（我），user=“你”, never third person.")] = None,
    user_word: Annotated[str | None, Field(description="User's mood right now; 1-4 chars. e.g. 烦/心虚/紧张激动/好可爱. Single side fine. On update, providing either user_word or assistant_word replaces the whole label.")] = None,
    assistant_word: Annotated[str | None, Field(description="How you feel right now. Same rules as user_word.")] = None,
    importance: Annotated[int | None, Field(ge=1, le=5, description="ONE event-level composite (not per person): intensity (current) * importance (future). 1-2 = low-medium & short-term, routine (casual chat, life admin, study, coding); 3 = both medium ~1 week (funny moments, light quarrels, outing); 4 = either high (major conflict, final exam); 5 = milestone (both high — worth recording forever). Omitted -> 3 on add, kept on update.")] = None,
    sid: Annotated[str | None, Field(description="Session id. add: overrides the auto-resolved current session for the row. clear: delete all tl rows for this session (mutually exclusive with event_id and before/after).")] = None,
    event_id: Annotated[int | None, Field(description="Target tl row id for update/clear. Get it from a 'query' call.")] = None,
    before: Annotated[str | None, Field(description="clear only: delete tl rows with timestamp < this value (ISO). Combine with after for a range; mutually exclusive with event_id and sid.")] = None,
    after: Annotated[str | None, Field(description="clear only: delete tl rows with timestamp >= this value (ISO). Combine with before for a range; mutually exclusive with event_id and sid.")] = None,
    match: Annotated[str | None, Field(description="Content substring to resolve a row for query/update/clear when you don't have event_id; matched case-sensitively, newest-first, capped at 20. Must resolve to a single row for update/clear.")] = None,
    date: Annotated[str | None, Field(description="Optional YYYY-MM-DD, backdates the row.")] = None,
) -> dict:
    """Summarise each session into tl lines.
    Pass PARTS (timerange/user_word/assistant_word/body/importance) ONLY - code assemble rows.
    - Casual chat: when topic/location/mood change or task/activity done, add one for previous turns.
    - Coding/study sessions: keep 1 tl each session - update only when things changed.
    - Each session edits its own tl ONLY — never touch other sessions'; overlap is expected.
    - Frequency: every 1-2h or 10-20 turns - you can skip even when hook nudges you.
    - query: look up rows/event_id by match and/or date. update/clear: address a row
      by event_id, OR by match (+optional date). e.g. update match='千层' date='2026-07-05'."""
    if action not in _TL_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_TL_ACTIONS)}"}

    if action == "add":
        if not timerange or not body:
            return {"ok": False, "error": "add requires timerange and body"}
        conn = storage.connect(_DB)
        try:
            from . import tl_nudge
            _sid = sid
            if not _sid:
                from .timeline import _query_current_sid
                _sid = _query_current_sid(conn)
            if tl_nudge.is_silent(_sid):
                return {"ok": False, "silenced": True, "error": "session is silenced (/tl-)"}
            from . import tl_writer
            try:
                return tl_writer.tl_add(
                    conn, timerange, body,
                    user_word=user_word, assistant_word=assistant_word,
                    importance=importance, sid=sid, date=date,
                )
            except tl_writer.TlError as exc:
                return {"ok": False, "error": str(exc)}
        finally:
            conn.close()

    if action == "query":
        if not match and not date:
            return {"ok": False, "error": "query requires match and/or date"}
        conn = storage.connect(_DB)
        try:
            try:
                return {"ok": True, "matches": _tl_resolve(conn, match, date)}
            except ValueError as exc:
                return {"ok": False, "error": f"bad date {date!r}: {exc}"}
        finally:
            conn.close()

    if action == "update":
        if event_id is None and not (match or date):
            return {"ok": False, "error": "update requires event_id or match/date"}
        conn = storage.connect(_DB)
        try:
            from . import tl_nudge
            from .timeline import _query_current_sid
            _sid = _query_current_sid(conn)
            if tl_nudge.is_silent(_sid):
                return {"ok": False, "silenced": True, "error": "session is silenced (/tl-)"}
            explicit_id = event_id is not None
            if event_id is None:
                try:
                    hits = _tl_resolve(conn, match, date)
                except ValueError as exc:
                    return {"ok": False, "error": f"bad date {date!r}: {exc}"}
                if not hits:
                    return {"ok": False, "error": "no tl row matches"}
                if len(hits) > 1:
                    return {"ok": False, "error": "multiple matches — refine or pass event_id",
                            "matches": hits}
                event_id = hits[0]["event_id"]
            from . import tl_writer
            try:
                result = tl_writer.tl_update(
                    conn, event_id, timerange=timerange, body=body,
                    user_word=user_word, assistant_word=assistant_word,
                    importance=importance,
                    date=date if explicit_id else None,
                )
            except tl_writer.TlError as exc:
                return {"ok": False, "error": str(exc)}
            tl_nudge.reset(_sid)
            return result
        finally:
            conn.close()

    # clear
    if event_id is None and (match or date) and not sid and not before and not after:
        conn = storage.connect(_DB)
        try:
            try:
                hits = _tl_resolve(conn, match, date)
            except ValueError as exc:
                return {"ok": False, "error": f"bad date {date!r}: {exc}"}
        finally:
            conn.close()
        if not hits:
            return {"ok": False, "error": "no tl row matches"}
        if len(hits) > 1:
            return {"ok": False, "error": "multiple matches — refine or pass event_id",
                    "matches": hits}
        event_id = hits[0]["event_id"]
    return _tl_clear(event_id=event_id, sid=sid, before=before, after=after)


# Inject persona markers into the tl docstring before MCP registration reads it.
_tl_persona = config.persona()
tl.__doc__ = (tl.__doc__ or "").replace(
    "{u}", _tl_persona["user_marker"]).replace(
    "{a}", _tl_persona["assistant_marker"])
tl = marrow_tool()(tl)


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


@marrow_tool()
def sticker(
    action: Annotated[str, Field(description="'search' to find stickers by vibe/emotion, or 'pick' to record a chosen sticker (bumps last_used).")],
    query: Annotated[str | None, Field(description="search only: free-text vibe/emotion query; empty returns nothing. Uses vector match when available, else falls back to per-term desc LIKE.")] = None,
    limit: Annotated[int, Field(ge=1, description="search only: max stickers returned (default 5).")] = 5,
    animated: Annotated[bool, Field(description="search only: when false, excludes .gif animated stickers (default true = include).")] = True,
    sticker_id: Annotated[int | None, Field(description="pick only: id of the sticker to mark used. Required for pick.")] = None,
) -> dict | list[dict]:
    """Send stickers to user when [channel: wx/tg]; NEVER on [channel: cli].
    Use stickers to express your feeling/thoughts; send the picked one via
    <image path="..."/> or <gif path="..."/>."""
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
        from .sticker_ops import delete_sticker, _purge_md_lines
        result = delete_sticker(conn, sticker_id)
        if result.get("ok"):
            _write_stickers_subpage(conn)
            # The stickers inserter is upsert-only and never removes a block
            # whose DB row is gone, so strip the stale anchored line directly.
            _purge_md_lines(config.db_pages_path(), [sticker_id])
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


@marrow_tool()
def sticker_admin(
    action: Annotated[str, Field(description="One of ingest / update / delete / pending.")],
    image_path: Annotated[str | None, Field(description="ingest only: path to the image file to add. Required for ingest; safe (no-op result) on duplicates.")] = None,
    desc: Annotated[str | None, Field(description="ingest/update: description text for the sticker. Required for both (ingest and update).")] = None,
    source: Annotated[str, Field(description="ingest only: origin tag stored on the sticker (default 'wechat').")] = "wechat",
    sticker_id: Annotated[int | None, Field(description="update/delete: id of the target sticker. Required for update and delete.")] = None,
) -> dict | list[dict]:
    """Sticker library management. 'delete': remove a sticker everywhere.
    'pending': list stickers with missing/placeholder desc."""
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


def _dim_upsert_meme(name: str, fact: str | None, meme_type: str | None) -> dict:
    from . import memes_dedup

    key = name
    value = (fact or "").strip() or None
    vtype_given = (meme_type or "").strip() or None
    if vtype_given and vtype_given not in _DIM_MEME_TYPES:
        return {"ok": False, "error": f"meme_type must be one of {sorted(_DIM_MEME_TYPES)}"}
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
                    " pinned=1,"
                    " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (new_type, value, existing["id"]),
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
                "INSERT INTO memes (type, key, value, pinned,"
                " source_hash, updated_at)"
                " VALUES (?, ?, ?, 1, 'dim_upsert',"
                " strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (vtype, key, value),
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
                date: str | None, scope: str) -> dict:
    kind = (kind or "").strip()
    if kind not in _DIM_KINDS:
        return {"ok": False, "error": f"kind must be one of {sorted(_DIM_KINDS)}"}
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    if kind in ("person", "pref", "place"):
        return _dim_upsert_entity(kind, name, fact, aliases)
    if kind == "meme":
        return _dim_upsert_meme(name, fact, meme_type)
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
                sql = "SELECT id, type, key, value, pinned, status FROM memes"
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


@marrow_tool()
def dim(
    action: Annotated[str, Field(description="One of upsert / query / delete.")],
    kind: Annotated[str | None, Field(description="Dimension kind: person, pref, place, meme, or milestone. Required for upsert and delete; optional filter for query (omitted = all kinds).")] = None,
    name: Annotated[str | None, Field(description="upsert: the entity/meme/milestone name (or meme key / milestone title); required. query: substring filter over name+fact (aliases for entities); omitted = no filter.")] = None,
    fact: Annotated[str | None, Field(description="upsert: the fact/description/meme value stored on the row; optional (blank kept as null). On update, null leaves the existing value unchanged.")] = None,
    aliases: Annotated[list[str] | None, Field(description="upsert of person/pref/place only: alternate names merged into the entity. Ignored for meme/milestone.")] = None,
    meme_type: Annotated[str | None, Field(description="upsert of a meme only: one of paw/fact/news/event/others (paw+fact=personal/couple, other 3=public). Omitted -> 'others' on create, keeps existing type on update.")] = None,
    date: Annotated[str | None, Field(description="upsert of a milestone only: YYYY-MM-DD or YYYY. Required for milestone; part of its dedup key.")] = None,
    scope: Annotated[str, Field(description="upsert of a milestone only: 'us' (couple) or 'me' (default 'me'; any other value coerced to 'me').")] = "me",
    id: Annotated[int | None, Field(description="delete only: numeric row id of the item to remove. Required for delete; find it via a query first.")] = None,
) -> dict | list[dict]:
    """Call for subpage edits (only 3 for now) - read/write/delete.
    - 'upsert': when recall misses something that clearly exists, or a hit
      shows stale/inaccurate info.
    - 'query': verify a write landed, get ids.
    - 'delete': query by name first if kind/id unknown. Removes DB row + md line + tombstone."""
    if action not in _DIM_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_DIM_ACTIONS)}"}
    if action == "upsert":
        return _dim_upsert(kind, name, fact, aliases, meme_type, date, scope)
    if action == "query":
        return _dim_query(kind, name)
    return _dim_delete(kind, id)


# ── alert ────────────────────────────────────────────────────────────────────

_ALERT_ACTIONS = {"list", "resolve"}


@marrow_tool()
def alert(
    action: Annotated[str, Field(description="'list' unresolved alerts (newest first), or 'resolve' one by alert_id.")],
    alert_id: Annotated[int | None, Field(description="resolve only: id of the alert to resolve (via `mw resolve`, which refreshes the dashboard and restarts the watcher if code changed). Required for resolve.")] = None,
) -> dict | list[dict]:
    """List or resolve alerts."""
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


# ── msg ───────────────────────────────────────────────────────────────────────

_MSG_ACTIONS = {"send", "list"}


@marrow_tool()
def msg(
    action: Annotated[str, Field(description="'send' a message, or 'list' your own recent outbox rows (debugging).")],
    to: Annotated[str | None, Field(description="send only: tg | wx | cli | ct | session:<sid-prefix>. tg/wx = her phone (whitelisted senders only); cli = any cli session; ct = cortex; session:<prefix> resolves to exactly one live session (0 or many matches = refused).")] = None,
    text: Annotated[str | None, Field(description="send only: message body (plain text). Required.")] = None,
    watch_reply: Annotated[bool, Field(description="send only: be kicked awake the moment she replies on the target channel (default false).")] = False,
    watch_timeout_min: Annotated[int | None, Field(description="send only: check back at N minutes: kicked only if no reply by then; if she already replied the watch clears silently (default none = no timeout watch).")] = None,
    limit: Annotated[int, Field(ge=1, description="list only: max rows to return (default 20).")] = 20,
) -> dict | list[dict]:
    """Leave a message across channels: to her phone (tg/wx, whitelisted senders) or covertly to another session (cli/ct). The resident session continues that conversation. Set watch_reply=true to be kicked awake the moment she replies; watch_timeout_min=N to check back at N minutes — kicked only if she hasn't replied by then.
    - 'send': needs `to` + `text`; tg/wx restricted to allowed sender channels.
    - 'list': your own pending/recent rows to confirm a send landed."""
    if action not in _MSG_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_MSG_ACTIONS)}"}
    if action == "list":
        return _outbox.list_recent(limit=limit, db=_DB)
    if not to:
        return {"ok": False, "error": "send requires `to`"}
    if not text:
        return {"ok": False, "error": "send requires `text`"}
    return _outbox.send(
        to, text, watch_reply=watch_reply,
        watch_timeout_min=watch_timeout_min, db=_DB,
    )


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


def _tombstone_deleted(conn, select_sql: str, params, reason: str) -> None:
    """Record event_tombstones for rows about to be deleted, so a later
    catchup/SessionEnd re-archive can't resurrect them (mirrors
    clean_harness_events.py). Keyed by the existing events.source_hash;
    rows with NULL source_hash are skipped by the caller's SQL."""
    hashes = [r[0] for r in conn.execute(select_sql, params).fetchall()]
    if hashes:
        conn.executemany(
            "INSERT OR IGNORE INTO event_tombstones (source_hash, reason)"
            " VALUES (?, ?)", [(h, reason) for h in hashes])


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
            _sub = "(SELECT id FROM events ORDER BY timestamp DESC LIMIT ?)"
            sids = [r[0] for r in conn.execute(
                "SELECT DISTINCT session_id FROM events WHERE id IN "
                + _sub, [last]).fetchall()]
            _tombstone_deleted(
                conn,
                "SELECT source_hash FROM events WHERE id IN " + _sub
                + " AND source_hash IS NOT NULL",
                [last], "event_clear: last=N")
            conn.execute("DELETE FROM events WHERE id IN " + _sub, [last])
            if sids:
                conn.executemany(
                    "DELETE FROM audit_log WHERE action='sessionend_extract' AND target_id=?",
                    [(s,) for s in sids])
            counts["events"] = last
        elif time_filtered:
            where, params = _time_where("timestamp", before, after)
            sids = [r[0] for r in conn.execute(
                "SELECT DISTINCT session_id FROM events" + where, params).fetchall()]
            _tombstone_deleted(
                conn,
                "SELECT source_hash FROM events" + where
                + " AND source_hash IS NOT NULL", params,
                "event_clear: range")
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
            # Triggers are dropped above so events_ad_vec does not cascade —
            # clear meta manually or freed ids inherit orphan meta rows that
            # poison the vec dedup on reuse.
            conn.execute("DELETE FROM events_vec_meta")
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


@marrow_tool()
def event_clear(
    before: Annotated[str, Field(description="Delete events with timestamp < this (ISO or YYYY-MM-DD). Combine with after for a range; mutually exclusive with last. Empty = no bound.")] = "",
    after: Annotated[str, Field(description="Delete events with timestamp >= this (ISO or YYYY-MM-DD). Combine with before for a range; mutually exclusive with last. Empty = no bound.")] = "",
    last: Annotated[int, Field(ge=0, description="Delete the N most recent events; mutually exclusive with before/after. 0 = unused. With no before/after/last set, ALL events are purged.")] = 0,
) -> dict:
    """Delete raw events (recall corpus) incl. FTS+vectors+tombstones. DB backup first."""
    return _do_event_clear(before or None, after or None, last or None)


def main() -> None:
    storage.init_db(_DB).close()
    mcp.run()


if __name__ == "__main__":
    main()
