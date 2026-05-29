"""Tests for handover_diff: id-based DOING diff apply, Done 24h roll-off,
hand-edit survival, Note remove-done, plus the git_log loader in
sessionend_async. compute_apply is a pure transform — most cases test it
directly; a few drive the flock/atomic path via apply_diff.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from marrow import handover_diff, handover_render, sessionend_async, storage


_BASE = (
    "# Handover\n\n"
    "## Done\n{done}\n\n"
    "## Doing\n{doing}\n\n"
    "## Lumi's Note\n{note}\n"
)


def _file(done="- N/A", doing="- N/A", note="- N/A"):
    return _BASE.format(done=done, doing=doing, note=note)


def _thread(scope, title, current="s", nxt="n", ref="N/A", ident=None):
    body = (f"{scope_head(scope, title)}\n"
            f"  - Current: {current}\n"
            f"  - Next: {nxt}\n"
            f"  - Reference: {ref}")
    if ident is not None:
        body += f"\n<!-- id:{ident} -->"
    return body


def scope_head(scope, title):
    return f"1. [{scope}] - {title}"


# ── ADD / fresh id ───────────────────────────────────────────────────────────

def test_add_assigns_fresh_monotonic_id():
    prior = _file()
    diff = {"close": [], "keep": [], "update": [],
            "add": ["[Marrow] - feature\n  - Current: a\n  - Next: b"
                    "\n  - Reference: N/A"]}
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot="", diff=diff, note_done=[],
        now_epoch=1700000000)
    assert "<!-- id:1 -->" in out
    assert "feature" in out


def test_add_id_continues_past_existing_max():
    prior = _file(doing=_thread("Marrow", "A", ident=7))
    diff = {"close": [], "keep": [], "update": [],
            "add": ["[Study] - B\n  - Current: c\n  - Next: d"
                    "\n  - Reference: N/A"]}
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot=prior, diff=diff, note_done=[],
        now_epoch=1700000000)
    assert "<!-- id:7 -->" in out
    assert "<!-- id:8 -->" in out  # max+1


# ── CLOSE ─────────────────────────────────────────────────────────────────────

def test_close_moves_thread_to_done_with_epoch():
    prior = _file(doing=_thread("Marrow", "ship it", current="done", ident=3))
    diff = {"close": [3], "keep": [], "update": [], "add": []}
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot=prior, diff=diff, note_done=[],
        now_epoch=1700000000)
    done_body = handover_diff._section_body(out, "Done")
    assert "ship it" in done_body
    assert "<!-- done:1700000000 -->" in done_body
    # No longer in Doing.
    doing, _ = handover_diff.parse_doing(handover_diff._section_body(out, "Doing"))
    assert 3 not in doing


def test_close_unknown_id_is_noop():
    prior = _file(doing=_thread("Marrow", "A", ident=1))
    diff = {"close": [99], "keep": [], "update": [], "add": []}
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot=prior, diff=diff, note_done=[],
        now_epoch=1700000000)
    doing, _ = handover_diff.parse_doing(handover_diff._section_body(out, "Doing"))
    assert 1 in doing  # untouched


# ── UPDATE / KEEP / unmentioned survival ─────────────────────────────────────

def test_update_replaces_block_keeps_id():
    prior = _file(doing=_thread("Marrow", "A", current="old", ident=5))
    diff = {"close": [], "keep": [], "add": [],
            "update": [{"id": 5,
                        "block": "#5 [Marrow] - A\n  - Current: NEW state"
                                 "\n  - Next: n2\n  - Reference: N/A"}]}
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot=prior, diff=diff, note_done=[],
        now_epoch=1700000000)
    doing, _ = handover_diff.parse_doing(handover_diff._section_body(out, "Doing"))
    assert 5 in doing
    assert "NEW state" in doing[5]
    assert "old" not in doing[5]


def test_keep_is_noop():
    prior = _file(doing=_thread("Marrow", "A", current="s1", ident=2))
    diff = {"close": [], "keep": [2], "update": [], "add": []}
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot=prior, diff=diff, note_done=[],
        now_epoch=1700000000)
    doing, _ = handover_diff.parse_doing(handover_diff._section_body(out, "Doing"))
    assert 2 in doing and "s1" in doing[2]


def test_unmentioned_id_survives():
    """An open id the diff did NOT mention must never be silently dropped."""
    two = (_thread("Marrow", "A", ident=1) + "\n"
           + _thread("Study", "B", ident=2))
    prior = _file(doing=two)
    diff = {"close": [1], "keep": [], "update": [], "add": []}  # 2 not mentioned
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot=prior, diff=diff, note_done=[],
        now_epoch=1700000000)
    doing, _ = handover_diff.parse_doing(handover_diff._section_body(out, "Doing"))
    assert 2 in doing  # survived


# ── Done 24h roll-off ─────────────────────────────────────────────────────────

def test_done_rolloff_drops_old_keeps_fresh():
    now = 1700000000
    done = (f"- old — x <!-- done:{now - 25 * 3600} -->\n"
            f"- fresh — y <!-- done:{now - 3600} -->")
    prior = _file(done=done)
    diff = {"close": [], "keep": [], "update": [], "add": []}
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot=prior, diff=diff, note_done=[],
        now_epoch=now)
    done_body = handover_diff._section_body(out, "Done")
    assert "old" not in done_body
    assert "fresh" in done_body


# ── hand-edit survival ────────────────────────────────────────────────────────

def test_hand_deleted_id_not_revived():
    """User hand-deleted id 9 from the file; KEEP must not revive it."""
    snapshot = _file(doing=(_thread("Marrow", "A", ident=1) + "\n"
                            + _thread("Study", "gone", ident=9)))
    # Current file: id 9 removed by hand.
    current = _file(doing=_thread("Marrow", "A", ident=1))
    diff = {"close": [], "keep": [1, 9], "update": [], "add": []}
    out = handover_diff.compute_apply(
        prior_text=current, last_snapshot=snapshot, diff=diff, note_done=[],
        now_epoch=1700000000)
    assert "<!-- id:9 -->" not in out
    assert "gone" not in out


def test_hand_added_block_gets_id_and_survives():
    """A Doing block with no id (hand-added) gets a fresh id and is kept."""
    current = _file(doing=(
        _thread("Marrow", "A", ident=1) + "\n"
        "2. [Daily] - hand added\n  - Current: hs\n  - Next: hn"
        "\n  - Reference: N/A"))
    diff = {"close": [], "keep": [], "update": [], "add": []}
    out = handover_diff.compute_apply(
        prior_text=current, last_snapshot=current, diff=diff, note_done=[],
        now_epoch=1700000000)
    assert "hand added" in out
    doing, no_id = handover_diff.parse_doing(
        handover_diff._section_body(out, "Doing"))
    assert no_id == []  # all blocks now carry ids
    assert any("hand added" in b for b in doing.values())


# ── Note remove-done ──────────────────────────────────────────────────────────

def test_note_remove_done_drops_listed_lines_only():
    prior = _file(note="- buy hand cream\n- recharge SIM\n- book GP")
    diff = {"close": [], "keep": [], "update": [], "add": []}
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot=prior, diff=diff,
        note_done=["- buy hand cream"], now_epoch=1700000000)
    note_body = handover_diff._section_body(out, "Lumi's Note")
    assert "buy hand cream" not in note_body
    assert "recharge SIM" in note_body
    assert "book GP" in note_body


def test_note_remove_done_tolerates_rephrase_punctuation():
    """NOTE_DONE matches via hash_bullet — punctuation/marker differences ok."""
    prior = _file(note="- Buy hand cream!")
    diff = {"close": [], "keep": [], "update": [], "add": []}
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot=prior, diff=diff,
        note_done=["buy hand cream"], now_epoch=1700000000)
    note_body = handover_diff._section_body(out, "Lumi's Note")
    assert "hand cream" not in note_body


def test_empty_note_done_leaves_note_untouched():
    prior = _file(note="- keep me\n- and me")
    diff = {"close": [], "keep": [], "update": [], "add": []}
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot=prior, diff=diff, note_done=[],
        now_epoch=1700000000)
    note_body = handover_diff._section_body(out, "Lumi's Note")
    assert "keep me" in note_body and "and me" in note_body


def test_note_never_rewritten_or_appended():
    """compute_apply only removes; it never adds or reorders Note lines."""
    prior = _file(note="- a\n- b\n- c")
    diff = {"close": [], "keep": [], "update": [], "add": []}
    out = handover_diff.compute_apply(
        prior_text=prior, last_snapshot=prior, diff=diff,
        note_done=["- b"], now_epoch=1700000000)
    note_body = handover_diff._section_body(out, "Lumi's Note")
    assert note_body.splitlines() == ["- a", "- c"]  # order preserved, b gone


# ── full round-trip via apply_diff (flock + atomic) ─────────────────────────

def test_apply_diff_round_trip_close_then_rolloff(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    conn = storage.init_db(db)
    h = tmp_path / "handover.md"
    monkeypatch.setattr(handover_render, "_RENDERED_PATH", h)
    # Seed + ADD.
    handover_diff.apply_diff(
        conn, "s1",
        {"close": [], "keep": [], "update": [],
         "add": ["[Marrow] - feature\n  - Current: started\n  - Next: finish"
                 "\n  - Reference: N/A"]}, [])
    assert "<!-- id:1 -->" in h.read_text(encoding="utf-8")
    # CLOSE id 1.
    handover_diff.apply_diff(
        conn, "s2", {"close": [1], "keep": [], "update": [], "add": []}, [])
    body = h.read_text(encoding="utf-8")
    assert "feature" in handover_diff._section_body(body, "Done")
    doing, _ = handover_diff.parse_doing(
        handover_diff._section_body(body, "Doing"))
    assert doing == {}
    conn.close()


# ── git_log loader (sessionend_async) ────────────────────────────────────────

def test_git_log_empty_cwd_returns_empty():
    assert sessionend_async._load_git_log("", 0) == ""
    assert sessionend_async._load_git_log(None, 0) == ""


def test_git_log_off_repo_returns_empty(tmp_path):
    # A bare tmp dir is not a git repo → git returns non-zero → "".
    assert sessionend_async._load_git_log(str(tmp_path), 0) == ""


def test_git_log_in_repo_returns_commit_subjects(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args],
                       check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "first subject line")
    out = sessionend_async._load_git_log(str(repo), 0)
    assert "first subject line" in out


def test_git_log_bad_cwd_no_crash():
    # Non-existent path → git -C fails → "" (no exception).
    assert sessionend_async._load_git_log("/no/such/path/xyz", 0) == ""
