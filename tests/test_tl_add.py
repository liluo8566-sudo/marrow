"""A2 — tl_add/tl_update self rows: write, render, reconcile, coexistence gate."""
from __future__ import annotations

import datetime as _dt
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from marrow import (
    reconcile,
    sessionend_writers as sw,
    storage,
    timeline,
    tl_nudge,
    tl_writer,
)

_MELB = ZoneInfo("Australia/Melbourne")


@pytest.fixture()
def conn(tmp_path):
    c = storage.init_db(str(tmp_path / "tl.db"))
    yield c
    c.close()


def _hhmm(hours_ago: float) -> str:
    return (_dt.datetime.now(_MELB) - _dt.timedelta(hours=hours_ago)).strftime("%H:%M")


def _add(conn, sid="sess-1", body="body orig", **kw):
    return tl_writer.tl_add(
        conn, f"{_hhmm(2)}-{_hhmm(1.9)}", body,
        n_word="愉悦", y_word="委屈",
        sid=sid, **kw,
    )


# ── schema ───────────────────────────────────────────────────────────────────

def test_events_has_timerange_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    assert {"ts_start", "ts_end"} <= cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 28


# ── tl_add write + mapping ───────────────────────────────────────────────────

def test_tl_add_writes_tl_row_no_affect(conn):
    r = _add(conn)
    ev = conn.execute(
        "SELECT role, channel, content, imp, flag, ts_start, ts_end"
        " FROM events WHERE id=?", (r["event_id"],)).fetchone()
    assert ev["role"] == "tl"
    assert ev["channel"] == "cli"  # MARROW_CHANNEL unset -> default platform
    # affect phrase lives verbatim inside content; no affect table write.
    assert ev["content"] == "【N愉悦♡Y委屈】body orig [3]"
    assert ev["imp"] == 3  # default
    assert ev["flag"] is None
    assert ev["ts_start"] and ev["ts_end"]
    n_af = conn.execute("SELECT COUNT(*) c FROM affect WHERE event_id=?",
                        (r["event_id"],)).fetchone()["c"]
    assert n_af == 0


def test_explicit_importance_sets_events_imp(conn):
    r = _add(conn, importance=5)
    imp = conn.execute("SELECT imp FROM events WHERE id=?",
                       (r["event_id"],)).fetchone()["imp"]
    assert imp == 5


def test_validation_word_and_body_limits(conn):
    # word cap is 8 chars now; 7-char word is valid
    tl_writer.tl_add(conn, _hhmm(1), "b", n_word="1234567", sid="s")
    with pytest.raises(tl_writer.TlError):
        tl_writer.tl_add(conn, _hhmm(1), "b", n_word="123456789", sid="s")
    with pytest.raises(tl_writer.TlError):
        tl_writer.tl_add(conn, _hhmm(1), "x" * 51, n_word="愉悦", sid="s")
    with pytest.raises(tl_writer.TlError):
        tl_writer.tl_add(conn, _hhmm(1), "b", sid="s")  # no word


def test_single_moment_no_end(conn):
    r = tl_writer.tl_add(conn, _hhmm(1), "moment", n_word="愉悦", sid="s")
    ev = conn.execute("SELECT ts_end FROM events WHERE id=?",
                      (r["event_id"],)).fetchone()
    assert ev["ts_end"] is None


# ── render ───────────────────────────────────────────────────────────────────

def test_render_new_format_with_anchor(conn):
    r = _add(conn, body="翻日志扑空")
    md = timeline.render_timeline(conn)
    assert f"【N愉悦♡Y委屈】翻日志扑空 [3] <!-- tl:e:{r['event_id']} -->" in md
    assert f"e={r['event_id']}" in md  # trail marker


def test_render_canonicalizes_letters_with_config_override(conn, monkeypatch):
    """Render normalizes label letters to the configured tl.user_letter /
    tl.assistant_letter, even though the row was written with hardcoded
    N/Y (e.g. by a still-running old-code window)."""
    r = _add(conn, body="翻日志扑空")
    from marrow import config as _config
    monkeypatch.setattr(_config, "load",
                        lambda: {"tl": {"user_letter": "S", "assistant_letter": "Q"}})
    md = timeline.render_timeline(conn)
    assert f"【S愉悦♡Q委屈】翻日志扑空 [3] <!-- tl:e:{r['event_id']} -->" in md
    # DB content itself is untouched by render (canonicalization is display-only)
    assert conn.execute("SELECT content FROM events WHERE id=?",
                        (r["event_id"],)).fetchone()["content"] == \
        "【N愉悦♡Y委屈】翻日志扑空 [3]"


