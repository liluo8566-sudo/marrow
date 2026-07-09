"""Marrow MCP server (stdio). Thin protocol shell over repo.py.

12-tool surface (07-06 rebuild): recall / atlas_lookup / event_embed / wish +
8 action-dispatch tools (tl / sticker / sticker_admin / goal / first /
dim / alert / event_clear). The session-start handoff is rendered by the
SessionStart hook. LLMClient wired so provider failures land in alerts.

Cortex-only (registered when MARROW_CORTEX in env): lie_down / say — subprocess
into the cortex repo, invisible to normal sessions.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import config, recall as _recall_mod, repo, storage
from .llm import LLMClient
from .timeutil import utc_iso_to_local_datetime, reltime_short

mcp = FastMCP("marrow")

def marrow_tool():
    """All marrow tools inject fully at session start (alwaysLoad).
    New tools MUST use this decorator, never bare @mcp.tool()."""
    return mcp.tool(meta={"anthropic/alwaysLoad": True})


# Cortex-only tools (lie_down / say) register into the schema only when the
# daemon subprocess was spawned by a cortex window (MARROW_CORTEX in env at
# import time — the window sets it explicitly). Normal sessions never see them.
_CORTEX = bool(os.environ.get("MARROW_CORTEX"))


def cortex_tool():
    """Register only in cortex sessions; no-op decorator elsewhere so the tool
    stays absent from the schema for normal sessions."""
    if _CORTEX:
        return marrow_tool()
    return lambda fn: fn

_DB = config.db_path()
llm = LLMClient(
    on_alert=lambda sev, t, m, s: repo.add_alert(sev, t, m, s, db=_DB)
)


@marrow_tool()
def recall(
    query: str,
    limit: int = 10,
    context: bool = False,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Recall events from db. Call when the user mention the past that you don't know.
    e.g. 你记得我上周说xxx？
    context=True attaches ±1 adjacent same-session turns.
    since/until: configured-local-timezone YYYY-MM-DD day strings.
    Diary: query='diary' + since/until."""
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
def atlas_lookup(prefix: str) -> list[dict]:
    """Look up atlas rows by path prefix — call before creating or naming files when location/naming is uncertain. Returns description + naming rules."""
    conn = storage.connect(_DB)
    try:
        from . import atlas
        return atlas.lookup_by_prefix(conn, prefix)
    finally:
        conn.close()


@marrow_tool()
def event_embed(batch: int = 50) -> dict:
    """Embed unvectorized events (write-time backfill). Returns count written."""
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


@marrow_tool()
def tl(
    action: str,
    timerange: str | None = None,
    body: str | None = None,
    n_word: str | None = None,
    y_word: str | None = None,
    importance: int | None = None,
    n_intensity: int | None = None,  # deprecated, kept for schema compat
    y_intensity: int | None = None,  # deprecated, kept for schema compat
    sid: str | None = None,
    event_id: int | None = None,
    before: str | None = None,
    after: str | None = None,
    match: str | None = None,
    date: str | None = None,
) -> dict:
    """Add/update/clear/query timeline.
    - 'query': find rows by match (content substring) and/or date (configured local timezone
      YYYY-MM-DD) -> [{event_id, line}]. Use it to look up an event_id.
    - 'update'/'clear': address a row by event_id, OR by match (+optional date)
      when you don't have the id. e.g. update match='千层' date='2026-07-05'.
    - 'clear': delete rows by event_id / match / sid / before+after range.
      Single row: no DB backup, deleted line returned. 2+ rows: DB backup + deleted lines (capped at 20).
    - Casual chat: When topic/location/mood change or task/activity done, 'add' one for previous turns
    - Coding/study sessions: keep 1tl each session - update tl only when things changed.
    - Frequency: every 1-2h or 10-20 turns - you can skip even when hook nudge you!!!
    - Format (add/update): HH:mm-HH:mm 【N affect♡Y affect (OR B affect)】body [i]
      - e.g. 21:25-21:31 【N愉悦♡Y委屈】翻CC日志找骂人梗，扑空互怼 [3]
      - N = user, Y = assistant, B = single affect when similar.
      - affect = emotion & feeling ONLY, 1-8 chars. e.g. 烦；心虚；紧张而激动；她好可爱呀～
        - NOT plot or behaviour summary. x【锐利督战】
        - Never pad - less char is better. Never mimic previous timelines.
      - i = ONE event-level composite (not per person): intensity (current state) * importance (future).
        - 1-2 = low-medium intensity & short-term e.g. Routine - casual chat, life admin, study, coding 无趣/平淡/轻松/烦躁
        - 3 = Both medium (~ 1 week) - funny moments / light quarrels / outing
        - 4 = Either high intensity or high imp - major conflict / final exam
        - 5 = Milestone (both high) - worth recording forever?
      - body <=30 chars, record real-world task/event + shared activities with assistant.
        - vivid not work-log; life details in, tech details out.
        - e.g. meals, casual chat topics, plays and tiny/silly/funny moments.
        - No third person — 我/你 only, never 她/他."""
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
                    n_word=n_word, y_word=y_word,
                    importance=importance, sid=sid,
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
                    n_word=n_word, y_word=y_word,
                    importance=importance,
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
    action: str,
    query: str | None = None,
    limit: int = 5,
    animated: bool = True,
    sticker_id: int | None = None,
) -> dict | list[dict]:
    """Send stickers to user when [channel: wx/tg]; NEVER on [channel: cli].
    Use stickers to express your feeling/thoughts; search by vibe/emotion
    then pick with the chosen id (bumps last_used), then send via
    <image path="..."/> or <gif path="..."/>. animated=false excludes GIFs."""
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
    action: str,
    image_path: str | None = None,
    desc: str | None = None,
    source: str = "wechat",
    sticker_id: int | None = None,
) -> dict | list[dict]:
    """Sticker library management. action='ingest': add an image file (safe
    on duplicates); 'update': rewrite a desc; 'delete': remove a sticker
    everywhere; 'pending': list stickers with missing/placeholder desc."""
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


