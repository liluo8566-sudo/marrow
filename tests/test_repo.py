"""Tests for marrow/repo.py public API + daemon smoke test."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import marrow.daemon as daemon
import pytest

from marrow import repo, storage


def _recent_ts(minutes: int = 0) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=1) + timedelta(minutes=minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


ROWS = [
    {"session_id": "s1", "timestamp": _recent_ts(), "role": "user",
     "content": "hello marrow world"},
    {"session_id": "s1", "timestamp": _recent_ts(1), "role": "assistant",
     "content": "hi there, welcome back"},
]


@pytest.fixture()
def db(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    yield conn
    conn.close()


# ── recall ────────────────────────────────────────────────────────────────────

def test_recall_hit(db):
    repo.archive_events(db, ROWS)
    results = repo.recall(db, "marrow")
    assert len(results) == 1
    assert results[0]["content"] == "hello marrow world"


def test_recall_empty_query(db):
    repo.archive_events(db, ROWS)
    assert repo.recall(db, "") == []
    assert repo.recall(db, "   ") == []


def test_recall_budget_chars_truncates(db):
    rows = [
        {"session_id": "s1", "timestamp": "2026-05-17T02:00:00Z", "role": "user",
         "content": "x" * 3000},
        {"session_id": "s1", "timestamp": "2026-05-17T02:01:00Z", "role": "user",
         "content": "x" * 3000},
    ]
    repo.archive_events(db, rows)
    # budget_chars=4000; first row fills 3000, second gets truncated to 1000
    results = repo.recall(db, "x", limit=10, budget_chars=4000)
    total = sum(len(r["content"]) for r in results)
    assert total <= 4000


# ── open_tasks ────────────────────────────────────────────────────────────────

def test_open_tasks_active_only(db):
    db.execute("INSERT INTO tasks(category,title,status) VALUES('work','A','active')")
    db.execute("INSERT INTO tasks(category,title,status) VALUES('work','B','closed')")
    db.commit()
    titles = [t["title"] for t in repo.open_tasks(db)]
    assert "A" in titles
    assert "B" not in titles


def test_open_tasks_due_before_null(db):
    db.execute("INSERT INTO tasks(category,title,status,due) VALUES('work','NullDue','active',NULL)")
    db.execute("INSERT INTO tasks(category,title,status,due) VALUES('work','HasDue','active','2026-06-01')")
    db.commit()
    titles = [t["title"] for t in repo.open_tasks(db)]
    assert titles.index("HasDue") < titles.index("NullDue")


# ── open_alerts ───────────────────────────────────────────────────────────────

def test_open_alerts_unresolved_only(db):
    db.execute("INSERT INTO alerts(severity,type,message,resolved) VALUES('warn','x','open',0)")
    db.execute("INSERT INTO alerts(severity,type,message,resolved) VALUES('warn','x','done',1)")
    db.commit()
    msgs = [a["message"] for a in repo.open_alerts(db)]
    assert "open" in msgs
    assert "done" not in msgs


def test_open_alerts_severity_order(db):
    db.execute("INSERT INTO alerts(severity,type,message) VALUES('warn','x','w')")
    db.execute("INSERT INTO alerts(severity,type,message) VALUES('info','x','o')")
    db.execute("INSERT INTO alerts(severity,type,message) VALUES('critical','x','c')")
    db.commit()
    severities = [a["severity"] for a in repo.open_alerts(db)]
    assert severities.index("critical") < severities.index("warn")
    assert severities.index("warn") < severities.index("info")


# ── add_alert ─────────────────────────────────────────────────────────────────

def test_add_alert_returns_id(tmp_path):
    p = str(tmp_path / "a.db")
    storage.init_db(p).close()
    aid = repo.add_alert("warn", "test", "something happened", db=p)
    assert isinstance(aid, int) and aid > 0


def test_add_alert_writes_alerts_and_audit(tmp_path):
    p = str(tmp_path / "a.db")
    storage.init_db(p).close()
    aid = repo.add_alert("critical", "llm", "provider down", source="llm.py", db=p)
    conn = storage.connect(p)
    try:
        alert_row = conn.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
        assert alert_row is not None
        assert alert_row["severity"] == "critical"
        audit_row = conn.execute(
            "SELECT * FROM audit_log WHERE target_table='alerts' AND target_id=?",
            (str(aid),),
        ).fetchone()
        assert audit_row is not None
        assert audit_row["action"] == "insert"
    finally:
        conn.close()


def test_add_alert_dedup_by_fingerprint_bumps_hit_count(tmp_path):
    # Regression for the 2026-06-05 760 silent_death flood: pre-v15 dedup keyed
    # on the full message string, so any high-cardinality field embedded in the
    # message (sid, hash, exception text) bypassed dedup. Now the third
    # positional is a stable fingerprint; same fingerprint -> hit_count bump,
    # not a new row.
    p = str(tmp_path / "dedup.db")
    storage.init_db(p).close()
    a1 = repo.add_alert("warn", "silent_death", "silent_death", "src", message="m1", db=p)
    a2 = repo.add_alert("warn", "silent_death", "silent_death", "src", message="m2", db=p)
    a3 = repo.add_alert("warn", "silent_death", "silent_death", "src", message="m3", db=p)
    assert a1 == a2 == a3
    conn = storage.connect(p)
    try:
        rows = conn.execute(
            "SELECT id, fingerprint, hit_count, message FROM alerts"
            " WHERE type='silent_death'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["hit_count"] == 3
        # Latest detail wins on the deduped row so the dashboard sees the
        # freshest context.
        assert rows[0]["message"] == "m3"
    finally:
        conn.close()


def test_add_alert_distinct_fingerprint_creates_new_row(tmp_path):
    p = str(tmp_path / "split.db")
    storage.init_db(p).close()
    a1 = repo.add_alert("warn", "drift", "fp_a", "src", db=p)
    a2 = repo.add_alert("warn", "drift", "fp_b", "src", db=p)
    assert a1 != a2
    conn = storage.connect(p)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE type='drift'"
        ).fetchone()[0]
        assert n == 2
    finally:
        conn.close()


def test_add_alert_resolved_row_does_not_block_new_alert(tmp_path):
    # Dedup is scoped to unresolved rows: a resolved alert of the same
    # fingerprint must not prevent a fresh insert.
    p = str(tmp_path / "resolved.db")
    storage.init_db(p).close()
    a1 = repo.add_alert("warn", "kind", "fp", "src", db=p)
    conn = storage.connect(p)
    try:
        with conn:
            conn.execute("UPDATE alerts SET resolved=1, resolved_at='2026-06-06T00:00:00Z' WHERE id=?", (a1,))
    finally:
        conn.close()
    a2 = repo.add_alert("warn", "kind", "fp", "src", db=p)
    assert a2 != a1
    conn = storage.connect(p)
    try:
        n = conn.execute("SELECT COUNT(*) FROM alerts WHERE type='kind'").fetchone()[0]
        assert n == 2
    finally:
        conn.close()


def test_add_alert_legacy_positional_still_works(tmp_path):
    # Back-compat for callsites that haven't migrated: third positional was
    # historically the free-form message. It now becomes the fingerprint, but
    # the row still lands and dedup still functions on identical text.
    p = str(tmp_path / "legacy.db")
    storage.init_db(p).close()
    a1 = repo.add_alert("warn", "legacy", "free text", "src", db=p)
    a2 = repo.add_alert("warn", "legacy", "free text", "src", db=p)
    assert a1 == a2
    conn = storage.connect(p)
    try:
        row = conn.execute(
            "SELECT fingerprint, message, hit_count FROM alerts WHERE id=?",
            (a1,),
        ).fetchone()
        assert row["fingerprint"] == "free text"
        assert row["message"] == "free text"
        assert row["hit_count"] == 2
    finally:
        conn.close()


# ── archive_events ────────────────────────────────────────────────────────────

def test_archive_events_inserts_n(db):
    n = repo.archive_events(db, ROWS)
    assert n == len(ROWS)


def test_archive_events_idempotent(db):
    repo.archive_events(db, ROWS)
    n2 = repo.archive_events(db, ROWS)
    assert n2 == 0


def test_archive_events_fts_indexed(db):
    repo.archive_events(db, ROWS)
    results = repo.recall(db, "welcome")
    assert len(results) == 1
    assert "welcome" in results[0]["content"]


def test_archive_events_writes_one_batch_audit_row(db):
    n = repo.archive_events(db, ROWS)
    rows = db.execute(
        "SELECT * FROM audit_log WHERE target_table='events'"
    ).fetchall()
    assert len(rows) == 1, "exactly one batch audit row per call, not one per event"
    a = rows[0]
    assert a["action"] == "insert"
    assert a["target_id"] == "s1"  # single distinct session_id
    assert str(n) in a["summary"]


def test_archive_events_multi_session_audit_target_id(db):
    rows = [
        {"session_id": "s1", "timestamp": "2026-05-17T03:00:00Z", "role": "user",
         "content": "alpha"},
        {"session_id": "s2", "timestamp": "2026-05-17T03:01:00Z", "role": "user",
         "content": "beta"},
    ]
    repo.archive_events(db, rows)
    a = db.execute(
        "SELECT * FROM audit_log WHERE target_table='events'"
    ).fetchone()
    assert a["target_id"] == "2"  # distinct-session count when multi-session


def test_archive_events_dedup_rerun_adds_no_audit_row(db):
    repo.archive_events(db, ROWS)
    before = db.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE target_table='events'"
    ).fetchone()["c"]
    n2 = repo.archive_events(db, ROWS)
    assert n2 == 0
    after = db.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE target_table='events'"
    ).fetchone()["c"]
    assert after == before, "fully-deduped re-run must not write a phantom audit row"


class _AuditFailConn(sqlite3.Connection):
    def execute(self, sql, *a, **k):  # noqa: D401
        if "INSERT INTO audit_log" in sql:
            raise sqlite3.OperationalError("forced audit failure")
        return super().execute(sql, *a, **k)


def test_archive_events_audit_atomic_rollback(tmp_path):
    # If the audit write fails, the whole archive (events + audit) rolls back:
    # the row loop and the batch audit row share one `with conn:` transaction.
    p = str(tmp_path / "rb.db")
    storage.init_db(p).close()
    conn = sqlite3.connect(p, factory=_AuditFailConn)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(sqlite3.OperationalError):
            repo.archive_events(conn, ROWS)
    finally:
        conn.close()
    chk = storage.connect(p)
    try:
        assert chk.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0
        assert chk.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE target_table='events'"
        ).fetchone()["c"] == 0
    finally:
        chk.close()


# ── archive_events: entity mention_count bump ─────────────────────────────────

def _seed_entity(conn, name, *, kind="person", aliases=None, fact=""):
    import json as _json
    al = _json.dumps(aliases) if aliases else None
    cur = conn.execute(
        "INSERT INTO entities (kind, name, fact, aliases) VALUES (?, ?, ?, ?)",
        (kind, name, fact, al),
    )
    conn.commit()
    return cur.lastrowid


def _mc(conn, eid):
    return conn.execute(
        "SELECT mention_count FROM entities_live WHERE id=?", (eid,)
    ).fetchone()["mention_count"]


def test_archive_events_bumps_mention_count_on_user_event(db):
    eid = _seed_entity(db, "(李小云)")
    rows = [{
        "session_id": "s1", "timestamp": "2026-05-17T04:00:00Z",
        "role": "user", "content": "我跟(李小云)吃饭",
    }]
    repo.archive_events(db, rows)
    assert _mc(db, eid) == 1


def test_archive_events_bumps_mention_count_on_assistant_event(db):
    eid = _seed_entity(db, "(铁锅)")
    rows = [{
        "session_id": "s1", "timestamp": "2026-05-17T04:01:00Z",
        "role": "assistant", "content": "(铁锅)在看你",
    }]
    repo.archive_events(db, rows)
    assert _mc(db, eid) == 1


def test_archive_events_alias_match_counts(db):
    eid = _seed_entity(db, "(屿忱)", aliases=["Stellan", "(阿屿)"])
    rows = [{
        "session_id": "s1", "timestamp": "2026-05-17T04:02:00Z",
        "role": "user", "content": "talked to Stellan today",
    }]
    repo.archive_events(db, rows)
    assert _mc(db, eid) == 1


def test_archive_events_same_message_double_mention_counts_once(db):
    eid = _seed_entity(db, "(李小云)")
    rows = [{
        "session_id": "s1", "timestamp": "2026-05-17T04:03:00Z",
        "role": "user", "content": "(李小云)说(李小云)累了",
    }]
    repo.archive_events(db, rows)
    assert _mc(db, eid) == 1


def test_archive_events_multiple_events_accumulate(db):
    eid = _seed_entity(db, "(李小云)")
    rows = [
        {"session_id": "s1", "timestamp": "2026-05-17T04:04:00Z",
         "role": "user", "content": "(李小云)来了"},
        {"session_id": "s1", "timestamp": "2026-05-17T04:05:00Z",
         "role": "assistant", "content": "(李小云)走了"},
    ]
    repo.archive_events(db, rows)
    assert _mc(db, eid) == 2


def test_archive_events_idempotent_rerun_no_double_bump(db):
    eid = _seed_entity(db, "(李小云)")
    rows = [{
        "session_id": "s1", "timestamp": "2026-05-17T04:06:00Z",
        "role": "user", "content": "(李小云)来了",
    }]
    repo.archive_events(db, rows)
    repo.archive_events(db, rows)  # full dedup, no new inserts
    assert _mc(db, eid) == 1


def test_archive_events_non_matching_event_no_bump(db):
    eid = _seed_entity(db, "(李小云)")
    rows = [{
        "session_id": "s1", "timestamp": "2026-05-17T04:07:00Z",
        "role": "user", "content": "just talking about the weather",
    }]
    repo.archive_events(db, rows)
    assert _mc(db, eid) == 0


def test_archive_events_skips_non_user_assistant_roles(db):
    eid = _seed_entity(db, "(李小云)")
    # system role: present in schema, ignored by bump logic
    rows = [{
        "session_id": "s1", "timestamp": "2026-05-17T04:08:00Z",
        "role": "system", "content": "(李小云)is mentioned here",
    }]
    repo.archive_events(db, rows)
    assert _mc(db, eid) == 0


def test_archive_events_case_insensitive_match(db):
    eid = _seed_entity(db, "Bendigo")
    rows = [{
        "session_id": "s1", "timestamp": "2026-05-17T04:09:00Z",
        "role": "user", "content": "driving to bendigo this weekend",
    }]
    repo.archive_events(db, rows)
    assert _mc(db, eid) == 1


# ── archived_today ────────────────────────────────────────────────────────────

def test_archived_today_returns_done_today(tmp_path, monkeypatch):
    """2 tasks done today + 1 done yesterday: only 2 returned."""
    from datetime import datetime, timedelta, timezone
    from marrow import top_sections
    p = str(tmp_path / "a.db")
    conn = storage.init_db(p)

    cutoff = top_sections._day_cutoff_utc()
    # Today = 1h after cutoff
    today_ts = (cutoff + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Also today = 2h after cutoff
    today_ts2 = (cutoff + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Yesterday = 1h before cutoff
    yesterday_ts = (cutoff - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn.execute(
        "INSERT INTO tasks(category,title,status,updated_at) VALUES('Project','Done-A','done',?)",
        (today_ts,),
    )
    conn.execute(
        "INSERT INTO tasks(category,title,status,updated_at) VALUES('Study','Done-B','done',?)",
        (today_ts2,),
    )
    conn.execute(
        "INSERT INTO tasks(category,title,status,updated_at) VALUES('Daily','Done-Yesterday','done',?)",
        (yesterday_ts,),
    )
    conn.execute(
        "INSERT INTO tasks(category,title,status) VALUES('Project','Active-Now','active')",
    )
    conn.commit()

    results = repo.archived_today(conn)
    conn.close()

    titles = [r["title"] for r in results]
    assert "Done-A" in titles
    assert "Done-B" in titles
    assert "Done-Yesterday" not in titles
    assert "Active-Now" not in titles
    assert len(results) == 2
    # Sorted by updated_at ASC
    assert titles.index("Done-A") < titles.index("Done-B")


def test_archived_today_empty_when_none(tmp_path):
    p = str(tmp_path / "b.db")
    conn = storage.init_db(p)
    results = repo.archived_today(conn)
    conn.close()
    assert results == []


# ── sessions ─────────────────────────────────────────────────────────────────

def test_upsert_session_effort_sticky(tmp_path):
    p = str(tmp_path / "sessions.db")
    storage.init_db(p).close()

    repo.upsert_session("s1", "opus", "wx", effort="high", db=p)
    repo.upsert_session("s1", "opus", "wx", effort="", db=p)
    assert repo.get_session("s1", db=p)["effort"] == "high"

    repo.upsert_session("s1", "opus", "wx", effort="low", db=p)
    assert repo.get_session("s1", db=p)["effort"] == "low"


def test_list_recent_sessions_includes_effort(tmp_path):
    p = str(tmp_path / "recent.db")
    storage.init_db(p).close()

    repo.upsert_session(
        "s1", "opus", "wx", effort="medium",
        last_active="2026-06-15T01:00:00Z", db=p,
    )
    rows = repo.list_recent_sessions(limit=1, db=p)

    assert rows[0]["sid"] == "s1"
    assert rows[0]["effort"] == "medium"


# ── daemon smoke ──────────────────────────────────────────────────────────────

def test_daemon_mcp_exists():
    assert hasattr(daemon, "mcp"), "daemon.mcp not found"


def test_daemon_recall_callable():
    assert callable(daemon.recall), "daemon.recall is not callable"


def test_daemon_recall_returns_list(tmp_path, monkeypatch):
    # _DB is read by name from daemon module globals at each call, so patching
    # it after import is safe — no closure baking at def time.
    p = str(tmp_path / "d.db")
    storage.init_db(p).close()
    monkeypatch.setattr(daemon, "_DB", p)
    result = daemon.recall("anything")
    assert isinstance(result, list)


# ── fingerprint dedup regression tests ───────────────────────────────────────
# Covers the callsite migrations from 2026-06-06: each callsite now passes a
# stable fingerprint so repeated failures produce 1 row + hit_count bump,
# not N rows.

def test_drift_sweep_dedup(tmp_path):
    """Repeated drift review for the same file pair dedups to 1 row."""
    p = str(tmp_path / "drift.db")
    storage.init_db(p).close()
    fp = "drift_review:foo.md:bar.md"
    a1 = repo.add_alert("warn", "drift_sweep", fp, "drift_sweep.py",
                        message="drift review: foo.md -> bar.md · 0 safe · 1 unsafe",
                        db=p)
    a2 = repo.add_alert("warn", "drift_sweep", fp, "drift_sweep.py",
                        message="drift review: foo.md -> bar.md · 0 safe · 2 unsafe",
                        db=p)
    assert a1 == a2, "same file pair must dedup to same alert row"
    conn = storage.connect(p)
    try:
        rows = conn.execute(
            "SELECT hit_count, message FROM alerts WHERE type='drift_sweep'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["hit_count"] == 2
    finally:
        conn.close()


def test_catchup_spawn_failed_dedup(tmp_path):
    """Repeated catchup spawn failures dedup to 1 row; sid list updates."""
    p = str(tmp_path / "catchup.db")
    storage.init_db(p).close()
    fp = "catchup_spawn_failed"
    a1 = repo.add_alert("warn", "catchup", fp, "sessionstart_catchup.py",
                        message="catchup spawn failed: abc12345:OSError",
                        db=p)
    a2 = repo.add_alert("warn", "catchup", fp, "sessionstart_catchup.py",
                        message="catchup spawn failed: def67890:OSError",
                        db=p)
    a3 = repo.add_alert("warn", "catchup", fp, "sessionstart_catchup.py",
                        message="catchup spawn failed: fff00000:OSError",
                        db=p)
    assert a1 == a2 == a3, "all catchup spawn failures must share one row"
    conn = storage.connect(p)
    try:
        rows = conn.execute(
            "SELECT hit_count, message FROM alerts WHERE type='catchup'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["hit_count"] == 3
        assert "fff00000" in rows[0]["message"]
    finally:
        conn.close()


def test_embedding_dim_mismatch_dedup(tmp_path):
    """Repeated dim-mismatch alerts per lane dedup correctly."""
    p = str(tmp_path / "embed.db")
    storage.init_db(p).close()
    # events_vec lane
    fp_events = "embedding_dim_mismatch:events_vec"
    a1 = repo.add_alert("warn", "embedding_dim_mismatch", fp_events,
                        "storage.py:init_db",
                        message="events_vec dim=384 != config 1024; 50 rows preserved",
                        db=p)
    a2 = repo.add_alert("warn", "embedding_dim_mismatch", fp_events,
                        "storage.py:init_db",
                        message="events_vec dim=384 != config 1024; 50 rows preserved",
                        db=p)
    assert a1 == a2
    # different lane gets its own row
    fp_memes = "embedding_dim_mismatch:memes_vec"
    a3 = repo.add_alert("warn", "embedding_dim_mismatch", fp_memes,
                        "storage.py:init_db",
                        message="memes_vec dim=384 != config 1024; 10 rows preserved",
                        db=p)
    assert a3 != a1, "different lane must create a separate alert row"
    conn = storage.connect(p)
    try:
        rows = conn.execute(
            "SELECT fingerprint, hit_count FROM alerts"
            " WHERE type='embedding_dim_mismatch' ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["fingerprint"] == fp_events
        assert rows[0]["hit_count"] == 2
        assert rows[1]["fingerprint"] == fp_memes
        assert rows[1]["hit_count"] == 1
    finally:
        conn.close()
