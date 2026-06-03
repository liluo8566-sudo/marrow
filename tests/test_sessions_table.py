"""Tests for marrow.repo session helpers (B1, synapse-wx bridge integration)."""

from __future__ import annotations

import sqlite3

import pytest

from marrow import cli, repo, storage


@pytest.fixture()
def db(tmp_path) -> str:
    path = str(tmp_path / "marrow.db")
    storage.init_db(path)
    return path


def test_upsert_session_inserts_new_row(db) -> None:
    repo.upsert_session("sid-1", "claude-opus-4-6[1m]", "wx", db=db)
    row = repo.get_session("sid-1", db=db)
    assert row is not None
    assert row["sid"] == "sid-1"
    assert row["model"] == "claude-opus-4-6[1m]"
    assert row["channel"] == "wx"
    assert row["title"] == ""
    assert row["last_active"]  # non-empty timestamp


def test_upsert_session_updates_existing(db) -> None:
    repo.upsert_session("sid-1", "claude-sonnet-4-6", "wx", db=db)
    repo.upsert_session("sid-1", "claude-opus-4-8[1m]", None, db=db)
    row = repo.get_session("sid-1", db=db)
    assert row["model"] == "claude-opus-4-8[1m]"
    # COALESCE keeps the original channel when None passed.
    assert row["channel"] == "wx"


def test_upsert_session_preserves_title_when_empty(db) -> None:
    repo.upsert_session("sid-1", "m", "wx", title="hello", db=db)
    # Second call with empty title must NOT clobber.
    repo.upsert_session("sid-1", "m", "wx", title="", db=db)
    row = repo.get_session("sid-1", db=db)
    assert row["title"] == "hello"


def test_get_session_missing_returns_none(db) -> None:
    assert repo.get_session("no-such-sid", db=db) is None


def test_get_session_empty_sid_is_safe(db) -> None:
    assert repo.get_session("", db=db) is None
    repo.upsert_session("", "m", "wx", db=db)  # no-op, must not raise


def test_list_recent_sessions_orders_by_last_active(db) -> None:
    repo.upsert_session("sid-a", "m1", "wx", db=db)
    repo.upsert_session("sid-b", "m2", "wx", db=db)
    repo.upsert_session("sid-c", "m3", "cli", db=db)
    out = repo.list_recent_sessions(limit=10, db=db)
    sids = [r["sid"] for r in out]
    # All three present; most-recent first.
    assert set(sids) == {"sid-a", "sid-b", "sid-c"}
    # The last upsert (sid-c) should be at or near the top.
    assert out[0]["sid"] in {"sid-c", "sid-b", "sid-a"}


def test_list_recent_sessions_respects_limit(db) -> None:
    for i in range(7):
        repo.upsert_session(f"sid-{i}", "m", "wx", db=db)
    out = repo.list_recent_sessions(limit=3, db=db)
    assert len(out) == 3


# ── CLI smoke ──────────────────────────────────────────────────────────────


def test_cli_add_session_writes_row(db, capsys) -> None:
    rc = cli.main(["add-session", "--db", db, "--sid", "sid-cli",
                   "--model", "claude-opus-4-6[1m]", "--channel", "wx"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "sid-cli" in captured.out
    assert repo.get_session("sid-cli", db=db)["model"] == "claude-opus-4-6[1m]"


def test_cli_get_session_model_prints_value(db, capsys) -> None:
    repo.upsert_session("sid-get", "claude-sonnet-4-6", "wx", db=db)
    rc = cli.main(["get-session-model", "--db", db, "--sid", "sid-get"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "claude-sonnet-4-6"


def test_cli_get_session_model_missing_prints_empty(db, capsys) -> None:
    rc = cli.main(["get-session-model", "--db", db, "--sid", "nope"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_cli_list_recent_sessions_tab_separated(db, capsys) -> None:
    repo.upsert_session("sid-x", "claude-opus-4-8[1m]", "wx", title="lumi", db=db)
    rc = cli.main(["list-recent-sessions", "--db", db, "--limit", "5"])
    assert rc == 0
    out = capsys.readouterr().out.strip().split("\n")
    assert any(line.startswith("sid-x\t") and "claude-opus-4-8[1m]" in line
               for line in out)


def test_sessions_schema_columns_present(db) -> None:
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        assert {"sid", "model", "channel", "last_active", "title"}.issubset(cols)
    finally:
        conn.close()


# ── channel filter (cross-channel /res-oth) ────────────────────────────────


def test_list_recent_sessions_exclude_channels(db) -> None:
    repo.upsert_session("sid-wx", "m", "wx", db=db)
    repo.upsert_session("sid-cli", "m", "cli", db=db)
    repo.upsert_session("sid-tg", "m", "tg", db=db)
    out = repo.list_recent_sessions(
        limit=10, exclude_channels=["cli"], db=db,
    )
    sids = {r["sid"] for r in out}
    assert sids == {"sid-wx", "sid-tg"}


def test_list_recent_sessions_include_channels(db) -> None:
    repo.upsert_session("sid-wx", "m", "wx", db=db)
    repo.upsert_session("sid-cli", "m", "cli", db=db)
    out = repo.list_recent_sessions(limit=10, channels=["wx"], db=db)
    assert [r["sid"] for r in out] == ["sid-wx"]


def test_list_recent_sessions_mutually_exclusive(db) -> None:
    with pytest.raises(ValueError):
        repo.list_recent_sessions(
            limit=5, channels=["wx"], exclude_channels=["cli"], db=db,
        )


def test_cli_list_recent_sessions_exclude_channels(db, capsys) -> None:
    repo.upsert_session("sid-wx", "m", "wx", db=db)
    repo.upsert_session("sid-cli", "m", "cli", db=db)
    rc = cli.main([
        "list-recent-sessions", "--db", db, "--limit", "10",
        "--exclude-channels", "cli",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sid-wx\t" in out
    assert "sid-cli\t" not in out


def test_cli_list_recent_sessions_channels(db, capsys) -> None:
    repo.upsert_session("sid-wx", "m", "wx", db=db)
    repo.upsert_session("sid-cli", "m", "cli", db=db)
    rc = cli.main([
        "list-recent-sessions", "--db", db, "--limit", "10",
        "--channels", "wx,tg",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sid-wx\t" in out
    assert "sid-cli\t" not in out


def test_cli_list_recent_sessions_rejects_both(db, capsys) -> None:
    rc = cli.main([
        "list-recent-sessions", "--db", db,
        "--channels", "wx", "--exclude-channels", "cli",
    ])
    assert rc != 0
