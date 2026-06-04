"""Tests for marrow/cli.py — the `mw` deterministic point-edit CLI.

CLI is the interface under test; DB state is the verification surface
(same convention as test_repo.py). main(argv) returns an exit code.
"""
from __future__ import annotations

import pytest

from marrow import cli, storage


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO tasks(category,title,status) VALUES('work','Old','active')"
    )
    conn.execute(
        "INSERT INTO alerts(severity,type,message,resolved) "
        "VALUES('warn','bug','boom',0)"
    )
    conn.commit()
    conn.close()
    return p


def _rows(p, sql, args=()):
    conn = storage.connect(p)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ── set: core point-edit ──────────────────────────────────────────────────────

def test_set_updates_one_field(db):
    rc = cli.main(["set", "tasks", "1", "title", "Renamed", "--db", db])
    assert rc == 0
    row = _rows(db, "SELECT title FROM tasks WHERE id=1")[0]
    assert row["title"] == "Renamed"


def test_set_mirrors_audit_log(db):
    cli.main(["set", "tasks", "1", "title", "Renamed", "--db", db])
    a = _rows(
        db,
        "SELECT * FROM audit_log WHERE target_table='tasks' AND target_id='1'",
    )
    assert len(a) == 1
    assert a[0]["action"] == "update"


def test_set_blocks_protected_field(db):
    rc = cli.main(["set", "tasks", "1", "id", "99", "--db", db])
    assert rc != 0
    # original row untouched
    assert _rows(db, "SELECT id FROM tasks")[0]["id"] == 1


def test_set_blocks_unknown_field(db):
    rc = cli.main(["set", "tasks", "1", "nope", "x", "--db", db])
    assert rc != 0


def test_set_rejects_unknown_table(db):
    rc = cli.main(["set", "robots", "1", "title", "x", "--db", db])
    assert rc != 0


def test_set_missing_id_fails(db):
    rc = cli.main(["set", "tasks", "999", "title", "x", "--db", db])
    assert rc != 0
    assert _rows(db, "SELECT COUNT(*) c FROM audit_log")[0]["c"] == 0


# ── rm ─────────────────────────────────────────────────────────────────────────

def test_rm_deletes_row_and_audits(db):
    rc = cli.main(["rm", "tasks", "1", "--db", db])
    assert rc == 0
    assert _rows(db, "SELECT COUNT(*) c FROM tasks")[0]["c"] == 0
    a = _rows(db, "SELECT action FROM audit_log WHERE target_table='tasks'")
    assert a[0]["action"] == "delete"


def test_rm_missing_id_fails(db):
    rc = cli.main(["rm", "tasks", "999", "--db", db])
    assert rc != 0


# ── resolve / done shortcuts ───────────────────────────────────────────────────

def test_resolve_marks_alert(db):
    rc = cli.main(["resolve", "1", "--db", db])
    assert rc == 0
    row = _rows(db, "SELECT resolved, resolved_at FROM alerts WHERE id=1")[0]
    assert row["resolved"] == 1
    assert row["resolved_at"] is not None


def test_done_marks_task(db):
    rc = cli.main(["done", "1", "--db", db])
    assert rc == 0
    assert _rows(db, "SELECT status FROM tasks WHERE id=1")[0]["status"] == "done"


# ── pin / unpin shortcuts ──────────────────────────────────────────────────────

def _insert_meme(p, mtype: str, pinned: int) -> None:
    conn = storage.connect(p)
    try:
        conn.execute(
            "INSERT INTO memes(type,key,value,pinned) VALUES(?,?,?,?)",
            (mtype, "k", "v", pinned),
        )
        conn.commit()
    finally:
        conn.close()


def test_pin_sets_memes_pinned(db):
    _insert_meme(db, "others", 0)
    rc = cli.main(["pin", "memes", "1", "--db", db])
    assert rc == 0
    assert _rows(db, "SELECT pinned FROM memes WHERE id=1")[0]["pinned"] == 1
    a = _rows(db, "SELECT action,summary FROM audit_log "
                  "WHERE target_table='memes' AND target_id='1'")
    assert a[0]["action"] == "update"
    assert "pinned" in a[0]["summary"]


def test_unpin_clears_memes_pinned(db):
    _insert_meme(db, "fact", 1)
    rc = cli.main(["unpin", "memes", "1", "--db", db])
    assert rc == 0
    assert _rows(db, "SELECT pinned FROM memes WHERE id=1")[0]["pinned"] == 0


def test_pin_rejects_table_without_pinned_column(db):
    # alerts has no pinned column
    rc = cli.main(["pin", "alerts", "1", "--db", db])
    assert rc != 0


