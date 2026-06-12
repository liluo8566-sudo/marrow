"""Tests for marrow/aging.py — weekly maintenance. No LLM under test.
Each pass: happy path + edge (pinned bypass / no-op empty / boundary)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from marrow import aging, storage


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    yield conn
    conn.close()


def _ins_memes(conn, key, *, vtype="meme", use_count=0, last_seen=None,
               pinned=0, status="active"):
    conn.execute(
        "INSERT INTO memes (type, key, use_count, last_seen, pinned, status)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (vtype, key, use_count, last_seen, pinned, status),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _ins_event(conn, content, *, ts="now", sid="s1"):
    if ts == "now":
        conn.execute(
            "INSERT INTO events (session_id, timestamp, role, content) "
            "VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'user', ?)",
            (sid, content),
        )
    else:
        conn.execute(
            "INSERT INTO events (session_id, timestamp, role, content) "
            "VALUES (?, ?, 'user', ?)",
            (sid, ts, content),
        )


def _ins_task(conn, title, *, status="active"):
    conn.execute(
        "INSERT INTO tasks (category, title, status) VALUES ('study', ?, ?)",
        (title, status),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _ins_alert(conn, atype, message, *, resolved=0, age_days=0):
    if age_days == 0:
        conn.execute(
            "INSERT INTO alerts (severity, type, message, resolved) "
            "VALUES ('info', ?, ?, ?)",
            (atype, message, resolved),
        )
    else:
        conn.execute(
            "INSERT INTO alerts (severity, type, message, resolved,"
            " created_at) "
            "VALUES ('info', ?, ?, ?, datetime('now', ? || ' days'))",
            (atype, message, resolved, f"-{age_days}"),
        )


# ── schema v3 memes columns ──────────────────────────────────────────────────

def test_v3_memes_columns_present(db):
    cols = {r["name"] for r in db.execute("PRAGMA table_info(memes)")}
    assert "pinned" in cols
    assert "status" in cols
    assert db.execute("PRAGMA user_version").fetchone()[0] >= 3


# ── retire_memes ──────────────────────────────────────────────────────────────

def test_retire_memes_old_last_seen_deletes(db):
    vid = _ins_memes(
        db, "stale", pinned=0, last_seen="2020-01-01T00:00:00Z",
    )
    db.commit()
    n = aging.retire_memes(db)
    assert n == 1
    assert db.execute(
        "SELECT 1 FROM memes WHERE id=?", (vid,)
    ).fetchone() is None


def test_retire_memes_skips_pinned(db):
    vid = _ins_memes(
        db, "paw-anchor", pinned=1, last_seen="2020-01-01T00:00:00Z",
    )
    db.commit()
    assert aging.retire_memes(db) == 0
    assert db.execute(
        "SELECT 1 FROM memes WHERE id=?", (vid,)
    ).fetchone() is not None


def test_retire_memes_skips_recent(db):
    _ins_memes(
        db, "fresh", pinned=0, last_seen="2026-05-20T00:00:00Z",
    )
    db.commit()
    assert aging.retire_memes(db) == 0


def test_retire_memes_skips_null_last_seen(db):
    _ins_memes(db, "unseen", pinned=0, last_seen=None)
    db.commit()
    assert aging.retire_memes(db) == 0


# ── archive_tasks ─────────────────────────────────────────────────────────────

def test_archive_tasks_no_mention_archives(db):
    tid = _ins_task(db, "forgotten thing", status="active")
    db.commit()
    n = aging.archive_tasks(db)
    assert n == 1
    row = db.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["status"] == "archived"


def test_archive_tasks_recent_mention_keeps_active(db):
    tid = _ins_task(db, "active project", status="active")
    _ins_event(db, "working on active project today", sid="s1")
    db.commit()
    n = aging.archive_tasks(db)
    assert n == 0
    row = db.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["status"] == "active"


def test_archive_tasks_old_mention_archives(db):
    tid = _ins_task(db, "stale project", status="active")
    _ins_event(db, "stale project ref",
               ts="2026-01-01T00:00:00Z", sid="s1")
    db.commit()
    n = aging.archive_tasks(db)
    assert n == 1
    row = db.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["status"] == "archived"


def test_archive_tasks_skips_already_archived(db):
    _ins_task(db, "done thing", status="archived")
    db.commit()
    assert aging.archive_tasks(db) == 0


def test_archive_tasks_empty_noop(db):
    assert aging.archive_tasks(db) == 0


# ── confirm_milestone_alerts ──────────────────────────────────────────────────

def test_confirm_milestone_alerts_old_unresolved_confirmed(db):
    _ins_alert(db, "milestone_added", "added X", resolved=0, age_days=10)
    db.commit()
    n = aging.confirm_milestone_alerts(db)
    assert n == 1
    row = db.execute(
        "SELECT resolved, resolved_at FROM alerts WHERE message='added X'"
    ).fetchone()
    assert row["resolved"] == 1
    assert row["resolved_at"] is not None


def test_confirm_milestone_alerts_recent_skipped(db):
    _ins_alert(db, "milestone_added", "added Y", resolved=0, age_days=3)
    db.commit()
    assert aging.confirm_milestone_alerts(db) == 0


def test_confirm_milestone_alerts_skips_other_types(db):
    _ins_alert(db, "routine", "daily failed", resolved=0, age_days=10)
    db.commit()
    assert aging.confirm_milestone_alerts(db) == 0


def test_confirm_milestone_alerts_skips_already_resolved(db):
    _ins_alert(db, "milestone_added", "added Z", resolved=1, age_days=10)
    db.commit()
    assert aging.confirm_milestone_alerts(db) == 0


# ── main entrypoint ───────────────────────────────────────────────────────────

def _route_init_db(monkeypatch, p):
    """Route aging.storage.init_db() to a fixed path without recursion."""
    real = storage.init_db
    monkeypatch.setattr(
        aging.storage, "init_db",
        lambda path=None: real(p),
    )


def test_main_runs_clean_on_empty_db(db, monkeypatch, capsys):
    p = db.execute("PRAGMA database_list").fetchone()["file"]
    db.close()
    _route_init_db(monkeypatch, p)
    aging.main([])
    cap = capsys.readouterr()
    assert "retired=0" in cap.err
    assert "archived=0" in cap.err


def test_main_writes_audit_log(db, monkeypatch):
    p = db.execute("PRAGMA database_list").fetchone()["file"]
    db.close()
    _route_init_db(monkeypatch, p)
    aging.main([])
    fresh = sqlite3.connect(p)
    fresh.row_factory = sqlite3.Row
    try:
        row = fresh.execute(
            "SELECT target_table, action, summary FROM audit_log "
            "WHERE target_table='aging' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["action"] == "weekly"
        assert "retired=" in row["summary"]
    finally:
        fresh.close()


# ── prune_md_index_tombstones ────────────────────────────────────────────────

def test_prune_md_index_tombstones_deletes_old_only(db):
    """Old-tombstoned rows (>30d) → deleted; recent / live rows preserved."""
    # Live row — must survive.
    db.execute(
        "INSERT INTO md_index (path, block_id, content_hash, last_seen_at, "
        "tombstone_at) VALUES ('/p/a.md', 'live', 'h-a', "
        "datetime('now'), NULL)"
    )
    # Recently tombstoned (5 days ago) — must survive.
    db.execute(
        "INSERT INTO md_index (path, block_id, content_hash, last_seen_at, "
        "tombstone_at) VALUES ('/p/b.md', 'recent', 'h-b', "
        "datetime('now', '-5 days'), datetime('now', '-5 days'))"
    )
    # Just outside the cliff (29 days ago) — must survive.
    db.execute(
        "INSERT INTO md_index (path, block_id, content_hash, last_seen_at, "
        "tombstone_at) VALUES ('/p/c.md', 'boundary', 'h-c', "
        "datetime('now', '-29 days'), datetime('now', '-29 days'))"
    )
    # Old tombstoned (40 days ago) — must be pruned.
    db.execute(
        "INSERT INTO md_index (path, block_id, content_hash, last_seen_at, "
        "tombstone_at) VALUES ('/p/d.md', 'old1', 'h-d', "
        "datetime('now', '-40 days'), datetime('now', '-40 days'))"
    )
    # Very old tombstoned (120 days ago) — must be pruned.
    db.execute(
        "INSERT INTO md_index (path, block_id, content_hash, last_seen_at, "
        "tombstone_at) VALUES ('/p/e.md', 'old2', 'h-e', "
        "datetime('now', '-120 days'), datetime('now', '-120 days'))"
    )
    db.commit()

    n = aging.prune_md_index_tombstones(db)
    assert n == 2

    surviving = {r["block_id"] for r in db.execute(
        "SELECT block_id FROM md_index ORDER BY block_id"
    ).fetchall()}
    assert surviving == {"boundary", "live", "recent"}


def test_prune_md_index_tombstones_empty_noop(db):
    """Empty md_index → 0 deletes, no error."""
    assert aging.prune_md_index_tombstones(db) == 0


def test_prune_md_index_tombstones_no_old_tombstones_noop(db):
    """Only live + recent rows → 0 deletes."""
    db.execute(
        "INSERT INTO md_index (path, block_id, content_hash, last_seen_at) "
        "VALUES ('/p/a.md', 'live1', 'h', datetime('now'))"
    )
    db.execute(
        "INSERT INTO md_index (path, block_id, content_hash, last_seen_at, "
        "tombstone_at) VALUES ('/p/b.md', 'fresh-tomb', 'h2', "
        "datetime('now'), datetime('now'))"
    )
    db.commit()
    assert aging.prune_md_index_tombstones(db) == 0
    n_rows = db.execute(
        "SELECT COUNT(*) c FROM md_index"
    ).fetchone()["c"]
    assert n_rows == 2


def test_main_audit_includes_tombs_count(db, monkeypatch):
    """main() must include `tombs=N` in audit_log summary."""
    db.execute(
        "INSERT INTO md_index (path, block_id, content_hash, last_seen_at, "
        "tombstone_at) VALUES ('/p/old.md', 'stale', 'h', "
        "datetime('now', '-50 days'), datetime('now', '-50 days'))"
    )
    db.commit()
    p = db.execute("PRAGMA database_list").fetchone()["file"]
    db.close()
    _route_init_db(monkeypatch, p)
    aging.main([])
    fresh = sqlite3.connect(p)
    fresh.row_factory = sqlite3.Row
    try:
        row = fresh.execute(
            "SELECT summary FROM audit_log "
            "WHERE target_table='aging' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert "tombs=1" in row["summary"]
        # Verify the old row actually went.
        n = fresh.execute(
            "SELECT COUNT(*) c FROM md_index WHERE block_id='stale'"
        ).fetchone()["c"]
        assert n == 0
    finally:
        fresh.close()


# ── prune_projects_worktrees ──────────────────────────────────────────────────

def _make_slugs(root: Path, names: list[str]) -> None:
    """Create fake cc projects slug dirs (some empty, some with a jsonl)."""
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        d = root / n
        d.mkdir()
        # half get a fake jsonl to prove purge is content-blind
        if "-with-jsonl" in n:
            (d / "session.jsonl").write_text("{}\n")


def test_prune_worktrees_removes_only_worktree_slugs(tmp_path):
    root = tmp_path / "projects"
    _make_slugs(root, [
        "-Users-x-CC-Lab-marrow",                                # real, keep
        "-Users-x-Desktop-NY",                                   # real, keep
        "-Users-x-cc-lab-marrow--claude-worktrees-agent-aaa",    # purge
        "-Users-x-cc-lab-marrow--claude-worktrees-phase1-review-with-jsonl",  # purge content-blind
    ])
    n = aging.prune_projects_worktrees(root)
    assert n == 2
    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == [
        "-Users-x-CC-Lab-marrow",
        "-Users-x-Desktop-NY",
    ]


# ── A-4: aging pending_alerts flushed in finally ──────────────────────────────

def test_aging_alerts_flushed_when_audit_insert_raises(tmp_path, monkeypatch):
    """evict_vec_window returns pending alerts; audit INSERT raises →
    alerts still land in the alerts table (finally block fires)."""
    p = str(tmp_path / "aging_a4.db")
    conn = storage.init_db(p)
    conn.close()

    fake_pending = [
        {"severity": "warn", "atype": "vec_evict", "fingerprint": "vec_evict_fp",
         "source": "aging.py", "message": "test eviction alert"},
    ]
    fake_vec = {
        "evicted": 1, "exempted": 0, "skipped": 0, "aborted": 0,
        "pending_alerts": fake_pending,
    }

    def fake_evict_vec(*a, **kw):
        return fake_vec

    # Make conn.execute raise on the audit INSERT (action='weekly') but allow
    # all other queries through so the rest of main() runs normally.
    real_init = storage.init_db

    class _PatchedConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if "VALUES ('aging'" in sql and "'weekly'" in sql:
                raise RuntimeError("forced audit insert failure")
            return self._inner.execute(sql, params)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *a):
            return self._inner.__exit__(*a)

        def close(self):
            return self._inner.close()

        # Delegate attribute lookups (row_factory etc.) to inner conn.
        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(
        aging.storage, "init_db",
        lambda path=None: _PatchedConn(real_init(p)),
    )
    monkeypatch.setattr(aging, "evict_vec_window", fake_evict_vec)
    monkeypatch.setattr(aging, "prune_projects_worktrees",
                        lambda root=None: 0)

    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="forced audit insert failure"):
        aging.main(["--apply"])

    fresh = sqlite3.connect(p)
    fresh.row_factory = sqlite3.Row
    try:
        row = fresh.execute(
            "SELECT fingerprint FROM alerts WHERE fingerprint='vec_evict_fp'"
        ).fetchone()
    finally:
        fresh.close()
    assert row is not None, "pending alerts must land even when audit INSERT raises"


def test_prune_worktrees_missing_dir_noop(tmp_path):
    assert aging.prune_projects_worktrees(tmp_path / "nope") == 0


def test_prune_worktrees_no_matches_noop(tmp_path):
    root = tmp_path / "projects"
    _make_slugs(root, ["-Users-x-CC-Lab-marrow", "-Users-x-Desktop-NY"])
    assert aging.prune_projects_worktrees(root) == 0
    assert len(list(root.iterdir())) == 2


def test_prune_worktrees_ignores_regular_files(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "stray-worktrees-file").write_text("x")  # not a dir → ignored
    assert aging.prune_projects_worktrees(root) == 0
    assert (root / "stray-worktrees-file").exists()


def test_main_audit_includes_wtshells_count(db, monkeypatch, tmp_path):
    """main() must include `wtshells=N` in audit_log summary."""
    projects = tmp_path / "projects"
    _make_slugs(projects, [
        "-Users-x-CC-Lab-marrow",
        "-Users-x-cc-lab-marrow--claude-worktrees-agent-aaa",
    ])
    p = db.execute("PRAGMA database_list").fetchone()["file"]
    db.close()
    _route_init_db(monkeypatch, p)
    # Default projects_dir routes through home; monkeypatch the call instead
    # of mocking Path.home so other parts of aging stay untouched.
    real = aging.prune_projects_worktrees
    monkeypatch.setattr(
        aging, "prune_projects_worktrees",
        lambda projects_dir=None: real(projects_dir or projects),
    )
    aging.main([])
    fresh = sqlite3.connect(p)
    fresh.row_factory = sqlite3.Row
    try:
        row = fresh.execute(
            "SELECT summary FROM audit_log "
            "WHERE target_table='aging' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert "wtshells=1" in row["summary"]
    finally:
        fresh.close()
    # Real purge happened on the test dir.
    assert not (projects / "-Users-x-cc-lab-marrow--claude-worktrees-agent-aaa").exists()
