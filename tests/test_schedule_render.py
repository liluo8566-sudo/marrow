"""Schedule render + diff + rollover units (P1/P2)."""
from __future__ import annotations

import json

import pytest

from marrow import schedule


TODAY = "2026-07-10"


def _rem(**kw):
    base = {"completed": False, "flagged": False, "priority": 0, "list": "Inbox"}
    base.update(kw)
    return base


def _cal(**kw):
    base = {"all_day": False, "calendar": "Routine"}
    base.update(kw)
    return base


# --- (a) render ordering + time display + done section ---------------------

def test_render_ordering_and_times():
    rems = [
        _rem(id=1, list="Learning", title="MH Latte",
             due_date="2026-07-06T00:00:00+10:00"),                 # overdue
        _rem(id=2, list="Appointment", title="GP", flagged=True,
             due_date="2026-07-10T08:30:00+10:00"),                 # timed
        _rem(id=3, list="Chore", title="周清洁",
             due_date="2026-07-10T00:00:00+10:00"),                 # untimed today
    ]
    done = [
        _rem(id=4, list="Financial", title="Transfer", completed=True,
             completion_date="2026-07-10T09:00:00+10:00"),
    ]
    lines = schedule._render_reminders(json.dumps(rems), json.dumps(done), TODAY)
    joined = "\n".join(lines)

    # order: overdue, timed, untimed, done; id trails after glyphs
    assert lines[0] == "- [Learning] MH Latte [Overdue] [1]"
    assert lines[1] == "- [Appointment] 08:30 GP 🚩 [2]"
    assert lines[2] == "- [Chore] 周清洁 [3]"
    assert lines[3] == "- [Financial] Transfer [Done 09:00] [4]"
    # timed shows due time; untimed does not
    assert "08:30" in lines[1]
    assert "🚩" in lines[1]


def test_done_only_today():
    done = [
        _rem(id=5, list="Chore", title="old", completed=True,
             completion_date="2026-07-01T20:00:00+10:00"),
        _rem(id=6, list="Chore", title="new", completed=True,
             completion_date="2026-07-10T20:00:00+10:00"),
    ]
    lines = schedule._render_reminders("[]", json.dumps(done), TODAY)
    assert lines == ["- [Chore] new [Done 20:00] [6]"]


def test_calendar_render_with_times():
    events = [
        _cal(calendar="GAMSAT", title="Deep Study",
             start="2026-07-10T06:15:00+10:00", end="2026-07-10T08:15:00+10:00"),
        _cal(calendar="Routine", title="Wake up",
             start="2026-07-10T05:30:00+10:00", end="2026-07-10T06:15:00+10:00"),
        _cal(calendar="Scheduled Reminders", title="dup", all_day=True,
             start="2026-07-10T10:00:00+10:00", end="2026-07-11T09:59:59+10:00"),
    ]
    lines = schedule._render_calendar(json.dumps(events), TODAY)
    # sorted by start; Scheduled Reminders all-day skipped
    assert lines == [
        "- [Routine] 05:30-06:15 Wake up",
        "- [GAMSAT] 06:15-08:15 Deep Study",
    ]


def test_calendar_exclude_drops_matching_calendar(monkeypatch):
    monkeypatch.setattr(schedule, "_cal_exclude", lambda: {"STAR"})
    monkeypatch.setattr(schedule, "_cal_keep_re", lambda: None)
    events = [
        _cal(calendar="STAR", title="Science And Society, Sem01",
             start="2026-07-10T08:00:00+10:00", end="2026-07-10T09:50:00+10:00"),
        _cal(calendar="Routine", title="Wake up",
             start="2026-07-10T05:30:00+10:00", end="2026-07-10T06:15:00+10:00"),
    ]
    lines = schedule._render_calendar(json.dumps(events), TODAY)
    assert lines == ["- [Routine] 05:30-06:15 Wake up"]


