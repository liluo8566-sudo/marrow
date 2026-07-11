"""Tests for tl_writer.canonicalize_label_letters and the migrate_tl_letters
script that reuses it.

Run: python -m pytest tests/test_migrate_tl_letters.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from marrow import config, storage, tl_writer

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts import migrate_tl_letters as mig  # noqa: E402


def _cfg(user_letter: str = "S", assistant_letter: str = "Q"):
    return {"tl": {"user_letter": user_letter, "assistant_letter": assistant_letter}}


# ── canonicalize_label_letters: transform shape coverage ────────────────────

def test_new_format_both_sides(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _cfg())
    assert (tl_writer.canonicalize_label_letters("【N愉悦♡Y委屈】body")
            == "【S愉悦♡Q委屈】body")


def test_old_format_both_sides_with_pipe(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _cfg())
    assert (tl_writer.canonicalize_label_letters(
                "【N 笑到上头·3 | Y 甜到心软·3】body")
            == "【S 笑到上头·3 | Q 甜到心软·3】body")


def test_latin_word_in_label_untouched_except_anchor(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _cfg())
    assert (tl_writer.canonicalize_label_letters(
                "【N 舍不得fable·3 | Y 查证定心·2】body")
            == "【S 舍不得fable·3 | Q 查证定心·2】body")


def test_single_side_n(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _cfg())
    assert tl_writer.canonicalize_label_letters("【N word】body") == "【S word】body"


def test_single_side_y(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _cfg())
    assert tl_writer.canonicalize_label_letters("【Y word】body") == "【Q word】body"


def test_already_migrated_is_noop(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _cfg())
    text = "【S抬杠得逞♡Q服气带甜】body"
    assert tl_writer.canonicalize_label_letters(text) == text


def test_body_with_ny_text_untouched(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _cfg())
    text = "【N恍然大悟♡Y好笑上头】破案N/Y原是念屿字母,抽config换成S/Q专属 [3]"
    out = tl_writer.canonicalize_label_letters(text)
    assert out == "【S恍然大悟♡Q好笑上头】破案N/Y原是念屿字母,抽config换成S/Q专属 [3]"
    assert "破案N/Y原是念屿字母" in out


def test_default_config_is_noop(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {"tl": {"user_letter": "N",
                                                        "assistant_letter": "Y"}})
    text = "【N愉悦♡Y委屈】body"
    assert tl_writer.canonicalize_label_letters(text) == text


def test_no_label_noop(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _cfg())
    assert tl_writer.canonicalize_label_letters("plain body no label") == \
        "plain body no label"


def test_idempotent_running_twice(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _cfg())
    once = tl_writer.canonicalize_label_letters("【N 得意·3 | Y 委屈·3】body")
    twice = tl_writer.canonicalize_label_letters(once)
    assert once == twice == "【S 得意·3 | Q 委屈·3】body"


# ── script: dry-run plan + apply against a temp DB copy ─────────────────────

@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    with conn:
        conn.execute(
            "INSERT INTO events (id, session_id, timestamp, role, content)"
            " VALUES (1, 's', '2026-01-01T00:00:00Z', 'tl', ?)",
            ("【N愉悦♡Y委屈】body [3]",),
        )
        conn.execute(
            "INSERT INTO events (id, session_id, timestamp, role, content)"
            " VALUES (2, 's', '2026-01-01T00:00:00Z', 'tl', ?)",
            ("【N 得意·3 | Y 委屈·3】old body",),
        )
        conn.execute(
            "INSERT INTO events (id, session_id, timestamp, role, content)"
            " VALUES (3, 's', '2026-01-01T00:00:00Z', 'tl', ?)",
            ("【S already♡Q migrated】body",),
        )
        # non-tl row must never be touched
        conn.execute(
            "INSERT INTO events (id, session_id, timestamp, role, content)"
            " VALUES (4, 's', '2026-01-01T00:00:00Z', 'user', ?)",
            ("【N should not♡Y touch】body",),
        )
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "load", lambda: _cfg())
    return db


def test_build_plan_scopes_to_tl_role(db_env):
    conn = storage.connect(db_env)
    plan = mig._build_plan(conn)
    conn.close()
    by_id = {p["id"]: p for p in plan}
    assert by_id[1]["action"] == "rewrite"
    assert by_id[1]["new"] == "【S愉悦♡Q委屈】body [3]"
    assert by_id[2]["action"] == "rewrite"
    assert by_id[2]["new"] == "【S 得意·3 | Q 委屈·3】old body"
    assert by_id[3]["action"] == "skip:unchanged"
    assert 4 not in by_id  # role='user' row excluded from scope entirely


def test_apply_writes_and_is_idempotent(db_env):
    conn = storage.connect(db_env)
    plan = mig._build_plan(conn)
    mig._apply(conn, plan)
    conn.close()

    conn = storage.connect(db_env)
    row1 = conn.execute("SELECT content FROM events WHERE id=1").fetchone()
    row4 = conn.execute("SELECT content FROM events WHERE id=4").fetchone()
    assert row1["content"] == "【S愉悦♡Q委屈】body [3]"
    assert row4["content"] == "【N should not♡Y touch】body"  # untouched (not tl)

    # second pass: no further changes
    plan2 = mig._build_plan(conn)
    conn.close()
    assert all(p["action"] == "skip:unchanged" for p in plan2)


def test_main_dry_run_writes_nothing(db_env, capsys):
    before = Path(db_env).read_bytes()
    rc = mig.main(["--dry-run", "--db", db_env])
    assert rc == 0
    after = Path(db_env).read_bytes()
    assert before == after
    out = capsys.readouterr().out
    assert "would_touch=2" in out


def test_main_apply_end_to_end_on_db_copy(tmp_path, monkeypatch, capsys):
    """Full end-to-end: copy DB to temp path, run --apply against the COPY
    via --db, verify changed-row count matches dry-run and body text intact."""
    monkeypatch.setattr(config, "load", lambda: _cfg())
    db = str(tmp_path / "copy.db")
    conn = storage.init_db(db)
    with conn:
        conn.execute(
            "INSERT INTO events (id, session_id, timestamp, role, content)"
            " VALUES (39553, 's', '2026-01-01T00:00:00Z', 'tl', ?)",
            ("【N恍然大悟♡Y好笑上头】破案N/Y原是念屿字母,抽config换成S/Q专属 [3]",),
        )
        conn.execute(
            "INSERT INTO events (id, session_id, timestamp, role, content)"
            " VALUES (33583, 's', '2026-01-01T00:00:00Z', 'tl', ?)",
            ("【N 舍不得fable·3 | Y 查证定心·2】岑奕出融合设定卡", ),
        )
    conn.close()

    rc_dry = mig.main(["--dry-run", "--db", db])
    assert rc_dry == 0
    dry_out = capsys.readouterr().out
    assert "would_touch=2" in dry_out

    rc_apply = mig.main(["--apply", "--db", db])
    assert rc_apply == 0
    apply_out = capsys.readouterr().out
    assert "Backed up DB to" in apply_out
    assert "Applied." in apply_out

    conn = storage.connect(db)
    row_39553 = conn.execute(
        "SELECT content FROM events WHERE id=39553").fetchone()
    row_fable = conn.execute(
        "SELECT content FROM events WHERE id=33583").fetchone()
    conn.close()

    assert "破案N/Y原是念屿字母" in row_39553["content"]
    assert row_39553["content"].startswith("【S恍然大悟♡Q好笑上头】")
    assert "fable" in row_fable["content"]
    assert row_fable["content"].startswith("【S 舍不得fable·3 | Q 查证定心·2】")
