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
        n_word="愉悦", n_intensity=3, y_word="委屈", y_intensity=2,
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
    assert ev["content"] == "【N 愉悦·3 | Y 委屈·2】body orig"
    assert ev["imp"] == 3  # default = max(n_int, y_int)
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
        tl_writer.tl_add(conn, _hhmm(1), "x" * 31, n_word="愉悦", sid="s")
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
    assert f"【N 愉悦·3 | Y 委屈·2】翻日志扑空 <!-- tl:e:{r['event_id']} -->" in md
    assert f"e={r['event_id']}" in md  # trail marker


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
                        .replace("愉悦·3", "温柔·4"))
    rpt = reconcile.reconcile_timeline(conn, dash)
    assert rpt.updated == 1
    # label + body both live inside content now
    assert conn.execute("SELECT content FROM events WHERE id=?",
                        (eid,)).fetchone()["content"] == \
        "【N 温柔·4 | Y 委屈·2】body edited"


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
                        n_word="温柔", n_intensity=5, y_word="委屈", y_intensity=1,
                        importance=4)
    ev = conn.execute("SELECT content, imp FROM events WHERE id=?",
                      (r["event_id"],)).fetchone()
    assert ev["content"] == "【N 温柔·5 | Y 委屈·1】updated"
    assert ev["imp"] == 4


def test_tl_update_body_only_keeps_label(conn):
    r = _add(conn, body="orig")
    tl_writer.tl_update(conn, r["event_id"], body="just body")
    ev = conn.execute("SELECT content FROM events WHERE id=?",
                      (r["event_id"],)).fetchone()
    assert ev["content"] == "【N 愉悦·3 | Y 委屈·2】just body"


def test_tl_update_rejects_non_tl(conn):
    conn.execute("INSERT INTO events (session_id, timestamp, role, content,"
                 " channel) VALUES ('m', '2026-07-03T00:00:00Z', 'user', 'x',"
                 " 'manual')")
    conn.commit()
    eid = conn.execute("SELECT id FROM events WHERE channel='manual'").fetchone()["id"]
    with pytest.raises(tl_writer.TlError):
        tl_writer.tl_update(conn, eid, body="y")


# ── C3: MARROW_CORTEX guard ───────────────────────────────────────────────────

def test_tl_add_blocked_under_marrow_cortex(conn, monkeypatch):
    monkeypatch.setenv("MARROW_CORTEX", "1")
    with pytest.raises(tl_writer.TlError, match="cortex"):
        _add(conn)
    n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    assert n == 0


def test_tl_update_blocked_under_marrow_cortex(conn, monkeypatch):
    r = _add(conn)
    monkeypatch.setenv("MARROW_CORTEX", "1")
    with pytest.raises(tl_writer.TlError, match="cortex"):
        tl_writer.tl_update(conn, r["event_id"], body="should not land")
    ev = conn.execute("SELECT content FROM events WHERE id=?",
                      (r["event_id"],)).fetchone()
    assert ev["content"] == "【N 愉悦·3 | Y 委屈·2】body orig"


# ── nudge ────────────────────────────────────────────────────────────────────

def test_nudge_on_by_default_10_turns(conn):
    assert tl_nudge.enabled() is True
    assert tl_nudge.threshold() == 10
    # 10 assistant turns, no tl_add -> nudge fires
    for _ in range(10):
        conn.execute("INSERT INTO events (session_id, timestamp, role, content)"
                     " VALUES ('nud', '2026-07-03T00:00:00Z', 'assistant', 'x')")
    conn.commit()
    assert tl_nudge.maybe_nudge(conn, "nud")


def test_nudge_silent_session_muted(conn):
    for _ in range(10):
        conn.execute("INSERT INTO events (session_id, timestamp, role, content)"
                     " VALUES ('mute', '2026-07-03T00:00:00Z', 'assistant', 'x')")
    conn.commit()
    tl_nudge.set_silent("mute")
    assert tl_nudge.is_silent("mute") is True
    assert tl_nudge.maybe_nudge(conn, "mute") is None
