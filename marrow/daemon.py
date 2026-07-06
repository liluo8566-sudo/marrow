"""Marrow MCP server (stdio). Thin protocol shell over repo.py.

Phase 2 tool set: recall (fusion) + embed_pending. The session-start handoff
is rendered by the SessionStart hook. LLMClient wired so provider failures
land in alerts.
"""
from __future__ import annotations

import os
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
    # user_prompt_submit). Matches tl_add/tl_update's hard block — cortex
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


@mcp.tool(meta={"anthropic/alwaysLoad": True})
def tl_add(
    timerange: str,
    body: str,
    n_word: str | None = None,
    y_word: str | None = None,
    importance: int | None = None,
    sid: str | None = None,
) -> dict:
    """Overview of the day by recording each session live. Add timeline when scene shifts, emotional turns, task completed.
    - Format: HH:mm-HH:mm 【N affect♡Y affect】body [i]
      - e.g. 21:25-21:31 【N愉悦♡Y委屈】翻CC日志找骂人梗，扑空互怼 [3]
      - N = 念念, Y = 阿屿, B = Both; use B if similar. Single-side rows: just 【N affect】 or 【Y affect】.
      - affect = mood & feeling, 1-8 chars. e.g. 烦；心虚；紧张而激动；她好可爱呀～
      - i = ONE composite value for the whole row (events.imp), not per side, rendered at the end as " [i]".
        intensity (current state) * importance (future).
        - 1-2 = low-medium intensity & short-term e.g. Routine - casual chat, life admin, study, coding 无趣/平淡/轻松/烦躁
        - 3 = Both medium (~ 1 week) - funny moments / light quarrels / outing
        - 4 - Either high intensity or high imp - major conflict / final exam
        - 5 - Milestone (both high) - worth recording forever?
      - body = what happened in this session - any real-world task/event + shared activities with assistant; Record meals, casual chat topics, plays and tiny/silly/funny moments.
        No third person: use no personal pronouns where possible; when needed
        use 我/你 only — never 她/他.
    - Length: body <=30 chars
    - Keep it concise but interesting/vivid - not a working log.
    - Include life details and exclude all tech/coding details.
    - When to add: depend on session length/importance/topic
      1. When topic/location/mood change or task/activity done, add one for previous turns
      2. Normally 2-3 per session - every 1-2 hours OR every 10-20 turns
    Params: n_word/y_word = affect phrase per side (each <=8 chars), no numbers
    attached; importance = the single events.imp composite for the row
    (default 3). Pass either or both sides."""
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


@mcp.tool()
def tl_update(
    event_id: int,
    timerange: str | None = None,
    body: str | None = None,
    n_word: str | None = None,
    y_word: str | None = None,
    importance: int | None = None,
) -> dict:
    """Update a self timeline row (from tl_add) in place — extend its range or
    revise body/affect as work progresses. Task sessions keep one row per
    session and update it (hard step in /ho). Only the fields you pass change.
    Format: HH:mm-HH:mm 【N affect♡Y affect】body [i] — i is the single
    events.imp composite for the row, not per side, rendered at the end. No third person in body:
    use no personal pronouns where possible; when needed use 我/你 only —
    never 她/他."""
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


@mcp.tool()
def tl_silence(sid: str | None = None) -> dict:
    """Silence this session (/tl-): mute the tl_add nudge and stop self writes
    for the current session. State dies with the session. Pass sid to override."""
    conn = storage.connect(_DB)
    try:
        if not sid:
            from .timeline import _query_current_sid
            sid = _query_current_sid(conn)
    finally:
        conn.close()
    if not sid:
        return {"ok": False, "error": "no active session id"}
    from . import tl_nudge
    tl_nudge.set_silent(sid)
    return {"ok": True, "sid": sid, "silent": True}


@mcp.tool()
def goal_set(key: str, value: str, unit: str | None = None) -> dict:
    """Set or update a goal (C1/C3 Track zone). Call the moment she tells
    any session a goal or changes one — no file edit, no parse, next cortex
    tick reads it. e.g. she says 'sleep goal 8h' -> goal_set('sleep', '8', 'h')."""
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