def test_calendar_keep_regex_rescues_matching_title(monkeypatch):
    monkeypatch.setattr(schedule, "_cal_exclude", lambda: {"STAR"})
    monkeypatch.setattr(schedule, "_cal_keep_re", lambda: __import__("re").compile("(?i)lab|pra"))
    events = [
        _cal(calendar="STAR", title="Science And Society, Sem01",
             start="2026-07-10T08:00:00+10:00", end="2026-07-10T09:50:00+10:00"),
        _cal(calendar="STAR", title="Anatomy, Lab01",
             start="2026-07-10T10:00:00+10:00", end="2026-07-10T12:00:00+10:00"),
    ]
    lines = schedule._render_calendar(json.dumps(events), TODAY)
    assert lines == ["- [STAR] 10:00-12:00 Anatomy, Lab01"]


def test_calendar_exclude_empty_is_no_change(monkeypatch):
    monkeypatch.setattr(schedule, "_cal_exclude", lambda: set())
    monkeypatch.setattr(schedule, "_cal_keep_re", lambda: None)
    events = [
        _cal(calendar="STAR", title="Science And Society, Sem01",
             start="2026-07-10T08:00:00+10:00", end="2026-07-10T09:50:00+10:00"),
    ]
    lines = schedule._render_calendar(json.dumps(events), TODAY)
    assert lines == ["- [STAR] 08:00-09:50 Science And Society, Sem01"]


def test_cal_keep_re_invalid_pattern_is_safe(monkeypatch):
    monkeypatch.setattr(schedule, "_schedule_cfg", lambda: {"cal_keep": "(unclosed"})
    assert schedule._cal_keep_re() is None


def test_priority_glyphs():
    """Priority 1 = High (RFC 5545) gets ⚡; Medium (5) and Low (9) get none —
    🚩 flagged is the primary highlight, medium/low glyphs are noise."""
    rems = [
        _rem(id=7, list="X", title="crit", priority=1,
             due_date="2026-07-10T00:00:00+10:00"),
        _rem(id=8, list="X", title="med", priority=5,
             due_date="2026-07-10T00:00:00+10:00"),
        _rem(id=9, list="X", title="low", priority=9,
             due_date="2026-07-10T00:00:00+10:00"),
    ]
    lines = schedule._render_reminders(json.dumps(rems), "[]", TODAY)
    by_id = {"[7]": next(l for l in lines if l.endswith("[7]")),
             "[8]": next(l for l in lines if l.endswith("[8]")),
             "[9]": next(l for l in lines if l.endswith("[9]"))}
    assert "⚡" in by_id["[7]"]       # crit / High
    assert "⚡" not in by_id["[8]"] and "❗" not in by_id["[8]"]  # med
    assert "⚡" not in by_id["[9]"] and "❗" not in by_id["[9]"]  # low


# --- (b) diff classification incl. done-with-completion-time ---------------

def _content(rem_lines, cal_lines):
    body = "\n".join([
        f"## Daily Schedule  {TODAY} Fri | now 10:00",
        schedule._flag_note(),
        "\n".join(rem_lines + ["---"] + cal_lines),
    ])
    return body


def test_diff_new_changed_done():
    old = _content(
        ["- [Appointment] 08:30 GP 🚩 [2]", "- [Chore] 周清洁 [3]"],
        ["- [Routine] 05:30-06:15 Wake up"],
    )
    new = _content(
        ["- [Appointment] 09:00 GP 🚩 [2]",          # changed (time)
         "- [Chore] 周清洁 [Done 11:00] [3]",         # done
         "- [Learning] read paper [10]"],             # new
        ["- [Routine] 05:30-06:15 Wake up",
         "- [Leisure] 18:00-19:30 TV"],          # new cal
    )
    diff = schedule.compute_diff(old, new)
    assert "~[Appointment] 09:00 GP" in diff       # changed marker
    assert "✓[Chore] 周清洁 [Done 11:00]" in diff   # done marker + completion time
    assert "+[Learning] read paper" in diff        # new rem
    assert "+[Leisure] 18:00-19:30 TV" in diff      # new cal line with times


