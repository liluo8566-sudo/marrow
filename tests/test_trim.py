"""Tests for marrow/trim.py — timeline trim (C4-rest, Decided 07-04).

Covers:
- Same-label adjacent rows merge; different labels never merge
- Gap > merge_gap_min doesn't merge; span cap breaks groups mid-run
- Rows newer than min_age_hours untouched
- Flagged rows skipped (never merged)
- Body concat order + dedup; imp = max; ts_start/ts_end span the group
- Deleted rows gone, kept row updated (earliest row kept)
- dry_run leaves DB unchanged; idempotent second run
- Disabled config returns empty report
"""
from __future__ import annotations

import datetime as _dt

import pytest

from marrow import config, storage
from marrow.trim import trim_timeline

_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
# Comfortably older than the 48h default min_age_hours regardless of small
# per-test minute offsets (used below up to ~200min).
_BASE_MIN_AGO = 4000


@pytest.fixture()
def conn(tmp_path):
    db = str(tmp_path / "trim.db")
    c = storage.init_db(db)
    yield c
    c.close()


def _ts(offset_min: float = 0) -> str:
    """UTC timestamp `offset_min` minutes forward from a fixed old base point."""
    now = _dt.datetime.now(_dt.timezone.utc)
    dt = now - _dt.timedelta(minutes=_BASE_MIN_AGO - offset_min)
    return dt.strftime(_UTC_FMT)


def _recent_ts(hours_ago: float = 1.0) -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return (now - _dt.timedelta(hours=hours_ago)).strftime(_UTC_FMT)


def _tl(conn, content: str, ts: str, imp: int | None = None,
        flag: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel,"
        " imp, flag) VALUES ('cli:test', ?, 'tl', ?, 'cli', ?, ?)",
        (ts, content, imp, flag),
    )
    conn.commit()
    return cur.lastrowid


def _row(conn, eid: int) -> dict:
    return dict(conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())


def _all_rows(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]


# ── basic merge ──────────────────────────────────────────────────────────────

def test_merge_same_label_adjacent_rows(conn):
    r1 = _tl(conn, "【平淡】起床", _ts(0))
    r2 = _tl(conn, "【平淡】吃早饭", _ts(10))
    r3 = _tl(conn, "【平淡】出门", _ts(20))

    rpt = trim_timeline(conn)
    assert rpt["merged"] == 1
    assert rpt["deleted"] == 2

    kept = _row(conn, r1)
    assert kept["content"] == "【平淡】起床；吃早饭；出门"
    assert conn.execute("SELECT id FROM events WHERE id IN (?,?)", (r2, r3)).fetchall() == []


def test_different_labels_never_merge(conn):
    r1 = _tl(conn, "【平淡】事件甲", _ts(0))
    r2 = _tl(conn, "【兴奋】事件乙", _ts(10))

    rpt = trim_timeline(conn)
    assert rpt["merged"] == 0
    assert rpt["deleted"] == 0
    assert _row(conn, r1)["content"] == "【平淡】事件甲"
    assert _row(conn, r2)["content"] == "【兴奋】事件乙"


def test_gap_over_max_does_not_merge(conn):
    r1 = _tl(conn, "【平淡】事件甲", _ts(0))
    r2 = _tl(conn, "【平淡】事件乙", _ts(50))  # gap 50min > merge_gap_min 45

    rpt = trim_timeline(conn)
    assert rpt["merged"] == 0
    assert _row(conn, r1)["content"] == "【平淡】事件甲"
    assert _row(conn, r2)["content"] == "【平淡】事件乙"


def test_span_cap_breaks_group(conn):
    # gap 40min each (<=45 ok); cumulative span from r1 crosses 120min at r5.
    r1 = _tl(conn, "【平淡】A", _ts(0))
    r2 = _tl(conn, "【平淡】B", _ts(40))
    r3 = _tl(conn, "【平淡】C", _ts(80))
    r4 = _tl(conn, "【平淡】D", _ts(120))   # span r1->r4 == 120, still <= cap
    r5 = _tl(conn, "【平淡】E", _ts(160))   # span r1->r5 == 160 > cap, breaks

    rpt = trim_timeline(conn)
    assert rpt["merged"] == 1
    assert rpt["deleted"] == 3
    kept = _row(conn, r1)
    assert kept["content"] == "【平淡】A；B；C；D"
    assert _row(conn, r5)["content"] == "【平淡】E"  # untouched, not merged