def test_no_self_rows_render_unchanged(conn):
    """Byte-identical fallback: DB with no self rows renders exactly as before
    the self-row branch (self query returns [], adds nothing)."""
    assert timeline._query_self_rows_24h(
        conn, "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z") == []
    md = timeline.render_timeline(conn)
    assert md == "## Timeline\n_none_"


# ── reconcile round-trip ─────────────────────────────────────────────────────

def _write_dash(dash: Path, text: str):
    dash.write_text(text, encoding="utf-8")
    time.sleep(0.01)
    os.utime(dash, None)


def test_self_edit_round_trip(conn, tmp_path):
    r = _add(conn, body="body original")
    eid = r["event_id"]
    dash = tmp_path / "dashboard.md"
    md = timeline.render_timeline(conn)
    _write_dash(dash, md.replace("body original", "body edited")
                        .replace("愉悦", "温柔"))
    rpt = reconcile.reconcile_timeline(conn, dash)
    assert rpt.updated == 1
    # label + i + body all live inside content now
    assert conn.execute("SELECT content FROM events WHERE id=?",
                        (eid,)).fetchone()["content"] == \
        "【N温柔♡Y委屈】body edited [3]"


def test_self_delete(conn, tmp_path):
    r = _add(conn)
    eid = r["event_id"]
    dash = tmp_path / "dashboard.md"
    _write_dash(dash, f"## Timeline\n_none_\n<!-- tl-rendered:e={eid} -->\n")
    rpt = reconcile.reconcile_timeline(conn, dash)
    assert rpt.updated == 1
    assert conn.execute("SELECT COUNT(*) c FROM events WHERE id=?",
                        (eid,)).fetchone()["c"] == 0


# ── coexistence gate ─────────────────────────────────────────────────────────

def test_seg_affect_skips_self_row_sid(conn):
    _add(conn, sid="sess-x")
    raw = "===AFFECT===\n- ep: 1\n  valence: 0.5\n  arousal: 0.3\n"
    assert sw.seg_affect(conn, raw, "sess-x", "2026-07-03") == 0
    # a different sid is not gated
    assert sw._sid_has_self_rows(conn, "other") is False


def test_seg_digest_suppresses_life_lines_for_self_sid(conn):
    _add(conn, sid="sess-y")
    raw = ("===DIGEST===\nKIND: casual\nTL: line\n"
           "LIFE:\n- 10:00 something\n===END===")
    sw.seg_digest(conn, raw, "sess-y", "2026-07-03")
    row = conn.execute(
        "SELECT life_lines FROM session_digests WHERE sid='sess-y'").fetchone()
    assert row["life_lines"] is None


# ── tl_update ────────────────────────────────────────────────────────────────

def test_tl_update_changes_body_and_label(conn):
    r = _add(conn, body="orig")
    tl_writer.tl_update(conn, r["event_id"], body="updated",
                        n_word="温柔", y_word="委屈",
                        importance=4)
    ev = conn.execute("SELECT content, imp FROM events WHERE id=?",
                      (r["event_id"],)).fetchone()
    assert ev["content"] == "【N温柔♡Y委屈】updated [4]"
    assert ev["imp"] == 4


def test_tl_update_body_only_keeps_label(conn):
    r = _add(conn, body="orig")
    tl_writer.tl_update(conn, r["event_id"], body="just body")
    ev = conn.execute("SELECT content FROM events WHERE id=?",
                      (r["event_id"],)).fetchone()
    assert ev["content"] == "【N愉悦♡Y委屈】just body [3]"


