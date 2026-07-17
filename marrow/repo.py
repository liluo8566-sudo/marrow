"""Query + write layer over the SQLite store. Daemon tools and hooks call
here; schema/connection stay in storage.py. Deterministic, no LLM.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from . import storage
from . import recall as _recall_mod
from . import entity_recall as _entity_recall
from . import candidates as _candidates

_IN_SESSION_SUBDIR = "in-session"
_PRUNE_PATTERN = "marrow-before-*.db"
_PRUNE_MAX_AGE = timedelta(days=7)


def safe_backup_db(reason: str, db_path: Path | None = None) -> Path:
    """Snapshot the live db to backup/in-session/ before a destructive op.

    Filename: marrow-before-<reason>-<utc_iso>.db  (utc_iso = %Y%m%dT%H%M%SZ).
    Side effect: prune in-session/ files older than 7 days.
    Returns the dest Path.
    """
    src = Path(db_path) if db_path is not None else Path(config.db_path())
    dest_dir = Path(config.DATA_DIR) / "backup" / _IN_SESSION_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"marrow-before-{reason}-{stamp}.db"
    shutil.copy2(src, dest)

    # Prune best-effort — never touch daily backups (marrow-YYYY-MM-DD.db)
    try:
        cutoff = now - _PRUNE_MAX_AGE
        for f in dest_dir.glob(_PRUNE_PATTERN):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) < cutoff:
                    f.unlink()
            except Exception as exc:
                print(f"safe_backup_db: prune {f.name}: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"safe_backup_db: prune scan failed: {exc}", file=sys.stderr)

    return dest


def _fts_query(q: str) -> str:
    # Phase 1: phrase match, FTS5-safe. Multi-term ranking is Pending.
    return '"' + q.replace('"', '""').strip() + '"'


def recall(conn: sqlite3.Connection, query: str, limit: int = 10,
           budget_chars: int = 4000) -> list[dict]:
    """Recall past events. Uses fusion (vec+bm25+recency+affect) when
    bge-m3 is loaded; falls back to FTS5-only when embedder is absent."""
    return _recall_mod.recall_fusion(
        conn, query, limit=limit, budget_chars=budget_chars
    )


def open_alerts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, severity, type, message, source "
        "FROM alerts WHERE resolved = 0 "
        "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 "
        "ELSE 2 END, created_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_session(sid: str, model: str | None, channel: str | None,
                   title: str = "", effort: str = "",
                   *, last_active: str | None = None,
                   cwd: str | None = None,
                   db: str | None = None) -> None:
    """B1: record/refresh the sessions row for `sid`. Bridge calls this on
    every swap_provider so /resume can read the model back later.

    Idempotent — INSERT OR REPLACE keyed on PK sid. last_active bumps to now
    on every call unless `last_active` is provided explicitly (used by the
    one-shot backfill to preserve historical jsonl mtimes).

    model/channel/cwd update semantics: never blank-overwrite a previously-set
    value. Keeps a cli backfill (channel='cli', model=None) from clobbering
    a later bridge write (channel='wx', model='claude-...'). cwd is recorded
    by session_start hook from cc's hook input; sticky so a mid-session
    /clear that drops into a different dir does not reclassify the recall
    bucket retroactively.
    """
    if not sid:
        return
    conn = storage.connect(db)
    try:
        with conn:
            # Sticky channel — once a sid is on a channel it stays (a sid only
            # lives in one cc subprocess). Backfill (channel='cli') never
            # clobbers a bridge-written channel='wx'.
            # Model: latest non-empty wins (so /model swap reflects). Empty new
            # (cli backfill / session_start hook) keeps the prior model.
            if last_active:
                conn.execute(
                    "INSERT INTO sessions "
                    "(sid, model, channel, cwd, created_at, last_active, title, effort) "
                    "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?, ?) "
                    "ON CONFLICT(sid) DO UPDATE SET "
                    "  model=COALESCE(NULLIF(excluded.model, ''), sessions.model),"
                    "  channel=CASE WHEN sessions.channel IS NULL OR sessions.channel='' "
                    "               THEN excluded.channel ELSE sessions.channel END,"
                    "  cwd=CASE WHEN sessions.cwd IS NULL OR sessions.cwd='' "
                    "           THEN excluded.cwd ELSE sessions.cwd END,"
                    "  last_active=CASE WHEN excluded.last_active > sessions.last_active "
                    "                  THEN excluded.last_active ELSE sessions.last_active END,"
                    "  title=CASE WHEN excluded.title='' THEN sessions.title "
                    "             ELSE excluded.title END,"
                    "  effort=CASE WHEN excluded.effort='' THEN sessions.effort "
                    "              ELSE excluded.effort END",
                    (sid, model, channel, cwd, last_active, title or "", effort or ""),
                )
            else:
                conn.execute(
                    "INSERT INTO sessions "
                    "(sid, model, channel, cwd, created_at, last_active, title, effort) "
                    "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?) "
                    "ON CONFLICT(sid) DO UPDATE SET "
                    "  model=COALESCE(NULLIF(excluded.model, ''), sessions.model),"
                    "  channel=CASE WHEN sessions.channel IS NULL OR sessions.channel='' "
                    "               THEN excluded.channel ELSE sessions.channel END,"
                    "  cwd=CASE WHEN sessions.cwd IS NULL OR sessions.cwd='' "
                    "           THEN excluded.cwd ELSE sessions.cwd END,"
                    "  last_active=excluded.last_active,"
                    "  title=CASE WHEN excluded.title='' THEN sessions.title "
                    "             ELSE excluded.title END,"
                    "  effort=CASE WHEN excluded.effort='' THEN sessions.effort "
                    "              ELSE excluded.effort END",
                    (sid, model, channel, cwd, title or "", effort or ""),
                )
    finally:
        conn.close()


def get_session(sid: str, *, db: str | None = None) -> dict | None:
    """B1: read back the model/channel/title for `sid`. None when absent."""
    if not sid:
        return None
    conn = storage.connect(db)
    try:
        row = conn.execute(
            "SELECT sid, model, channel, cwd, created_at, last_active, title, effort "
            "FROM sessions WHERE sid=?",
            (sid,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def touch_session_active(sid: str, *, db: str | None = None) -> None:
    if not sid:
        return
    conn = storage.connect(db)
    try:
        conn.execute(
            "UPDATE sessions SET last_active = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE sid = ?",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()


def list_recent_sessions(
    limit: int = 5,
    *,
    exclude_channels: Sequence[str] | None = None,
    channels: Sequence[str] | None = None,
    require_user_events: bool = False,
    db: str | None = None,
) -> list[dict]:
    """B6: N most-recent sessions for the /resume picker.

    `channels` / `exclude_channels` are mutually exclusive; pass at most one.

    `require_user_events`: when True, drop sessions with no real user prompt
    in the events table (only slash-command / control-prefix lifetimes).
    Used by the wx /resume picker to hide empty sessions.
    """
    if channels and exclude_channels:
        raise ValueError("channels and exclude_channels are mutually exclusive")
    sql = "SELECT sid, model, channel, cwd, last_active, title, effort FROM sessions"
    where: list[str] = []
    params: list = []
    if channels:
        chans = [c for c in channels if c]
        if chans:
            where.append("channel IN (" + ",".join("?" * len(chans)) + ")")
            params.extend(chans)
    elif exclude_channels:
        chans = [c for c in exclude_channels if c]
        if chans:
            where.append("channel NOT IN (" + ",".join("?" * len(chans)) + ")")
            params.extend(chans)
    if require_user_events:
        # length>1 nukes single-char/empty noise; slash commands never reach
        # events (handled cli- or bridge-side before cc), so no allowlist needed.
        where.append(
            "EXISTS (SELECT 1 FROM events e"
            " WHERE e.session_id = sessions.sid"
            " AND e.role = 'user' AND length(e.content) > 1)"
        )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_active DESC LIMIT ?"
    params.append(limit)
    conn = storage.connect(db)
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_alert(severity: str, atype: str, fingerprint: str,
              source: str | None = None, *,
              message: str | None = None,
              db: str | None = None) -> int:
    # on_alert sink for LLMClient: self-contained connection so it works
    # from any context (pipeline, hook, daemon). Mirrors to audit_log.
    #
    # Dedup key: (type, fingerprint, resolved=0). Callers pass a STABLE
    # fingerprint that excludes high-cardinality fields (sid, hash, exception
    # text, counters). Repeats bump hit_count + updated_at on the existing
    # row instead of inserting a new one.
    #
    # Back-compat: third positional was previously `message` (free-form
    # human text). Existing callers still work — the free text becomes both
    # fingerprint and message; dedup quality is the caller's responsibility
    # until they migrate to a stable fingerprint and pass `message=` for the
    # human detail.
    #
    # Never raises: on any DB exception the record is appended to
    # alerts-fallback.jsonl (drained by sessionstart_catchup on next boot).
    try:
        detail = message if message is not None else fingerprint
        conn = storage.connect(db)
        try:
            existing = conn.execute(
                "SELECT id FROM alerts"
                " WHERE type=? AND fingerprint=? AND resolved=0"
                " LIMIT 1",
                (atype, fingerprint),
            ).fetchone()
            if existing is not None:
                aid = existing["id"]
                with conn:
                    conn.execute(
                        "UPDATE alerts SET hit_count=hit_count+1,"
                        " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
                        " message=?, severity=?, source=?"
                        " WHERE id=?",
                        (detail, severity, source, aid),
                    )
                return aid
            with conn:
                cur = conn.execute(
                    "INSERT INTO alerts"
                    " (severity, type, fingerprint, message, source)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (severity, atype, fingerprint, detail, source),
                )
                aid = cur.lastrowid
                conn.execute(
                    "INSERT INTO audit_log "
                    "(target_table, target_id, action, summary) "
                    "VALUES ('alerts', ?, 'insert', ?)",
                    (str(aid), f"{severity}/{atype}: {detail[:120]}"),
                )
            return aid
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        try:
            sink = config.DATA_DIR / "alerts-fallback.jsonl"
            sink.parent.mkdir(parents=True, exist_ok=True)
            rec = json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "severity": severity,
                "type": atype,
                "fingerprint": fingerprint,
                "source": source,
                "message": message if message is not None else fingerprint,
            })
            with sink.open("a", encoding="utf-8") as fh:
                fh.write(rec + "\n")
        except Exception:  # noqa: BLE001
            pass
        sys.stderr.write(
            f"[repo.add_alert] DB write failed ({exc!r}); queued to fallback sink\n"
        )
        return -1


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _sid_is_blocked(conn: sqlite3.Connection, sid: str) -> bool:
    """Belt-and-braces check: latest session_block row for sid wins.
    archive -> True (drop rows), cleared/absent -> False (allow insert).
    Mirrors hooks._is_session_blocked without the circular import."""
    row = conn.execute(
        "SELECT summary FROM audit_log"
        " WHERE action='session_block' AND target_id=?"
        " ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return bool(row and row[0] == "archive")


def archive_events(conn: sqlite3.Connection, rows: list[dict]) -> int:
    # Write path for #7 SessionEnd. Idempotent by source_hash; re-run skips.
    # Defensive gate: drop rows whose sid has session_block=archive regardless
    # of when the block was written. Catches sid-drift and any future write path
    # that bypasses the hooks.py gate (belt-and-braces).
    n = 0
    sessions: set[str] = set()
    inserted: list[dict] = []
    _blocked_cache: dict[str, bool] = {}
    with conn:
        for r in rows:
            sid = r["session_id"]
            if sid not in _blocked_cache:
                _blocked_cache[sid] = _sid_is_blocked(conn, sid)
            if _blocked_cache[sid]:
                continue
            h = _hash(sid, r["timestamp"], r["role"],
                      r["content"])
            if conn.execute(
                "SELECT 1 FROM events WHERE source_hash=? "
                "UNION ALL SELECT 1 FROM event_tombstones WHERE source_hash=? "
                "LIMIT 1", (h, h)
            ).fetchone():
                continue
            conn.execute(
                "INSERT INTO events "
                "(session_id, timestamp, role, content, channel, source_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, r["timestamp"], r["role"], r["content"],
                 r.get("channel"), h),
            )
            sessions.add(sid)
            inserted.append(r)
            n += 1
        # Bump entities.mention_count for entities whose name/alias appears
        # in newly-inserted events. Same transaction = atomic with inserts;
        # dedup-aware (only `inserted`, not `rows`) so re-runs don't double-count.
        if inserted:
            _entity_recall.bump_mention_counts(conn, inserted)
            # Same pattern for memes.use_count — meme key substring match,
            # one bump per event per meme. recall_fusion._memes_candidates
            # reads use_count as the heat score for the meme lane.
            _candidates.bump_use_counts(conn, inserted)
        # One batch audit row per call (Monitor Zone), atomic with the inserts.
        # Skip when n == 0 so a fully-deduped re-run shows no phantom archive.
        if n:
            target = next(iter(sessions)) if len(sessions) == 1 else str(len(sessions))
            conn.execute(
                "INSERT INTO audit_log "
                "(target_table, target_id, action, summary) "
                "VALUES ('events', ?, 'insert', ?)",
                (target, f"archived {n} events ({len(sessions)} sessions)"),
            )
    return n