def test_rows_newer_than_min_age_untouched(conn):
    r1 = _tl(conn, "【平淡】刚发生的事", _recent_ts(1))
    r2 = _tl(conn, "【平淡】紧接着的事", _recent_ts(0.8))

    rpt = trim_timeline(conn)
    assert rpt["merged"] == 0
    assert _row(conn, r1)["content"] == "【平淡】刚发生的事"
    assert _row(conn, r2)["content"] == "【平淡】紧接着的事"


def test_flagged_rows_skipped(conn):
    r1 = _tl(conn, "【平淡】甲", _ts(0), flag="retired")
    r2 = _tl(conn, "【平淡】乙", _ts(10), flag="unresolved")

    rpt = trim_timeline(conn)
    assert rpt["merged"] == 0
    assert _row(conn, r1)["content"] == "【平淡】甲"
    assert _row(conn, r2)["content"] == "【平淡】乙"


def test_body_concat_order_and_dedup(conn):
    r1 = _tl(conn, "【平淡】内容甲", _ts(0))
    r2 = _tl(conn, "【平淡】内容乙", _ts(10))
    r3 = _tl(conn, "【平淡】内容甲", _ts(20))  # duplicate of r1's body

    trim_timeline(conn)
    kept = _row(conn, r1)
    assert kept["content"] == "【平淡】内容甲；内容乙"


def test_imp_is_max_of_group(conn):
    r1 = _tl(conn, "【平淡】甲", _ts(0), imp=1)
    r2 = _tl(conn, "【平淡】乙", _ts(10), imp=3)
    r3 = _tl(conn, "【平淡】丙", _ts(20), imp=2)

    trim_timeline(conn)
    assert _row(conn, r1)["imp"] == 3


def test_ts_start_ts_end_span_group(conn):
    ts1, ts2, ts3 = _ts(0), _ts(10), _ts(20)
    r1 = _tl(conn, "【平淡】甲", ts1)
    _tl(conn, "【平淡】乙", ts2)
    _tl(conn, "【平淡】丙", ts3)

    trim_timeline(conn)
    kept = _row(conn, r1)
    assert kept["ts_start"] == ts1
    assert kept["ts_end"] == ts3


def test_deleted_rows_gone_kept_row_updated(conn):
    r1 = _tl(conn, "【平淡】甲", _ts(0))
    r2 = _tl(conn, "【平淡】乙", _ts(10))

    before_count = len(_all_rows(conn))
    trim_timeline(conn)
    after = _all_rows(conn)
    assert len(after) == before_count - 1
    assert conn.execute("SELECT id FROM events WHERE id=?", (r2,)).fetchone() is None
    assert _row(conn, r1)["content"] == "【平淡】甲；乙"


# ── dry_run / idempotence ────────────────────────────────────────────────────

def test_dry_run_leaves_db_unchanged(conn):
    r1 = _tl(conn, "【平淡】甲", _ts(0))
    r2 = _tl(conn, "【平淡】乙", _ts(10))
    before = _all_rows(conn)

    rpt = trim_timeline(conn, dry_run=True)
    assert rpt["merged"] == 1
    assert rpt["dry_run"] is True

    after = _all_rows(conn)
    assert after == before  # nothing written
    assert _row(conn, r1)["content"] == "【平淡】甲"
    assert _row(conn, r2)["content"] == "【平淡】乙"


def test_dry_run_still_journals(conn):
    _tl(conn, "【平淡】甲", _ts(0))
    _tl(conn, "【平淡】乙", _ts(10))
    trim_timeline(conn, dry_run=True)

    log_path = config.ensure_data_dir() / "logs" / "trim.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    import json
    last = json.loads(lines[-1])
    assert last["dry_run"] is True


def test_idempotent_second_run_merges_nothing_new(conn):
    _tl(conn, "【平淡】甲", _ts(0))
    _tl(conn, "【平淡】乙", _ts(10))
    _tl(conn, "【平淡】丙", _ts(20))

    first = trim_timeline(conn)
    assert first["merged"] == 1

    second = trim_timeline(conn)
    assert second["merged"] == 0
    assert second["deleted"] == 0


def test_disabled_config_returns_empty_report(conn, monkeypatch):
    _tl(conn, "【平淡】甲", _ts(0))
    _tl(conn, "【平淡】乙", _ts(10))

    monkeypatch.setattr(config, "load", lambda: {"trim": {"enabled": False}})
    rpt = trim_timeline(conn)
    assert rpt == {"groups": [], "merged": 0, "deleted": 0}
    # untouched
    assert len(_all_rows(conn)) == 2