def test_pin_rejects_unknown_table(db):
    rc = cli.main(["pin", "robots", "1", "--db", db])
    assert rc != 0


def test_pin_missing_id_fails(db):
    rc = cli.main(["pin", "memes", "999", "--db", db])
    assert rc != 0
    assert _rows(db, "SELECT COUNT(*) c FROM audit_log "
                     "WHERE target_table='memes'")[0]["c"] == 0


# ── show / ls read paths ───────────────────────────────────────────────────────

def test_show_prints_row(db, capsys):
    rc = cli.main(["show", "tasks", "1", "--db", db])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Old" in out
    assert "title" in out


def test_show_missing_id_fails(db):
    assert cli.main(["show", "tasks", "999", "--db", db]) != 0


def test_ls_lists_rows(db, capsys):
    cli.main(["ls", "tasks", "--db", db])
    out = capsys.readouterr().out
    assert "Old" in out


def test_ls_status_filter(db, capsys):
    cli.main(["set", "tasks", "1", "status", "done", "--db", db])
    cli.main(["ls", "tasks", "--status", "active", "--db", db])
    assert "Old" not in capsys.readouterr().out


# ── diary: TEXT primary key (date, no id) ──────────────────────────────────────

@pytest.fixture()
def diary_db(db):
    conn = storage.connect(db)
    conn.execute("INSERT INTO diary(date,content) VALUES('2026-05-17','draft')")
    conn.commit()
    conn.close()
    return db


def test_set_diary_by_date(diary_db):
    rc = cli.main(["set", "diary", "2026-05-17", "content", "final", "--db",
                   diary_db])
    assert rc == 0
    assert _rows(diary_db, "SELECT content FROM diary")[0]["content"] == "final"


def test_rm_diary_by_date(diary_db):
    rc = cli.main(["rm", "diary", "2026-05-17", "--db", diary_db])
    assert rc == 0
    assert _rows(diary_db, "SELECT COUNT(*) c FROM diary")[0]["c"] == 0


def test_show_diary_by_date(diary_db, capsys):
    assert cli.main(["show", "diary", "2026-05-17", "--db", diary_db]) == 0
    assert "draft" in capsys.readouterr().out


# ── add milestone ─────────────────────────────────────────────────────────────

def test_add_milestone_inserts_with_timestamps(db, capsys):
    rc = cli.main([
        "add", "milestone",
        "--scope", "me", "--date", "2026-05-22",
        "--title", "Started Round 2",
        "--description", "milestone reconcile work",
        "--db", db,
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Started Round 2" in out
    row = _rows(db, "SELECT scope, date, title, description, pinned, "
                    "created_at, updated_at, source_hash "
                    "FROM milestones WHERE title = 'Started Round 2'")[0]
    assert row["scope"] == "me"
    assert row["date"] == "2026-05-22"
    assert row["description"] == "milestone reconcile work"
    assert row["pinned"] == 0
    assert row["created_at"] is not None
    assert row["updated_at"] is not None
    assert row["source_hash"] is not None


def test_add_milestone_audits(db):
    cli.main(["add", "milestone", "--scope", "us", "--date", "2026-01-17",
              "--title", "First meeting", "--db", db])
    a = _rows(db,
              "SELECT action FROM audit_log WHERE target_table='milestones'")
    assert a and a[0]["action"] == "insert"


def test_add_milestone_pinned_flag(db):
    cli.main(["add", "milestone", "--scope", "me", "--date", "2026-05-15",
              "--title", "Marrow rebuild", "--pinned", "--db", db])
    row = _rows(db, "SELECT pinned FROM milestones WHERE title = ?",
                ("Marrow rebuild",))[0]
    assert row["pinned"] == 1


def test_add_milestone_rejects_bad_scope(db):
    rc = cli.main(["add", "milestone", "--scope", "wrong", "--date",
                   "2026-05-22", "--title", "x", "--db", db])
    assert rc != 0
    assert _rows(db, "SELECT COUNT(*) c FROM milestones")[0]["c"] == 0


def test_add_milestone_rejects_bad_date(db):
    rc = cli.main(["add", "milestone", "--scope", "me", "--date",
                   "2026/05/22", "--title", "x", "--db", db])
    assert rc != 0


def test_add_milestone_rejects_empty_title(db):
    rc = cli.main(["add", "milestone", "--scope", "me", "--date",
                   "2026-05-22", "--title", "   ", "--db", db])
    assert rc != 0


def test_add_unknown_target_fails(db):
    # argparse choices=... rejects the value before our code runs -> SystemExit
    with pytest.raises(SystemExit):
        cli.main(["add", "robots", "--scope", "me", "--date", "2026-05-22",
                  "--title", "x", "--db", db])
