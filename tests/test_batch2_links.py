"""Tests for affect event_hint linking + recall_count bump (Batch 2)."""
from __future__ import annotations

import pytest

from marrow import recall, sessionend_writers, storage


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    yield conn, p
    conn.close()


def _ins_event(conn, content, *, sid="sid-1"):
    cur = conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content)"
        " VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'user', ?)",
        (sid, content),
    )
    return cur.lastrowid


def _affect_raw(hint, *, description=None):
    desc = f' "description": "{description}",' if description else ""
    return (
        "===AFFECT===\n"
        '[{"ep": 1, "valence": 0.7, "arousal": 0.4, "importance": 3,'
        f' "label": "测试",{desc} "entities": [],'
        f' "event_hint": "{hint}", "unresolved": 0,'
        ' "reconcile_prev": "N/A"}]\n'
        "===END===\n"
    )


class TestMatchEventHint:
    def test_single_match_returns_id(self, db):
        conn, _ = db
        eid = _ins_event(conn, "今天拿到了解剖学的HD成绩超开心")
        _ins_event(conn, "完全无关的另一句话")
        got = sessionend_writers._match_event_hint(conn, "解剖学的HD成绩", "sid-1")
        assert got == eid

    def test_ambiguous_returns_none(self, db):
        conn, _ = db
        _ins_event(conn, "复习GAMSAT到深夜")
        _ins_event(conn, "又是复习GAMSAT到深夜的一天")
        assert sessionend_writers._match_event_hint(
            conn, "复习GAMSAT", "sid-1"
        ) is None

    def test_no_match_returns_none(self, db):
        conn, _ = db
        _ins_event(conn, "聊了今天的晚饭")
        assert sessionend_writers._match_event_hint(
            conn, "完全不存在的短语", "sid-1"
        ) is None

    def test_other_session_not_matched(self, db):
        conn, _ = db
        _ins_event(conn, "独一无二的里程碑时刻", sid="other-sid")
        assert sessionend_writers._match_event_hint(
            conn, "独一无二的里程碑", "sid-1"
        ) is None

    def test_empty_hint_returns_none(self, db):
        conn, _ = db
        assert sessionend_writers._match_event_hint(conn, "", "sid-1") is None
        assert sessionend_writers._match_event_hint(conn, None, "sid-1") is None


class TestSegAffectLink:
    def test_hint_match_sets_event_id_and_audit(self, db):
        conn, _ = db
        eid = _ins_event(conn, "考完了生理学期末感觉不错")
        n = sessionend_writers.seg_affect(
            conn, _affect_raw("生理学期末"), "sid-1", "2026-06-01"
        )
        assert n == 1
        row = conn.execute(
            "SELECT event_id FROM affect WHERE date='2026-06-01'"
        ).fetchone()
        assert row["event_id"] == eid
        audit = conn.execute(
            "SELECT 1 FROM audit_log WHERE target_table='affect'"
            " AND action='event_link'"
        ).fetchone()
        assert audit

    def test_no_match_leaves_null(self, db):
        conn, _ = db
        _ins_event(conn, "毫无关联的内容")
        sessionend_writers.seg_affect(
            conn, _affect_raw("不存在的事件提示"), "sid-1", "2026-06-02"
        )
        row = conn.execute(
            "SELECT event_id FROM affect WHERE date='2026-06-02'"
        ).fetchone()
        assert row["event_id"] is None

    def test_description_fallback_when_hint_empty(self, db):
        conn, _ = db
        eid = _ins_event(conn, "深夜聊了未来的规划觉得很安心")
        sessionend_writers.seg_affect(
            conn, _affect_raw("", description="未来的规划"),
            "sid-1", "2026-06-03",
        )
        row = conn.execute(
            "SELECT event_id FROM affect WHERE date='2026-06-03'"
        ).fetchone()
        assert row["event_id"] == eid


class TestBumpRecallCounts:
    def test_bump_increments_and_stamps(self, db):
        conn, p = db
        eid = _ins_event(conn, "被召回的记忆")
        conn.commit()
        recall.bump_recall_counts([eid], db=p)
        row = conn.execute(
            "SELECT recall_count, last_recalled_at FROM events WHERE id=?",
            (eid,),
        ).fetchone()
        assert row["recall_count"] == 1
        assert row["last_recalled_at"] and row["last_recalled_at"].endswith("Z")
        recall.bump_recall_counts([eid], db=p)
        row = conn.execute(
            "SELECT recall_count FROM events WHERE id=?", (eid,)
        ).fetchone()
        assert row["recall_count"] == 2

    def test_empty_list_noop(self, db):
        _, p = db
        recall.bump_recall_counts([], db=p)  # must not raise

    def test_failure_never_raises(self):
        recall.bump_recall_counts([1], db="/nonexistent/dir/nope.db")