def test_diff_title_change_same_id_is_changed_not_remove_add():
    """Same id, different title → classified as changed (~), not -/+."""
    old = _content(["- [Chore] old title [3]"], [])
    new = _content(["- [Chore] new title [3]"], [])
    diff = schedule.compute_diff(old, new)
    assert "~[Chore] new title [3]" in diff
    assert "-[Chore] old title [3]" not in diff
    assert "+[Chore] new title [3]" not in diff


def test_diff_empty_when_same():
    c = _content(["- [X] a"], ["- [Y] 01:00-02:00 b"])
    assert schedule.compute_diff(c, c) == ""


# --- (c) date-rollover forces full injection -------------------------------

def test_date_rollover_forces_full(monkeypatch, tmp_path):
    monkeypatch.setattr(schedule, "_SNAPSHOT_DIR", tmp_path / "snap")
    sid = "sess-roll"

    full = _content(["- [X] a"], ["- [Y] 01:00-02:00 b"])
    monkeypatch.setattr(schedule, "is_enabled", lambda: True)
    monkeypatch.setattr(schedule, "refresh_daily", lambda *a, **k: (full, True))
    monkeypatch.setattr(schedule, "get_data_mtime", lambda: 100.0)

    # seed snapshot state as if rendered yesterday, same mtime (would early-exit)
    d = schedule._snapshot_dir()
    (d / f"{sid}.mtime").write_text("100.0")
    (d / f"{sid}.content").write_text(full)
    (d / f"{sid}.date").write_text("2026-07-09")

    import marrow.config as cfg
    from datetime import datetime, timezone

    class _Now:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(schedule, "datetime", _Now)

    out = schedule.check_and_inject(sid)
    # full content returned despite matching mtime, because date rolled
    assert out == full


def test_mtime_early_exit_same_day(monkeypatch, tmp_path):
    monkeypatch.setattr(schedule, "_SNAPSHOT_DIR", tmp_path / "snap")
    sid = "sess-nochange"
    full = _content(["- [X] a"], [])
    monkeypatch.setattr(schedule, "is_enabled", lambda: True)
    monkeypatch.setattr(schedule, "get_data_mtime", lambda: 100.0)

    d = schedule._snapshot_dir()
    (d / f"{sid}.mtime").write_text("100.0")
    (d / f"{sid}.content").write_text(full)

    from datetime import datetime, timezone

    class _Now:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(schedule, "datetime", _Now)
    (d / f"{sid}.date").write_text("2026-07-10")

    assert schedule.check_and_inject(sid) is None


def test_first_injection_returns_full(monkeypatch, tmp_path):
    monkeypatch.setattr(schedule, "_SNAPSHOT_DIR", tmp_path / "snap")
    sid = "sess-first"
    full = _content(["- [X] a"], [])
    monkeypatch.setattr(schedule, "is_enabled", lambda: True)
    monkeypatch.setattr(schedule, "refresh_daily", lambda *a, **k: (full, True))
    monkeypatch.setattr(schedule, "get_data_mtime", lambda: 100.0)
    assert schedule.check_and_inject(sid) == full


