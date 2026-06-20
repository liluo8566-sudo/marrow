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


def main() -> None:
    storage.init_db(_DB).close()
    mcp.run()


if __name__ == "__main__":
    main()
