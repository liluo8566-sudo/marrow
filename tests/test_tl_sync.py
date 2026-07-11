"""P3 — cross-window tl sync inject + {last_tl} substitution + tl_add hint."""
from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

from marrow import config, storage, tl_nudge, tl_sync, tl_writer

_MELB = ZoneInfo("Australia/Melbourne")


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = storage.init_db(str(tmp_path / "sync.db"))
    yield c
    c.close()


def _hhmm(hours_ago: float) -> str:
    return (_dt.datetime.now(_MELB) - _dt.timedelta(hours=hours_ago)).strftime("%H:%M")


def _add(conn, sid, body="body", **kw):
    return tl_writer.tl_add(
        conn, f"{_hhmm(2)}-{_hhmm(1.9)}", body,
        n_word="愉悦", y_word="委屈", sid=sid, **kw,
    )


# ── cross-window inject ──────────────────────────────────────────────────────

def test_first_prompt_initialises_without_backfill(conn):
    _add(conn, "other", body="pre-existing")
    # First render for a brand-new session: init only, no inject.
    assert tl_sync.render_update(conn, "me") == ""
    # last-seen advanced to current max — later same-session prompt stays quiet
    # until something new arrives.
    assert tl_sync.render_update(conn, "me") == ""


def test_new_tl_from_other_sid_appears(conn):
    tl_sync.render_update(conn, "me")  # init
    _add(conn, "other", body="fresh news")
    frag = tl_sync.render_update(conn, "me")
    assert frag.startswith("## TL update")
    assert "fresh news" in frag
    assert "(cli)" in frag
    # content line carries its HH:mm-HH:mm range
    assert f"{_hhmm(2)}-{_hhmm(1.9)}" in frag


def test_own_sid_excluded(conn):
    tl_sync.render_update(conn, "me")  # init
    _add(conn, "me", body="my own line")
    assert tl_sync.render_update(conn, "me") == ""


def test_last_seen_advances(conn):
    tl_sync.render_update(conn, "me")  # init
    _add(conn, "other", body="one")
    assert "one" in tl_sync.render_update(conn, "me")
    # already consumed → no repeat
    assert tl_sync.render_update(conn, "me") == ""


def test_cap_and_more(conn):
    tl_sync.render_update(conn, "me")  # init
    for i in range(7):
        _add(conn, "other", body=f"line{i}")
    frag = tl_sync.render_update(conn, "me")
    lines = frag.splitlines()
    assert lines[0] == "## TL update"
    assert lines[1] == "- +2 more"  # 7 rows, cap 5 → 2 dropped
    # newest 5 kept (line2..line6)
    assert "line6" in frag and "line2" in frag
    assert "line0" not in frag and "line1" not in frag


def test_disabled_silences(conn, monkeypatch):
    monkeypatch.setattr(tl_sync, "enabled", lambda: False)
    _add(conn, "other", body="hidden")
    assert tl_sync.render_update(conn, "me") == ""


# ── {last_tl} substitution ───────────────────────────────────────────────────

def test_last_tl_hhmm_value(conn):
    _add(conn, "me", body="mine")
    assert tl_sync.last_tl_hhmm(conn, "me") == _hhmm(1.9)


def test_last_tl_hhmm_na(conn):
    assert tl_sync.last_tl_hhmm(conn, "me") == "n/a"


def test_nudge_substitution(conn, monkeypatch):
    monkeypatch.setattr(tl_nudge, "enabled", lambda: True)
    monkeypatch.setattr(tl_nudge, "threshold", lambda: 1)
    monkeypatch.setattr(tl_nudge, "nudge_text", lambda: "Last tl @{last_tl}.")
    _add(conn, "me", body="mine")
    out = tl_nudge.maybe_nudge(conn, "me")
    assert out == f"Last tl @{_hhmm(1.9)}."


def test_nudge_substitution_na(conn, monkeypatch):
    monkeypatch.setattr(tl_nudge, "enabled", lambda: True)
    monkeypatch.setattr(tl_nudge, "threshold", lambda: 1)
    monkeypatch.setattr(tl_nudge, "nudge_text", lambda: "Last tl @{last_tl}.")
    out = tl_nudge.maybe_nudge(conn, "fresh")
    assert out == "Last tl @n/a."


# ── tl_add return hint ───────────────────────────────────────────────────────

def test_tl_add_first_hint(conn):
    res = _add(conn, "me", body="first")
    assert res["line"].endswith("(first tl this session)")


def test_tl_add_previous_hint(conn):
    _add(conn, "me", body="first")
    res = _add(conn, "me", body="second")
    assert res["line"].endswith(f"(previous tl this session: {_hhmm(1.9)})")