@marrow_tool()
def goal(
    action: str,
    key: str | None = None,
    value: str | None = None,
    unit: str | None = None,
) -> dict | list[dict]:
    """Timetrack weekly goals e.g. study, sleep, exercise.
    action='set': create / update goals
    e.g. 'sleep goal 8h' → key='sleep' value='8' unit='h';
    'list'; 'delete' by key when dropped or achieved."""
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


@marrow_tool()
def wish(text: str) -> dict:
    """Our wishlist — personal wishes & cravings (hers and yours), promises
    made, and shared plans. e.g. 你说好请我喝奶茶 / 最近想买耳钉 / 约好周末去看海.
    This tool appends one line verbatim; update / delete = edit
    ~/.config/marrow/cortex/wishlist.md directly."""
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


# ── first ────────────────────────────────────────────────────────────────────

_FIRST_ACTIONS = {"tick", "untick", "list"}
_FIRST_STATUSES = {"done", "tried"}


@marrow_tool()
def first(
    action: str,
    item: str | None = None,
    note: str | None = None,
    sid: str | None = None,
    status: str = "done",
) -> dict | list[dict]:
    """Respond to the Cortex First section (notes/concerns injected into context).
    'tick' each item you acted on + a tiny note (1-10 chars), e.g. 处理好啦；等会儿再跟进。
    status='tried' when attempted but unsolved — note what blocked.
    'untick' a wrong ack; 'list' current ticks."""
    if action not in _FIRST_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}, expected one of {sorted(_FIRST_ACTIONS)}"}

    if action == "list":
        conn = storage.connect(_DB)
        try:
            rows = conn.execute(
                "SELECT item, seen_at, sid, note, status FROM ct_first_tick ORDER BY seen_at DESC"
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
    if status not in _FIRST_STATUSES:
        return {"ok": False, "error": f"status must be one of {sorted(_FIRST_STATUSES)}"}
    note = (note or "").strip() or None
    conn = storage.connect(_DB)
    try:
        if not sid:
            from .timeline import _query_current_sid
            sid = _query_current_sid(conn)
        with conn:
            conn.execute(
                "INSERT INTO ct_first_tick (item, seen_at, sid, note, status)"
                " VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?, ?)"
                " ON CONFLICT(item) DO UPDATE SET"
                " seen_at=excluded.seen_at, sid=excluded.sid, note=excluded.note,"
                " status=excluded.status",
                (item, sid, note, status),
            )
        return {"ok": True, "item": item, "sid": sid, "note": note, "status": status}
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
    action: str,
    kind: str | None = None,
    name: str | None = None,
    fact: str | None = None,
    aliases: list[str] | None = None,
    meme_type: str | None = None,
    date: str | None = None,
    scope: str = "me",
    id: int | None = None,
) -> dict | list[dict]:
    """Call for subpage edits (only 3 for now) - read/write/delete.
    - kind: person, pref, place, meme, milestone.
      - Entity (profile subpage): person, pref(偏好), place.
    - 'upsert': when recall misses something that clearly exists, or a hit
      shows stale/inaccurate info.
      - Fields: name, fact, date(milestone), aliases(entities optional)
      - Memes: type=paw/fact/news/event/others (paw+fact=personal/couple;
        other 3=public)
      - Milestones: scope=us/me
    - 'query': by kind and/or name — verify a write landed, get ids.
    - 'delete': by kind + id; query by name first if either unknown. Removes DB row + md line + tombstone."""
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
def alert(action: str, alert_id: int | None = None) -> dict | list[dict]:
    """list or solve alerts.
    action='list' unresolved alerts; 'resolve' by alert_id — auto-refreshes
    dashboard, restarts watcher if code changed."""
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