def test_tl_update_rewrites_rendered_dashboard_line(conn, monkeypatch, tmp_path):
    """A rendered row's md line must be rewritten to match the DB update, or
    the resident reconcile sees a stale diff and reverts it (silent data loss
    bug — see tl_update)."""
    r = _add(conn, body="orig")
    eid = r["event_id"]
    md = timeline.render_timeline(conn)
    dash = tmp_path / "dashboard.md"
    dash.write_text(md, encoding="utf-8")
    monkeypatch.setattr(tl_writer, "_dashboard_path", lambda: dash)

    tl_writer.tl_update(conn, eid, body="updated", n_word="温柔", y_word="委屈")

    new_md = dash.read_text(encoding="utf-8")
    assert f"【N温柔♡Y委屈】updated [3] <!-- tl:e:{eid} -->" in new_md
    assert "orig" not in new_md
    # reconcile must now see no diff -> no self-edit revert
    rpt = reconcile.reconcile_timeline(conn, dash)
    assert rpt.updated == 0
    assert conn.execute("SELECT content FROM events WHERE id=?",
                        (eid,)).fetchone()["content"] == "【N温柔♡Y委屈】updated [3]"


def test_tl_update_unrendered_row_leaves_dashboard_untouched(conn, monkeypatch, tmp_path):
    r = _add(conn, body="orig")
    eid = r["event_id"]
    dash = tmp_path / "dashboard.md"
    dash.write_text("## Timeline\n_none_\n", encoding="utf-8")
    monkeypatch.setattr(tl_writer, "_dashboard_path", lambda: dash)

    tl_writer.tl_update(conn, eid, body="updated")

    assert dash.read_text(encoding="utf-8") == "## Timeline\n_none_\n"


def test_tl_update_rejects_non_tl(conn):
    conn.execute("INSERT INTO events (session_id, timestamp, role, content,"
                 " channel) VALUES ('m', '2026-07-03T00:00:00Z', 'user', 'x',"
                 " 'manual')")
    conn.commit()
    eid = conn.execute("SELECT id FROM events WHERE channel='manual'").fetchone()["id"]
    with pytest.raises(tl_writer.TlError):
        tl_writer.tl_update(conn, eid, body="y")


# ── B3m (07-08): MARROW_CORTEX full memory parity ────────────────────────────

def test_tl_add_allowed_under_marrow_cortex(conn, monkeypatch):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    _add(conn)
    row = conn.execute("SELECT channel FROM events").fetchone()
    assert row["channel"] == "ct"


def test_tl_update_allowed_under_marrow_cortex(conn, monkeypatch):
    r = _add(conn)
    monkeypatch.setenv("MARROW_CORTEX", "1")
    monkeypatch.setenv("MARROW_CHANNEL", "ct")
    tl_writer.tl_update(conn, r["event_id"], body="should land")
    ev = conn.execute("SELECT content FROM events WHERE id=?",
                      (r["event_id"],)).fetchone()
    assert "should land" in ev["content"]


# ── nudge ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_nudge_state(monkeypatch, tmp_path):
    """Isolate the per-sid counter files from the real ~/.config/marrow tree."""
    monkeypatch.setattr(tl_nudge.config, "DATA_DIR", tmp_path / "nudge_data")
    yield


def test_nudge_on_by_default_10_turns(conn):
    assert tl_nudge.enabled() is True
    assert tl_nudge.threshold() == 10


def test_nudge_fires_at_threshold_then_resets(conn):
    for _ in range(9):
        assert tl_nudge.maybe_nudge(conn, "nud") is None
    assert tl_nudge.maybe_nudge(conn, "nud")  # 10th call fires
    # counter reset after firing -> next 9 calls stay quiet again
    for _ in range(9):
        assert tl_nudge.maybe_nudge(conn, "nud") is None
    assert tl_nudge.maybe_nudge(conn, "nud")  # fires again at the next 10


def test_nudge_resets_on_tl_add(conn):
    for _ in range(9):
        assert tl_nudge.maybe_nudge(conn, "sess-1") is None
    _add(conn, sid="sess-1")  # tl_add resets the counter
    for _ in range(9):
        assert tl_nudge.maybe_nudge(conn, "sess-1") is None
    assert tl_nudge.maybe_nudge(conn, "sess-1")  # fires only after 10 fresh turns


def test_nudge_silent_session_muted(conn):
    tl_nudge.set_silent("mute")
    assert tl_nudge.is_silent("mute") is True
    for _ in range(15):
        assert tl_nudge.maybe_nudge(conn, "mute") is None


def test_nudge_disabled_never_fires(conn, monkeypatch):
    monkeypatch.setattr(tl_nudge, "enabled", lambda: False)
    for _ in range(20):
        assert tl_nudge.maybe_nudge(conn, "off") is None
