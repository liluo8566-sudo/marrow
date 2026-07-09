"""Tests for marrow/day_para.py. LLM faked — prompt quality not under test.

Covers: tl-row local-date bounding (Melbourne date edge), prompt-file override
via config, PARA/TONE marker parse (missing TONE tolerated, missing PARA →
no write), UPDATE path preserves diary.content, INSERT-stub path,
skip-when-overview-exists vs --force, calendar failure degrades to "",
diary loader fills style/length vars.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from marrow import config, day_para, storage
from marrow.llm import LLMError


class FakeLLM:
    def __init__(self, prose="PARA: 一段日概要。\nTONE: 温暖",
                 raise_on_call=False):
        self.prose = prose
        self.raise_on_call = raise_on_call
        self.calls: list[str] = []

    def call(self, role, body, *, tier="cheap"):
        self.calls.append(role)
        if self.raise_on_call:
            raise LLMError("fake failure")
        return self.prose

    def n(self, role):
        return self.calls.count(role)


def _tl(conn, ts_start, content, ts_end=None, sid="s1"):
    conn.execute(
        "INSERT INTO events(session_id,timestamp,role,content,ts_start,ts_end)"
        " VALUES(?,?,?,?,?,?)",
        (sid, ts_start, "tl", content, ts_start, ts_end))


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    yield p, conn
    conn.close()


# ── tl-row local-date bounding ───────────────────────────────────────────────

def test_read_tl_lines_melbourne_date_edge(db, monkeypatch):
    """Row at 00:30 local lands in the day; 23:30 prev-local excluded."""
    p, conn = db
    monkeypatch.setattr(config, "get_tz",
                        lambda: ZoneInfo("Australia/Melbourne"))
    # July → AEST (UTC+10, no DST).
    _tl(conn, "2026-07-03T14:30:00Z", "【温暖】刚过零点 [3]")   # 00:30 07-04
    _tl(conn, "2026-07-03T13:30:00Z", "【平淡】前一天深夜 [2]")  # 23:30 07-03
    conn.commit()
    lines = day_para._read_tl_lines(conn, "2026-07-04")
    assert len(lines) == 1
    assert "刚过零点" in lines[0]
    assert lines[0].startswith("00:30 ")
    assert "前一天深夜" not in "\n".join(lines)


def test_read_tl_lines_range_and_order(db, monkeypatch):
    p, conn = db
    monkeypatch.setattr(config, "get_tz",
                        lambda: ZoneInfo("Australia/Melbourne"))
    _tl(conn, "2026-07-04T02:00:00Z", "【晚】later [2]")   # 12:00
    _tl(conn, "2026-07-03T22:00:00Z", "【早】early [3]",
        ts_end="2026-07-03T23:00:00Z")                     # 08:00-09:00
    conn.commit()
    lines = day_para._read_tl_lines(conn, "2026-07-04")
    assert lines[0].startswith("08:00-09:00 ")   # ordered by start
    assert lines[1].startswith("12:00 ")


# ── prompt-file override ─────────────────────────────────────────────────────

def test_render_day_para_prompt_override(tmp_path):
    f = tmp_path / "custom.txt"
    f.write_text("OVERRIDE {user_name} {chars_min}-{chars_max} {date}",
                 encoding="utf-8")
    cfg = {"day_para": {"prompt_file": str(f),
                        "chars_min": 100, "chars_max": 150}}
    out = day_para.render_day_para_prompt(cfg)
    assert out.startswith("OVERRIDE ")
    assert "100-150" in out
    assert "{date}" in out            # runtime slot preserved
    assert "{chars_min}" not in out


def test_render_day_para_prompt_packaged_default():
    out = day_para.render_day_para_prompt()
    assert "{user_name}" not in out
    assert "{chars_min}" not in out
    # runtime slots preserved for write_day_para
    assert "{date}" in out and "{timeline}" in out and "{calendar}" in out


# ── marker parse ─────────────────────────────────────────────────────────────

def test_parse_para_tone_both():
    para, tone = day_para._parse_para_tone("PARA: 正文内容。\nTONE: 温暖")
    assert para == "正文内容。"
    assert tone == "温暖"


def test_parse_para_tone_fullwidth_colon():
    para, tone = day_para._parse_para_tone("PARA：正文\nTONE：愉悦")
    assert para == "正文"
    assert tone == "愉悦"


def test_parse_para_tone_missing_tone():
    para, tone = day_para._parse_para_tone("PARA: 只有正文")
    assert para == "只有正文"
    assert tone is None


def test_parse_para_tone_missing_para():
    para, tone = day_para._parse_para_tone("TONE: 平淡")
    assert para is None
    assert tone == "平淡"


# ── write_day_para paths ─────────────────────────────────────────────────────

def _seed_one_tl(conn):
    # 2026-05-16T02:00Z → Shanghai default tz 10:00, date 05-16.
    _tl(conn, "2026-05-16T02:00:00Z", "【温暖】写代码 [3]")
    conn.commit()


def test_write_no_tl_rows_returns_false(db, monkeypatch):
    p, conn = db
    monkeypatch.setattr(day_para, "_read_calendar", lambda *a, **k: "")
    assert day_para.write_day_para(conn, "2026-05-16", FakeLLM(), db=p) is False
    row = conn.execute(
        "SELECT 1 FROM diary WHERE date='2026-05-16'").fetchone()
    assert row is None


def test_write_insert_stub_path(db, monkeypatch):
    p, conn = db
    monkeypatch.setattr(day_para, "_read_calendar", lambda *a, **k: "")
    _seed_one_tl(conn)
    f = FakeLLM(prose="PARA: 上午写代码，傍晚散步。\nTONE: 温暖")
    assert day_para.write_day_para(conn, "2026-05-16", f, db=p) is True
    assert f.n("day_para") == 1
    row = conn.execute(
        "SELECT content, overview, tone, session_ids FROM diary"
        " WHERE date='2026-05-16'").fetchone()
    assert row["content"] == "—"
    assert row["overview"] == "上午写代码，傍晚散步。"
    assert row["tone"] == "温暖"
    assert row["session_ids"] == ""
    audit = conn.execute(
        "SELECT action FROM audit_log WHERE action='day_para'"
        " AND target_id='2026-05-16'").fetchone()
    assert audit is not None


def test_write_update_preserves_content(db, monkeypatch):
    p, conn = db
    monkeypatch.setattr(day_para, "_read_calendar", lambda *a, **k: "")
    _seed_one_tl(conn)
    conn.execute(
        "INSERT INTO diary (date, content, overview) VALUES (?, ?, ?)",
        ("2026-05-16", "原始日记正文", None))
    conn.commit()
    f = FakeLLM(prose="PARA: 新的日概要。\nTONE: 愉悦")
    assert day_para.write_day_para(conn, "2026-05-16", f, db=p) is True
    row = conn.execute(
        "SELECT content, overview, tone FROM diary"
        " WHERE date='2026-05-16'").fetchone()
    assert row["content"] == "原始日记正文"   # untouched
    assert row["overview"] == "新的日概要。"
    assert row["tone"] == "愉悦"


def test_write_skip_when_overview_exists(db, monkeypatch):
    p, conn = db
    monkeypatch.setattr(day_para, "_read_calendar", lambda *a, **k: "")
    _seed_one_tl(conn)
    conn.execute(
        "INSERT INTO diary (date, content, overview) VALUES (?, ?, ?)",
        ("2026-05-16", "正文", "已有概要"))
    conn.commit()
    f = FakeLLM(prose="PARA: 不该写入。\nTONE: x")
    assert day_para.write_day_para(conn, "2026-05-16", f, db=p) is False
    assert f.calls == []   # skipped before LLM call
    row = conn.execute(
        "SELECT overview FROM diary WHERE date='2026-05-16'").fetchone()
    assert row["overview"] == "已有概要"


def test_write_force_overwrites_overview(db, monkeypatch):
    p, conn = db
    monkeypatch.setattr(day_para, "_read_calendar", lambda *a, **k: "")
    _seed_one_tl(conn)
    conn.execute(
        "INSERT INTO diary (date, content, overview) VALUES (?, ?, ?)",
        ("2026-05-16", "正文", "旧概要"))
    conn.commit()
    f = FakeLLM(prose="PARA: 覆盖后的概要。\nTONE: 温暖")
    assert day_para.write_day_para(
        conn, "2026-05-16", f, db=p, force=True) is True
    row = conn.execute(
        "SELECT overview FROM diary WHERE date='2026-05-16'").fetchone()
    assert row["overview"] == "覆盖后的概要。"


def test_write_missing_para_no_write_and_alerts(db, monkeypatch):
    p, conn = db
    monkeypatch.setattr(day_para, "_read_calendar", lambda *a, **k: "")
    _seed_one_tl(conn)
    f = FakeLLM(prose="TONE: 只有情绪没有正文")
    assert day_para.write_day_para(conn, "2026-05-16", f, db=p) is False
    assert conn.execute(
        "SELECT 1 FROM diary WHERE date='2026-05-16'").fetchone() is None
    al = conn.execute(
        "SELECT severity, message FROM alerts WHERE type='routine'").fetchone()
    assert al and al["severity"] == "warn" and "PARA" in al["message"]


def test_write_llm_failure_alerts(db, monkeypatch):
    p, conn = db
    monkeypatch.setattr(day_para, "_read_calendar", lambda *a, **k: "")
    _seed_one_tl(conn)
    f = FakeLLM(raise_on_call=True)
    assert day_para.write_day_para(conn, "2026-05-16", f, db=p) is False
    al = conn.execute(
        "SELECT severity, message FROM alerts WHERE type='routine'").fetchone()
    assert al and al["severity"] == "warn" and "failed" in al["message"]


# ── calendar degrade ─────────────────────────────────────────────────────────

def test_read_calendar_disabled_returns_empty():
    cfg = {"day_para": {"include_calendar": False}}
    assert day_para._read_calendar("2026-05-16", cfg) == ""


def test_read_calendar_missing_binary_returns_empty(monkeypatch):
    monkeypatch.setattr(day_para.schedule, "_cadence_bin",
                        lambda: "/nonexistent/cadence")
    cfg = {"day_para": {"include_calendar": True}}
    assert day_para._read_calendar("2026-05-16", cfg) == ""


# ── diary prompt loader ──────────────────────────────────────────────────────

def test_load_diary_prompt_fills_style_and_length():
    cfg = {"diary": {"length_range": "500-900",
                     "style": "轻快幽默"}}
    out = day_para.load_diary_prompt(cfg)
    assert "500-900" in out
    assert "轻快幽默" in out
    assert "{length_range}" not in out and "{style}" not in out
    # runtime slots preserved
    assert "{date}" in out and "{digest}" in out


def test_load_diary_prompt_packaged_default():
    out = day_para.load_diary_prompt()
    assert "{user_name}" not in out and "{assistant_name}" not in out
    assert "{date}" in out and "{digest}" in out
