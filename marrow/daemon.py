"""Marrow MCP server (stdio). Thin protocol shell over repo.py.

Phase 2 tool set: recall (fusion) + embed_pending. The session-start handoff
is rendered by the SessionStart hook. LLMClient wired so provider failures
land in alerts.
"""
from __future__ import annotations

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
    """The user loves it when you pair messages with stickers — they're your actions and inner voice. When you express affection, tease, react, or show a mood, search by vibe/emotion (e.g. '老婆别走' → search '爱你' '委屈' '哭'). Don't wait for a special moment — weave them in naturally. Call sticker_pick(id) to confirm, then send with <image path="..."/> or <gif path="..."/>. Pass animated=false to exclude GIF stickers (use for channels that don't support animated formats)."""
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


def _do_delete(targets, before, after, last):
    import re, shutil, subprocess
    from datetime import datetime, timezone

    valid = {"events", "digests", "affect", "tl_line"}
    bad = set(targets) - valid
    if bad:
        return {"ok": False, "error": f"unknown targets: {bad}. valid: {valid}"}

    time_filtered = bool(before or after)
    if time_filtered and last:
        return {"ok": False, "error": "before/after and last are mutually exclusive"}

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = f"/tmp/marrow-backup-purge-{ts}.db"
    shutil.copy2(str(_DB), backup)

    _ts_col = {"events": "timestamp", "digests": "ts", "affect": "created_at", "tl_line": "date"}
    _table = {"events": "events", "digests": "session_digests", "affect": "affect", "tl_line": "diary"}
    _pk = {"events": "id", "digests": "rowid", "affect": "id", "tl_line": "date"}

    if not (time_filtered or last):
        dash = Path.home() / "Desktop" / "NY" / "dashboard.md"
        if dash.exists():
            text = dash.read_text(encoding="utf-8")
            clear_tl = any(t in targets for t in ("events", "digests", "tl_line"))
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

            if last and tgt == "tl_line":
                conn.execute(
                    f"UPDATE diary SET tl_line = NULL WHERE {pk} IN "
                    f"(SELECT {pk} FROM {tbl} WHERE tl_line IS NOT NULL ORDER BY {col} DESC LIMIT ?)", [last])
                counts[tgt] = last
            elif last:
                conn.execute(
                    f"DELETE FROM {tbl} WHERE {pk} IN "
                    f"(SELECT {pk} FROM {tbl} ORDER BY {col} DESC LIMIT ?)", [last])
                counts[tgt] = last
            elif time_filtered:
                where, params = _time_where(col, before, after)
                if tgt == "tl_line":
                    conn.execute("UPDATE diary SET tl_line = NULL" + where, params)
                else:
                    conn.execute(f"DELETE FROM {tbl}" + where, params)
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
                elif tgt == "tl_line":
                    conn.execute("UPDATE diary SET tl_line = NULL")

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
def event_delete(before: str = "", after: str = "", last: int = 0) -> dict:
    """Delete events from DB (events + FTS + vec + tombstones). Use when user asks to delete/clear/remove events.
    Optional: before/after (ISO datetime or YYYY-MM-DD) for time range; last (int) to delete N most recent. Omit all to delete everything."""
    return _do_delete(["events"], before, after, last)

@mcp.tool()
def digest_delete(before: str = "", after: str = "", last: int = 0) -> dict:
    """Delete session digests from DB (session_digests + FTS). Use when user asks to delete/clear/remove digests or session summaries.
    Optional: before/after (ISO datetime or YYYY-MM-DD) for time range; last (int) to delete N most recent. Omit all to delete everything."""
    return _do_delete(["digests"], before, after, last)

@mcp.tool()
def affect_delete(before: str = "", after: str = "", last: int = 0) -> dict:
    """Delete affect entries from DB. Use when user asks to delete/clear/remove affect or emotion data.
    Optional: before/after (ISO datetime or YYYY-MM-DD) for time range; last (int) to delete N most recent. Omit all to delete everything."""
    return _do_delete(["affect"], before, after, last)

@mcp.tool()
def timeline_delete(before: str = "", after: str = "", last: int = 0) -> dict:
    """Delete timeline data (diary.tl_line) from DB. Use when user asks to delete/clear/remove timeline lines.
    Optional: before/after (ISO datetime or YYYY-MM-DD) for time range; last (int) to delete N most recent. Omit all to delete everything."""
    return _do_delete(["tl_line"], before, after, last)

@mcp.tool()
def db_clear(targets: list[str], before: str = "", after: str = "", last: int = 0) -> dict:
    """Delete multiple data types from marrow DB at once. Use when user asks to clear all data or multiple tables.
    Targets: 'events', 'digests', 'affect', 'tl_line'. Pass all four to wipe everything.
    Optional: before/after (ISO datetime or YYYY-MM-DD) for time range; last (int) to delete N most recent. Omit all to delete everything."""
    return _do_delete(targets, before, after, last)


def main() -> None:
    storage.init_db(_DB).close()
    mcp.run()


if __name__ == "__main__":
    main()