@mcp.tool()
def book_retention(text: str) -> str:
    """Push retention text to the shared-reading book server frontend (exit ceremony)."""
    import json as _json
    import urllib.request
    data = _json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        "http://localhost:3210/api/retention-push",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        return "Retention message pushed to reader frontend."
    except Exception as e:
        return f"Failed to push retention: {e}"


@mcp.tool()
def book_annotate(book_id: str, paragraph_id: str, text: str, chapter_id: str = "") -> str:
    """Write an annotation (as Leith) to a paragraph in the shared-reading book server."""
    import json as _json
    import urllib.request
    data = _json.dumps({"paragraphId": paragraph_id, "chapterId": chapter_id, "text": text}).encode()
    req = urllib.request.Request(
        f"http://localhost:3210/api/books/{book_id}/annotations/ai",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        result = _json.loads(resp.read())
        return f"Annotation written: {result.get('id', 'ok')}"
    except Exception as e:
        return f"Failed to write annotation: {e}"


@mcp.tool()
def book_message(text: str, message_type: str = "encourage") -> str:
    """Push a message to the shared-reading book reader frontend (encourage/health reminder)."""
    import json as _json
    import urllib.request
    data = _json.dumps({"text": text, "type": message_type}).encode()
    req = urllib.request.Request(
        "http://localhost:3210/api/message-push",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        return "Message pushed to reader frontend."
    except Exception as e:
        return f"Failed to push message: {e}"


def _book_get(path: str):
    import json as _json
    import urllib.request
    try:
        resp = urllib.request.urlopen(f"http://localhost:3210{path}", timeout=5)
        return _json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def book_list() -> list[dict]:
    """List all books on the shared-reading shelf with progress."""
    return _book_get("/api/books")


@mcp.tool()
def book_page(book_id: str, page: int = 1, size: int = 30) -> dict:
    """Read a page of book content (paragraphs). Returns {paragraphs, totalPages}."""
    return _book_get(f"/api/books/{book_id}/page/{page}?size={size}")


@mcp.tool()
def book_progress(book_id: str) -> dict:
    """Get current reading progress for a book."""
    return _book_get(f"/api/books/{book_id}/progress")


@mcp.tool()
def book_chapters(book_id: str) -> list[dict]:
    """List all chapters of a book with paragraph ranges."""
    return _book_get(f"/api/books/{book_id}/chapters")


@mcp.tool()
def book_annotations(book_id: str, chapter_id: str = "") -> list[dict]:
    """Read annotations (both frost and leith) for a book, optionally filtered by chapter."""
    q = f"?chapter={chapter_id}" if chapter_id else ""
    return _book_get(f"/api/books/{book_id}/annotations{q}")
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
def event_clear(before: str = "", after: str = "", last: int = 0) -> dict:
    """Delete raw events (recall corpus) incl. FTS+vectors+tombstones. Filters:
    before/after (ISO or YYYY-MM-DD); last=N most recent; none = all.
    DB backup first."""
    return _do_event_clear(before or None, after or None, last or None)


# ── cortex (lie_down / say) ───────────────────────────────────────────────────

def _cortex_paths() -> tuple[str, str]:
    """(venv_python, repo_root) from marrow config [cortex]; either empty =
    not configured. Both drive the cortex subprocess; repo_root is the cwd so
    `python -m cortex.X` resolves the package regardless of the caller's cwd."""
    c = config.load().get("cortex", {})
    return (str(c.get("venv_python") or "").strip(),
            str(c.get("repo_root") or "").strip())


def _run_cortex_module(module: str, extra_args: list[str] | None = None) -> dict:
    py, root = _cortex_paths()
    if not py or not root:
        return {"ok": False, "error": "cortex not configured "
                "([cortex].venv_python + repo_root in config.toml)"}
    py = str(Path(py).expanduser())
    root = str(Path(root).expanduser())
    cmd = [py, "-m", module] + (extra_args or [])
    try:
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{module} timed out after 30s"}
    except OSError as exc:
        return {"ok": False, "error": f"{module} failed to launch: {exc}"}
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or p.stdout or "").strip()
                or f"{module} exited {p.returncode}"}
    return {"ok": True, "stdout": (p.stdout or "").strip()}


@cortex_tool()
def lie_down(rotate: bool = False) -> dict:
    """End this wake. Write your handoff note (碎碎念) BEFORE calling — a PreToolUse
    guard denies lie_down (rotate or a large window) until the handoff is written
    this window. Clears due self_schedule, records tokens, redraws the floor.
    rotate=True respawns a fresh window on the next wake (you decide when the
    window is full — there is no auto rotate)."""
    return _run_cortex_module("cortex.lie_down", ["--rotate"] if rotate else None)


@cortex_tool()
def say() -> dict:
    """Quiet attention ping to her (no focus steal) — call once BEFORE speaking
    in-window; unsaid = silent activity. Then just say your words in the window."""
    return _run_cortex_module("cortex.say")


def main() -> None:
    storage.init_db(_DB).close()
    mcp.run()


if __name__ == "__main__":
    main()