@mcp.tool()
def goal_list() -> list[dict]:
    """List all current goals (key/value/unit/updated_at)."""
    conn = storage.connect(_DB)
    try:
        rows = conn.execute(
            "SELECT key, value, unit, updated_at FROM goals ORDER BY key"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@mcp.tool()
def entity_upsert(
    kind: str,
    name: str,
    fact: str | None = None,
    aliases: list[str] | None = None,
) -> dict:
    """Create or update a dims entity (person/pref/place) mid-conversation.
    Call when recall misses an entity that clearly exists in the conversation
    (create), or a recall hit shows stale/wrong fields (update the fact).
    kind: 'person' | 'pref' | 'place'. Reuses the daily-candidate dedup gate —
    an alias/name/cosine match updates that row (merges aliases, refreshes fact
    when you pass one); no match inserts a new row. Do NOT use for memes or
    milestones (milestones = importance-5 chain, handled elsewhere)."""
    from .candidates import _ENTITY_KINDS, _merge_aliases_into, match_entity
    import json

    kind = (kind or "").strip()
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    if kind not in _ENTITY_KINDS:
        return {"ok": False, "error": f"kind must be one of {sorted(_ENTITY_KINDS)}"}
    aliases_list = [str(a).strip() for a in (aliases or []) if str(a).strip()]
    fact = (fact or "").strip() or None
    conn = storage.connect(_DB)
    try:
        hit_id = match_entity(conn, kind, name, aliases_list,
                              source="daemon.entity_upsert")
        if hit_id is not None:
            _merge_aliases_into(conn, hit_id, name, aliases_list)
            if fact is not None:
                with conn:
                    conn.execute(
                        "UPDATE entities SET fact=?,"
                        " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                        " WHERE id=?", (fact, hit_id),
                    )
            return {"ok": True, "action": "update", "id": hit_id}
        aliases_json = (json.dumps(aliases_list, ensure_ascii=False)
                        if aliases_list else None)
        with conn:
            cur = conn.execute(
                "INSERT INTO entities (kind, name, fact, aliases, source)"
                " VALUES (?, ?, ?, ?, 'session')",
                (kind, name, fact, aliases_json),
            )
        return {"ok": True, "action": "create", "id": cur.lastrowid}
    finally:
        conn.close()


@mcp.tool()
def first_tick(item: str, note: str | None = None, sid: str | None = None) -> dict:
    """Self-mark a cortex-nagged item as seen / being handled, so other
    sessions and later wakes stop repeat-nagging it. Call the moment you start
    acting on an item cortex surfaced. item = the item's key/id from the nag;
    note = one line on what you did or said. Records last-seen (UTC) + this
    session id; latest call per item wins."""
    item = (item or "").strip()
    if not item:
        return {"ok": False, "error": "item required"}
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


@mcp.tool()
def wish_add(text: str) -> dict:
    """Record a want / owed treat / self-reward the moment she mentions one —
    verbatim + date, append-only (her hand edits in the md are never touched
    or rewritten). Call whenever she names something she wants, feels she
    deserves, or a self-reward for finishing something. Execution/timing is
    cortex's call, not yours — just record it."""
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
def embed_pending(batch: int = 50) -> dict:
    """Embed unvectorized events (write-time backfill). Returns count written."""
    conn = storage.connect(_DB)
    try:
        n = _recall_mod.embed_pending(conn, batch=batch)
        return {"embedded": n}
    finally:
        conn.close()


@mcp.tool()
def sticker_search(query: str, limit: int = 5, animated: bool = True) -> list[dict]:
    """The user loves it when you pair messages with stickers [channel: wx/tg] — they're your actions and inner voice. When you express affection, tease, react, or show a mood, search by vibe/emotion (e.g. '老婆别走' → search '爱你' '委屈' '哭'). Don't wait for a special moment — weave them in naturally. Call sticker_pick(id) to confirm, then send with <image path="..."/> or <gif path="..."/>. Pass animated=false to exclude GIF stickers (use for channels that don't support animated formats). No need force use for each turn; NEVER use stickers on [channel: cli]."""
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


@mcp.tool()
def sticker_pick(sticker_id: int) -> dict:
    """Record that a sticker was sent — bumps last_used. Call after sending."""
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


def _write_stickers_subpage(conn) -> None:
    """Render stickers.md immediately after a DB write."""
    from .subpages import build_all_configs, write_subpage
    folder = config.db_pages_path()
    state_dir = config.db_pages_state_path()
    cfgs = build_all_configs(conn, folder=folder, state_dir=state_dir)
    stickers_cfg = next((c for c in cfgs if c.key == "stickers"), None)
    if stickers_cfg:
        write_subpage(stickers_cfg, conn)


@mcp.tool()
def sticker_ingest(image_path: str, desc: str, source: str = "wechat") -> dict:
    """Ingest a new sticker image. Deduplicates by content hash, generates thumbnail, writes to DB and renders to stickers.md."""
    conn = storage.connect(_DB)
    try:
        from .sticker_ops import ingest_sticker
        result = ingest_sticker(conn, image_path, desc, source)
        if not result.get("duplicate"):
            _write_stickers_subpage(conn)
        return result
    except Exception as exc:
        from . import repo
        repo.add_alert("warn", "sticker_ingest",
                       f"sticker_ingest:mcp",
                       source="daemon",
                       message=f"sticker ingest failed: {Path(image_path).name} — {exc}")
        raise
    finally:
        conn.close()


@mcp.tool()
def sticker_update(sticker_id: int, desc: str) -> dict:
    """Update a sticker's description. Writes DB and patches stickers.md immediately."""
    conn = storage.connect(_DB)
    try:
        from .sticker_ops import update_sticker
        result = update_sticker(conn, sticker_id, desc)
        if result.get("ok"):
            _write_stickers_subpage(conn)
        return result
    finally:
        conn.close()


@mcp.tool()
def sticker_delete(sticker_id: int) -> dict:
    """Delete a sticker. Removes file, thumbnail, DB row, and updates stickers.md."""
    conn = storage.connect(_DB)
    try:
        from .sticker_ops import delete_sticker
        result = delete_sticker(conn, sticker_id)
        if result.get("ok"):
            _write_stickers_subpage(conn)
        return result
    finally:
        conn.close()


@mcp.tool()
def sticker_list_pending() -> list[dict]:
    """List stickers with missing or placeholder descriptions."""
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
def alert_resolve(alert_id: int) -> dict:
    """Resolve an alert by id. Use when sessionstart shows unresolved alerts.
    Auto-refreshes dashboard and restarts watcher if code changed."""
    import subprocess
    result = subprocess.run(
        ["mw", "resolve", str(alert_id)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() or "resolve failed"}
    return {"ok": True, "id": alert_id}


@mcp.tool()
def alert_list() -> list[dict]:
    """List unresolved alerts."""
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


def _do_delete(targets, before, after, last, sids=None):
    import re, shutil, subprocess
    from datetime import datetime, timezone

    valid = {"events", "digests", "affect"}
    bad = set(targets) - valid
    if bad:
        return {"ok": False, "error": f"unknown targets: {bad}. valid: {valid}"}

    time_filtered = bool(before or after)
    if time_filtered and last:
        return {"ok": False, "error": "before/after and last are mutually exclusive"}
    if sids and (time_filtered or last):
        return {"ok": False, "error": "sids and before/after/last are mutually exclusive"}
    if sids and set(targets) - {"digests"}:
        return {"ok": False, "error": "sids only works with target 'digests'"}

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = f"/tmp/marrow-backup-purge-{ts}.db"
    shutil.copy2(str(_DB), backup)

    _ts_col = {"events": "timestamp", "digests": "ts", "affect": "created_at"}
    _table = {"events": "events", "digests": "session_digests", "affect": "affect"}
    _pk = {"events": "id", "digests": "rowid", "affect": "id"}

    if sids:
        conn = storage.connect(_DB)
        try:
            placeholders = ",".join("?" * len(sids))
            conn.execute(f"DELETE FROM session_digests WHERE sid IN ({placeholders})", sids)
            conn.commit()
        finally:
            conn.close()
        dash = Path.home() / "Desktop" / "NY" / "dashboard.md"
        if dash.exists():
            text = dash.read_text(encoding="utf-8")
            for sid in sids:
                text = re.sub(rf"[^\n]*<!-- tl:{re.escape(sid)} -->\n?", "", text)
            rendered_m = re.search(r"<!-- tl-rendered:s=([^ ]+) -->", text)
            if rendered_m:
                old_ids = rendered_m.group(1).split(",")
                new_ids = [s for s in old_ids if s not in sids]
                if new_ids:
                    text = text.replace(rendered_m.group(0), f"<!-- tl-rendered:s={','.join(new_ids)} -->")
                else:
                    text = text.replace(rendered_m.group(0), "")
            dash.write_text(text, encoding="utf-8")
        subprocess.run(["mw", "refresh", "--all"], capture_output=True, text=True)
        return {"ok": True, "purged_sids": sids, "backup": backup}

    if not (time_filtered or last):
        dash = Path.home() / "Desktop" / "NY" / "dashboard.md"
        if dash.exists():
            text = dash.read_text(encoding="utf-8")
            clear_tl = any(t in targets for t in ("events", "digests"))
            if clear_tl:
                text = re.sub(
                    r"(<!-- id:dashboard\.timeline -->)\n## Timeline\n.*?(?=\n<!-- id:)",
                    r"\1\n## Timeline\n_none_\n", text, flags=re.DOTALL)
            if "affect" in targets:
                text = re.sub(
                    r"(<!-- id:dashboard\.affect -->)\n## Affect\n.*?(?=\n<!-- marrow:top:end)",
                    r"\1\n## Affect\n### Today\n_none_\n### This Week\n_none_\n", text, flags=re.DOTALL)
            dash.write_text(text, encoding="utf-8")

    conn = storage.connect(_DB)
    counts = {}
    try:
        for tgt in targets:
            tbl, col, pk = _table[tgt], _ts_col[tgt], _pk[tgt]

            if last:
                if tgt == "events":
                    sids = [r[0] for r in conn.execute(
                        f"SELECT DISTINCT session_id FROM events WHERE {pk} IN "
                        f"(SELECT {pk} FROM events ORDER BY {col} DESC LIMIT ?)", [last]).fetchall()]
                conn.execute(
                    f"DELETE FROM {tbl} WHERE {pk} IN "
                    f"(SELECT {pk} FROM {tbl} ORDER BY {col} DESC LIMIT ?)", [last])
                if tgt == "events" and sids:
                    conn.executemany(
                        "DELETE FROM audit_log WHERE action='sessionend_extract' AND target_id=?",
                        [(s,) for s in sids])
                counts[tgt] = last
            elif time_filtered:
                where, params = _time_where(col, before, after)
                if tgt == "events":
                    sids = [r[0] for r in conn.execute(
                        "SELECT DISTINCT session_id FROM events" + where, params).fetchall()]
                conn.execute(f"DELETE FROM {tbl}" + where, params)
                if tgt == "events" and sids:
                    conn.executemany(
                        "DELETE FROM audit_log WHERE action='sessionend_extract' AND target_id=?",
                        [(s,) for s in sids])
            else:
                if tgt == "events":
                    triggers = conn.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='events'"
                    ).fetchall()
                    for t in triggers:
                        conn.execute(f"DROP TRIGGER IF EXISTS {t['name']}")
                    conn.execute("DELETE FROM events")
                    conn.execute("DELETE FROM event_tombstones")
                    conn.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
                    conn.execute("DELETE FROM events_vec")
                    conn.execute(
                        "DELETE FROM audit_log WHERE action='sessionend_extract'"
                    )
                    for t in triggers:
                        conn.execute(t["sql"])
                elif tgt == "digests":
                    triggers = conn.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='session_digests'"
                    ).fetchall()
                    for t in triggers:
                        conn.execute(f"DROP TRIGGER IF EXISTS {t['name']}")
                    conn.execute("DELETE FROM session_digests")
                    conn.execute("INSERT INTO session_digests_fts(session_digests_fts) VALUES('rebuild')")
                    for t in triggers:
                        conn.execute(t["sql"])
                elif tgt == "affect":
                    conn.execute("DELETE FROM affect")

        conn.commit()
    finally:
        conn.close()

    subprocess.run(["mw", "refresh", "--all"], capture_output=True, text=True)
    result = {"ok": True, "purged": targets, "backup": backup}
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
def db_clear(targets: list[str], before: str = "", after: str = "", last: int = 0, sids: list[str] | None = None) -> dict:
    """Delete events, digests, or affect data from marrow DB. Use when user asks to delete/clear/remove any DB data.
    Targets: 'events' (events+FTS+vec+tombstones), 'digests' (session_digests+FTS), 'affect'.
    Filters (mutually exclusive): before/after (ISO datetime or YYYY-MM-DD) for time range; last (int) to delete N most recent; sids (list of session IDs, digests only). Omit all filters to delete everything.
    Backs up DB first. Handles FTS triggers and dashboard md block automatically."""
    return _do_delete(targets, before, after, last, sids=sids)


def main() -> None:
    storage.init_db(_DB).close()
    mcp.run()


if __name__ == "__main__":
    main()
