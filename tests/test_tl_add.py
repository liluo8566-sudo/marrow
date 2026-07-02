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

def test_tl_add_writes_event_and_affect_with_fk(conn):
    r = _add(conn)
    ev = conn.execute(
        "SELECT channel, content, ts_start, ts_end FROM events WHERE id=?",
        (r["event_id"],)).fetchone()
    assert ev["channel"] == "self"
    assert ev["content"] == "body orig"
    assert ev["ts_start"] and ev["ts_end"]
    af = conn.execute(
        "SELECT event_id, valence, arousal, importance, label FROM affect"
        " WHERE event_id=?", (r["event_id"],)).fetchone()
    assert af["event_id"] == r["event_id"]
    # primary word = n_word 愉悦 -> 0.80/0.60 from seed map
    assert af["valence"] == pytest.approx(0.80)
    assert af["arousal"] == pytest.approx(0.60)
    assert af["importance"] == 2  # default
    assert af["label"] == "N 愉悦·3 | Y 委屈·2"


def test_explicit_va_overrides_map(conn):
    r = _add(conn, valence=0.11, arousal=0.99)
    af = conn.execute("SELECT valence, arousal FROM affect WHERE event_id=?",
                      (r["event_id"],)).fetchone()
    assert af["valence"] == pytest.approx(0.11)
    assert af["arousal"] == pytest.approx(0.99)


def test_unknown_word_without_override_errors(conn):
    with pytest.raises(tl_writer.TlError) as e:
        tl_writer.tl_add(conn, _hhmm(1), "body", n_word="魑魅", sid="s")
    assert "not in affect map" in str(e.value)


def test_validation_word_and_body_limits(conn):
    with pytest.raises(tl_writer.TlError):
        tl_writer.tl_add(conn, _hhmm(1), "b", n_word="1234567", sid="s")
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
    assert conn.execute("SELECT content FROM events WHERE id=?",
                        (eid,)).fetchone()["content"] == "body edited"
    assert conn.execute("SELECT label FROM affect WHERE event_id=?",
                        (eid,)).fetchone()["label"] == "N 温柔·4 | Y 委屈·2"


def test_self_delete_cascades_affect(conn, tmp_path):
    r = _add(conn)
    eid = r["event_id"]
    dash = tmp_path / "dashboard.md"
    _write_dash(dash, f"## Timeline\n_none_\n<!-- tl-rendered:e={eid} -->\n")
    rpt = reconcile.reconcile_timeline(conn, dash)
    assert rpt.updated == 1
    assert conn.execute("SELECT COUNT(*) c FROM events WHERE id=?",
                        (eid,)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM affect WHERE event_id=?",
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

def test_tl_update_changes_body_and_affect(conn):
    r = _add(conn, body="orig")
    tl_writer.tl_update(conn, r["event_id"], body="updated",
                        n_word="温柔", n_intensity=5, y_word="委屈", y_intensity=1)
    ev = conn.execute("SELECT content FROM events WHERE id=?",
                      (r["event_id"],)).fetchone()
    af = conn.execute("SELECT label, valence FROM affect WHERE event_id=?",
                      (r["event_id"],)).fetchone()
    assert ev["content"] == "updated"
    assert af["label"] == "N 温柔·5 | Y 委屈·1"
    assert af["valence"] == pytest.approx(0.85)  # 温柔


def test_tl_update_rejects_non_self(conn):
    conn.execute("INSERT INTO events (session_id, timestamp, role, content,"
                 " channel) VALUES ('m', '2026-07-03T00:00:00Z', 'user', 'x',"
                 " 'manual')")
    conn.commit()
    eid = conn.execute("SELECT id FROM events WHERE channel='manual'").fetchone()["id"]
    with pytest.raises(tl_writer.TlError):
        tl_writer.tl_update(conn, eid, body="y")


# ── nudge ────────────────────────────────────────────────────────────────────

def test_nudge_off_by_default(conn):
    assert tl_nudge.enabled() is False
    assert tl_nudge.maybe_nudge(conn, "sess-1") is None
    assert tl_nudge.should_nudge(999) is False  # gated by enabled flag