def test_disabled_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(schedule, "_SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(schedule, "is_enabled", lambda: False)
    assert schedule.check_and_inject("any") is None


# --- render_daily: fetch-failure vs legitimately-empty ---------------------

@pytest.fixture()
def fake_binary(tmp_path):
    binary = tmp_path / "cadence"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return str(binary)


def test_render_daily_all_calls_fail_returns_empty(monkeypatch, fake_binary):
    monkeypatch.setattr(schedule, "_run_cadence", lambda args, binary: "")
    assert schedule.render_daily(fake_binary) == ""


def test_render_daily_success_but_no_items_is_header_only(monkeypatch, fake_binary):
    def _fake_run(args, binary):
        if args[0] == "cal":
            return "[]"
        if "--all" in args:
            return "[]"
        return "[]"
    monkeypatch.setattr(schedule, "_run_cadence", _fake_run)
    out = schedule.render_daily(fake_binary)
    assert out != ""
    assert "## Daily Schedule" in out
    assert "(nothing scheduled today)" in out


def test_render_daily_partial_failure_still_renders_header_only(monkeypatch, fake_binary):
    """Even if cal/done fail but rem succeeds empty, one success is enough
    to distinguish from a total fetch failure."""
    def _fake_run(args, binary):
        if args[0] == "cal":
            return ""
        if "--all" in args:
            return "[]"
        return ""
    monkeypatch.setattr(schedule, "_run_cadence", _fake_run)
    out = schedule.render_daily(fake_binary)
    assert out != ""
    assert "(nothing scheduled today)" in out


def test_render_daily_with_real_items_unchanged(monkeypatch, fake_binary):
    rems = [{"id": 1, "list": "Chore", "title": "sweep", "completed": False,
             "flagged": False, "priority": 0,
             "due_date": "2026-07-10T00:00:00+10:00"}]
    events = [{"calendar": "Routine", "title": "Wake up", "all_day": False,
               "start": "2026-07-10T05:30:00+10:00",
               "end": "2026-07-10T06:15:00+10:00"}]

    def _fake_run(args, binary):
        if args[0] == "cal":
            return json.dumps(events)
        if "--all" in args:
            return json.dumps(rems)
        return "[]"
    monkeypatch.setattr(schedule, "_run_cadence", _fake_run)

    from datetime import datetime, timezone

    class _Now:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(schedule, "datetime", _Now)

    out = schedule.render_daily(fake_binary)
    assert "- [Chore] sweep" in out
    assert "- [Routine] 05:30-06:15 Wake up" in out
    assert "(nothing scheduled today)" not in out


# --- refresh_daily: stale file gets replaced by header-only render ---------

def test_refresh_daily_overwrites_stale_file_when_empty_but_fetched(
    monkeypatch, tmp_path, fake_binary
):
    daily_path = tmp_path / "daily.md"
    daily_path.write_text("## Daily Schedule  2026-07-11 Saturday | now 23:22\n"
                           "stale content from days ago")

    monkeypatch.setattr(schedule, "_run_cadence", lambda args, binary: "[]")

    content, changed = schedule.refresh_daily(fake_binary, str(daily_path))
    assert changed is True
    assert "(nothing scheduled today)" in content
    assert daily_path.read_text() == content
    assert "stale content" not in daily_path.read_text()


def test_refresh_daily_keeps_stale_file_when_all_calls_fail(
    monkeypatch, tmp_path, fake_binary
):
    daily_path = tmp_path / "daily.md"
    stale = "## Daily Schedule  2026-07-11 Saturday | now 23:22\nstale content"
    daily_path.write_text(stale)

    monkeypatch.setattr(schedule, "_run_cadence", lambda args, binary: "")

    content, changed = schedule.refresh_daily(fake_binary, str(daily_path))
    assert changed is False
    assert content == stale
    assert daily_path.read_text() == stale


# --- cadence-failure alert message classification --------------------------

def test_alert_message_authorization_denied():
    msg = schedule._alert_message("rem", "cadence: authorization denied")
    assert "authorization" in msg.lower()
    assert "Fix order:" in msg
    assert "kickstart" in msg


def test_alert_message_reminders_store():
    msg = schedule._alert_message("rem", "could not locate the Reminders store")
    assert "Fix order:" in msg
    assert "Full Disk Access" in msg


def test_alert_message_timeout():
    msg = schedule._alert_message("timeout", "")
    assert "timing out" in msg.lower()
    assert "NOT an authorization" in msg
    assert "Fix order:" not in msg


def test_alert_message_other():
    msg = schedule._alert_message("rem", "cadence: unexpected exit code 2\ntrace")
    assert "Fix order:" not in msg
    assert "cadence: unexpected exit code 2" in msg
    assert "\n" not in msg
