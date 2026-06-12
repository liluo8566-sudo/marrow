"""Tests for scripts/clean_harness_events.py migration script."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _make_db(tmp_path: Path) -> tuple[str, sqlite3.Connection]:
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            timestamp TEXT,
            role TEXT,
            content TEXT,
            channel TEXT,
            source_hash TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE TABLE events_vec (
            rowid INTEGER PRIMARY KEY,
            embedding BLOB
        );
        CREATE TABLE events_vec_meta (
            rowid INTEGER PRIMARY KEY,
            embedder_id TEXT NOT NULL,
            dim INTEGER NOT NULL
        );
        CREATE TABLE event_tombstones (
            source_hash TEXT PRIMARY KEY,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
    """)
    return db_path, conn


def _insert_event(conn, eid, session_id, timestamp, role, content):
    h = _hash(session_id, timestamp, role, content)
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, role, content, source_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (eid, session_id, timestamp, role, content, h),
    )
    # Add a vec entry so we can verify it gets deleted
    conn.execute("INSERT OR IGNORE INTO events_vec (rowid, embedding) VALUES (?, ?)", (eid, b'\x00' * 4))
    conn.execute("INSERT OR IGNORE INTO events_vec_meta (rowid, embedder_id, dim) VALUES (?, ?, ?)", (eid, "bge-m3", 1024))
    conn.commit()


def _run_script(db_path, *extra_args):
    from scripts.clean_harness_events import main
    argv = list(extra_args) + ["--db", db_path]
    return main(argv)


# ── dry-run: nothing changes ─────────────────────────────────────────────────

def test_dry_run_no_changes(tmp_path):
    db_path, conn = _make_db(tmp_path)
    _insert_event(conn, 1, "s1", "t1", "user",
                  "hello <command-message>noise</command-message> world")
    _run_script(db_path, "--dry-run")
    row = conn.execute("SELECT content FROM events WHERE id=1").fetchone()
    assert row["content"] == "hello <command-message>noise</command-message> world"
    # vec entry untouched
    assert conn.execute("SELECT 1 FROM events_vec WHERE rowid=1").fetchone() is not None
    conn.close()


# ── apply: non-empty cleaned content → UPDATE ────────────────────────────────

def test_apply_updates_dirty_row(tmp_path):
    db_path, conn = _make_db(tmp_path)
    _insert_event(conn, 1, "s1", "t1", "user",
                  "hello <command-message>noise</command-message> world")
    _run_script(db_path, "--apply")
    row = conn.execute("SELECT content, source_hash FROM events WHERE id=1").fetchone()
    assert row["content"] == "hello world"
    expected_hash = _hash("s1", "t1", "user", "hello world")
    assert row["source_hash"] == expected_hash
    conn.close()


def test_apply_deletes_vec_entry_for_updated_row(tmp_path):
    db_path, conn = _make_db(tmp_path)
    _insert_event(conn, 1, "s1", "t1", "user",
                  "text [Image #3] more text")
    _run_script(db_path, "--apply")
    assert conn.execute("SELECT 1 FROM events_vec WHERE rowid=1").fetchone() is None
    assert conn.execute("SELECT 1 FROM events_vec_meta WHERE rowid=1").fetchone() is None
    conn.close()


# ── apply: empty after stripping → DELETE + tombstone ────────────────────────

def test_apply_deletes_empty_row(tmp_path):
    db_path, conn = _make_db(tmp_path)
    _insert_event(conn, 2, "s1", "t2", "user",
                  "<command-message>everything is noise</command-message>")
    _run_script(db_path, "--apply")
    assert conn.execute("SELECT 1 FROM events WHERE id=2").fetchone() is None
    conn.close()


def test_apply_inserts_tombstone_for_deleted_row(tmp_path):
    db_path, conn = _make_db(tmp_path)
    content = "<command-message>everything is noise</command-message>"
    _insert_event(conn, 2, "s1", "t2", "user", content)
    _run_script(db_path, "--apply")
    old_hash = _hash("s1", "t2", "user", content)
    tomb = conn.execute(
        "SELECT source_hash, reason FROM event_tombstones WHERE source_hash=?",
        (old_hash,),
    ).fetchone()
    assert tomb is not None
    assert "clean_harness_events" in tomb["reason"]
    conn.close()


def test_apply_deletes_vec_entry_for_deleted_row(tmp_path):
    db_path, conn = _make_db(tmp_path)
    _insert_event(conn, 2, "s1", "t2", "user",
                  "<local-command-stdout>output</local-command-stdout>")
    _run_script(db_path, "--apply")
    assert conn.execute("SELECT 1 FROM events_vec WHERE rowid=2").fetchone() is None
    assert conn.execute("SELECT 1 FROM events_vec_meta WHERE rowid=2").fetchone() is None
    conn.close()


# ── rows without harness markers are untouched ───────────────────────────────

def test_clean_row_untouched(tmp_path):
    db_path, conn = _make_db(tmp_path)
    _insert_event(conn, 3, "s1", "t3", "user", "just a normal message 你好")
    _run_script(db_path, "--apply")
    row = conn.execute("SELECT content FROM events WHERE id=3").fetchone()
    assert row["content"] == "just a normal message 你好"
    # vec entry also untouched
    assert conn.execute("SELECT 1 FROM events_vec WHERE rowid=3").fetchone() is not None
    conn.close()


# ── image ref in body is stripped ────────────────────────────────────────────

def test_image_ref_stripped_from_body(tmp_path):
    db_path, conn = _make_db(tmp_path)
    _insert_event(conn, 4, "s1", "t4", "user", "see [Image #1] here")
    _run_script(db_path, "--apply")
    row = conn.execute("SELECT content FROM events WHERE id=4").fetchone()
    assert "[Image #1]" not in row["content"]
    assert "see" in row["content"] and "here" in row["content"]
    conn.close()
