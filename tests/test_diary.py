"""Tests for marrow/diary.py. LLM faked — prompt quality not under test;
day-boundary, single-call pipeline, affect rows, fallback path, idempotency,
and dual triggers are.

Melbourne is UTC+10 (AEST) on these dates; diary_day(utc) = (utc+10h-4h)
= (utc+6h).date(). So UTC 18:00 rolls into the next diary day; a local
02:00 (UTC 16:00) still counts as the previous day.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from marrow import diary, storage
from marrow.llm import LLMError


class FakeLLM:
    """Fake LLM for unit tests.

    Normal path: role='diary' -> returns prose + affect JSON.
    Fallback path (over-volume or LLMError): also handles day-digest/stitch.
    Pass raise_on_diary=True to simulate LLMError on the single call.
    """
    def __init__(self, digest="digest: did X", raise_on_diary=False):
        self.digest = digest
        self.raise_on_diary = raise_on_diary
        self.calls: list[str] = []
        self.digest_bodies: list[str] = []
        self.stitch_bodies: list[str] = []
        # tracks how many diary calls so tests can vary the return value
        self._diary_call_n = 0

    def call(self, role, body, *, tier="cheap"):
        self.calls.append(role)
        if role == "day-digest":
            self.digest_bodies.append(body)
            return self.digest
        if role == "stitch":
            self.stitch_bodies.append(body)
            return "woven strand with X"
        if role == "diary":
            if self.raise_on_diary:
                raise LLMError("fake diary failure")
            self._diary_call_n += 1
            # Return a prose+affect block so affect rows are written.
            # Different digest -> different content (force-overwrite test).
            prose = f"今天我们一起把 X 做完了。[{self.digest}]"
            affect = json.dumps(
                [{"ep": 1, "valence": 0.7, "arousal": 0.5, "importance": 5,
                  "label": "温馨日常", "entities": [], "event_hint": ""}])
            return f"{prose}\n===AFFECT===\n{affect}\n===END==="
        return ""

    def n(self, role):
        return self.calls.count(role)


class FakeLLMNoAffect:
    """Single-call LLM that returns prose without any ===AFFECT=== block."""
    def call(self, role, body, *, tier="cheap"):
        if role == "diary":
            return "今天没什么特别的。"
        if role == "day-digest":
            return "digest"
        if role == "stitch":
            return "woven"
        return ""

    def n(self, role):
        return 0


def _ev(conn, sid, ts, role, content):
    conn.execute("INSERT INTO events(session_id,timestamp,role,content) "
                 "VALUES(?,?,?,?)", (sid, ts, role, content))


def _session(conn, sid, hh, n_user=4):
    # n_user user turns (+ a reply each) inside diary day 2026-05-16.
    # Default 4 clears _SKIP_DROP_MAX (3) and lands in the judge window.
    for i in range(n_user):
        _ev(conn, sid, f"2026-05-16T{hh:02d}:{i:02d}:00Z", "user", f"msg {i}")
        _ev(conn, sid, f"2026-05-16T{hh:02d}:{i:02d}:30Z", "assistant", "ok")


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    # all map to diary day 2026-05-16 (utc+6h within that date);
    # 4 user turns each -> survive the skip filter, kept
    _session(conn, "s1", 2)
    _session(conn, "s2", 9)
    conn.commit()
    return p, conn


# ── day boundary ──────────────────────────────────────────────────────────────

def test_diary_day_local_0400_cutoff():
    # UTC 18:00 -> local next-day 04:00 -> rolls to next diary day
    assert diary._diary_day("2026-05-16T17:00:00Z") == "2026-05-16"
    assert diary._diary_day("2026-05-16T18:00:00Z") == "2026-05-17"
    # local 02:00 (UTC 16:00) counts as previous day (late-night spillover)
    assert diary._diary_day("2026-05-16T16:00:00Z") == "2026-05-16"


def test_routine_target_is_just_closed_day(monkeypatch):
    fixed = dt.datetime(2026, 5, 18, 4, 30, tzinfo=diary._TZ)

    class _DT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(diary._dt, "datetime", _DT)
    assert diary._routine_target() == "2026-05-17"


# ── grouping / pending ────────────────────────────────────────────────────────

def test_day_events_only_that_diary_day(db):
    _, conn = db
    _ev(conn, "s9", "2026-05-16T18:00:00Z", "user", "next day")  # -> 05-17
    conn.commit()
    evs = diary.day_events(conn, "2026-05-16")
    assert {e["session_id"] for e in evs} == {"s1", "s2"}


def test_pending_days_excludes_written(db):
    p, conn = db
    assert diary.pending_days(conn) == ["2026-05-16"]
    conn.execute("INSERT INTO diary(date,content) VALUES('2026-05-16','x')")
    conn.commit()
    assert diary.pending_days(conn) == []


# ── single-call pipeline (Phase 2 main path) ────────────────────────────────

def test_run_day_single_call(db):
    # Phase 2: normal volume -> ONE diary call, no day-digest or stitch.
    # Both sessions are fenced in the prompt; prose+affect returned together.
    p, conn = db
    f = FakeLLM()
    assert diary.run_day(conn, "2026-05-16", f, db=p) is True
    assert f.n("day-digest") == 0          # map skipped in single-call path
    assert f.n("stitch") == 0              # stitch skipped
    assert f.n("diary") == 1              # ONE sonnet call
    row = conn.execute(
        "SELECT content,session_ids FROM diary WHERE date='2026-05-16'"
    ).fetchone()
    assert "X" in row["content"]
    assert row["session_ids"] == "s1,s2"


def test_run_day_writes_affect_rows(db):
    # affect rows written in same txn as diary row.
    p, conn = db
    f = FakeLLM()
    diary.run_day(conn, "2026-05-16", f, db=p)
    rows = conn.execute(
        "SELECT * FROM affect WHERE date='2026-05-16'"
    ).fetchall()
    assert len(rows) == 1
    r = dict(rows[0])
    assert r["ep"] == 1
    assert abs(r["valence"] - 0.7) < 0.01
    assert r["source"] == "diary_single_call"


def test_single_call_audit_log_ok_outcome(db):
    # Successful parse -> one diary_single_call_affect_ok audit row.
    p, conn = db
    diary.run_day(conn, "2026-05-16", FakeLLM(), db=p)
    rows = conn.execute(
        "SELECT action, summary FROM audit_log "
        "WHERE target_table='diary' AND target_id='2026-05-16' "
        "AND action LIKE 'diary_single_call_%'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["action"] == "diary_single_call_affect_ok"
    assert "2026-05-16" in rows[0]["summary"]


def test_single_call_audit_log_no_marker_outcome(tmp_path):
    # No ===AFFECT=== marker -> diary_single_call_no_affect_marker audit row.
    p = str(tmp_path / "tel_na.db")
    conn = storage.init_db(p)
    _session(conn, "s1", 2)
    conn.commit()
    diary.run_day(conn, "2026-05-16", FakeLLMNoAffect(), db=p)
    rows = conn.execute(
        "SELECT action FROM audit_log "
        "WHERE target_table='diary' AND target_id='2026-05-16' "
        "AND action LIKE 'diary_single_call_%'"
    ).fetchall()
    assert any(r["action"] == "diary_single_call_no_affect_marker" for r in rows)


def test_single_call_audit_log_parse_fail_outcome(tmp_path):
    # Marker present but JSON broken -> diary_single_call_affect_parse_fail.
    p = str(tmp_path / "tel_pf.db")
    conn = storage.init_db(p)
    _session(conn, "s1", 2)
    conn.commit()

    class BrokenJSONLLM:
        def call(self, role, body, *, tier="cheap"):
            if role == "diary":
                return ("正文段落。\n===AFFECT===\n"
                        "{this is not valid json}\n===END===")
            return ""

    diary.run_day(conn, "2026-05-16", BrokenJSONLLM(), db=p)
    rows = conn.execute(
        "SELECT action, summary FROM audit_log "
        "WHERE target_table='diary' AND target_id='2026-05-16' "
        "AND action='diary_single_call_affect_parse_fail'"
    ).fetchall()
    assert len(rows) == 1
    assert "error" in rows[0]["summary"]


def test_run_day_neutral_affect_on_missing_json(tmp_path):
    # No ===AFFECT=== block -> neutral fallback; diary still written.
    p = str(tmp_path / "na.db")
    conn = storage.init_db(p)
    _session(conn, "s1", 2)
    conn.commit()
    f = FakeLLMNoAffect()
    assert diary.run_day(conn, "2026-05-16", f, db=p) is True
    diary_row = conn.execute(
        "SELECT content FROM diary WHERE date='2026-05-16'"
    ).fetchone()
    assert diary_row is not None
    affect_rows = conn.execute(
        "SELECT * FROM affect WHERE date='2026-05-16'"
    ).fetchall()
    assert len(affect_rows) == 1
    r = dict(affect_rows[0])
    assert abs(r["valence"] - diary._NEUTRAL_VALENCE) < 0.01
    assert abs(r["arousal"] - diary._NEUTRAL_AROUSAL) < 0.01
    # Source tag distinguishes single-call-with-no-affect from full LLMError
    # fallback; this row came from a successful single call missing AFFECT.
    assert r["source"] == "diary_single_call_no_affect"


def test_run_day_parse_fail_affect_source_tag(tmp_path):
    # Marker present but JSON broken -> source tag is diary_single_call_no_affect
    # (not diary_single_call, not diary_fallback).
    p = str(tmp_path / "pf.db")
    conn = storage.init_db(p)
    _session(conn, "s1", 2)
    conn.commit()

    class BrokenJSONLLM:
        def call(self, role, body, *, tier="cheap"):
            if role == "diary":
                return ("正文。\n===AFFECT===\n{broken json}\n===END===")
            return ""

    diary.run_day(conn, "2026-05-16", BrokenJSONLLM(), db=p)
    rows = conn.execute(
        "SELECT source FROM affect WHERE date='2026-05-16'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "diary_single_call_no_affect"


def test_run_day_multi_episode_affect(tmp_path):
    # Two --- separators -> two affect rows (ep=1,2).
    p = str(tmp_path / "ep.db")
    conn = storage.init_db(p)
    _session(conn, "s1", 2)
    conn.commit()

    class MultiEpLLM:
        def call(self, role, body, *, tier="cheap"):
            if role == "diary":
                prose = "早上很开心。\n---\n晚上有点累。"
                affect = json.dumps([
                    {"ep": 1, "valence": 0.8, "arousal": 0.6,
                     "importance": 6, "label": "开心", "entities": [],
                     "event_hint": ""},
                    {"ep": 2, "valence": 0.3, "arousal": 0.2,
                     "importance": 4, "label": "疲惫", "entities": [],
                     "event_hint": ""},
                ])
                return f"{prose}\n===AFFECT===\n{affect}\n===END==="
            return "ok"

    diary.run_day(conn, "2026-05-16", MultiEpLLM(), db=p)
    rows = conn.execute(
        "SELECT ep, valence FROM affect WHERE date='2026-05-16' ORDER BY ep"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["ep"] == 1
    assert rows[1]["ep"] == 2
    assert rows[0]["valence"] > rows[1]["valence"]


def test_run_day_affect_cascade_on_force(db):
    # force=True: old affect rows deleted and rebuilt with diary in same txn.
    p, conn = db
    diary.run_day(conn, "2026-05-16", FakeLLM(), db=p)
    count_before = conn.execute(
        "SELECT COUNT(*) FROM affect WHERE date='2026-05-16'"
    ).fetchone()[0]
    assert count_before == 1
    diary.run_day(conn, "2026-05-16", FakeLLM(), db=p, force=True)
    count_after = conn.execute(
        "SELECT COUNT(*) FROM affect WHERE date='2026-05-16'"
    ).fetchone()[0]
    # rebuilt: still 1, no orphan or duplicate
    assert count_after == 1


def test_over_volume_falls_back_to_3stage(db, monkeypatch):
    # chars > _OVER_VOLUME_CHARS -> pre-call early-exit to fallback; alert fired.
    p, conn = db
    monkeypatch.setattr(diary, "_OVER_VOLUME_CHARS", 1)  # force over-volume
    f = FakeLLM()
    assert diary.run_day(conn, "2026-05-16", f, db=p) is True
    assert f.n("day-digest") >= 1          # fallback map fires
    assert f.n("diary") >= 1
    al = conn.execute(
        "SELECT message FROM alerts WHERE type='routine'"
    ).fetchone()
    assert al and "over-volume" in al["message"]


def test_over_volume_fallback_writes_neutral_affect(db, monkeypatch):
    # Fallback (3-stage) yields neutral affect row (source=diary_fallback).
    p, conn = db
    monkeypatch.setattr(diary, "_OVER_VOLUME_CHARS", 1)
    f = FakeLLM()
    diary.run_day(conn, "2026-05-16", f, db=p)
    rows = conn.execute(
        "SELECT source FROM affect WHERE date='2026-05-16'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "diary_fallback"


def test_llmerror_triggers_fallback(db):
    # LLMError on single call -> falls back to 3-stage map/stitch/write.
    p, conn = db

    class ErrThenOkLLM:
        """First diary call raises; subsequent calls (fallback) succeed."""
        def __init__(self):
            self.calls = []
            self._diary_n = 0

        def call(self, role, body, *, tier="cheap"):
            self.calls.append(role)
            if role == "diary":
                self._diary_n += 1
                if self._diary_n == 1:
                    raise LLMError("refusal sentinel")
                return "今天也是平凡的一天。"
            if role == "day-digest":
                return "digest"
            if role == "stitch":
                return "woven"
            return ""

    llm = ErrThenOkLLM()
    assert diary.run_day(conn, "2026-05-16", llm, db=p) is True
    assert llm.calls.count("diary") == 2   # 1st raises, 2nd in fallback
    assert llm.calls.count("day-digest") >= 1


def test_stitch_span_tag_carries_local_date_in_fallback(tmp_path, monkeypatch):
    # Fallback stitch must carry local date so haiku keeps real order.
    p = str(tmp_path / "cross.db")
    conn = storage.init_db(p)
    for i in range(4):  # afternoon: UTC 04:00 -> local 14:00
        _ev(conn, "pm", f"2026-05-16T04:0{i}:00Z", "user", f"a{i}")
        _ev(conn, "pm", f"2026-05-16T04:0{i}:30Z", "assistant", "ok")
    for i in range(4):  # next-midnight: UTC 15:00 -> local 01:00 (05-17)
        _ev(conn, "am", f"2026-05-16T15:0{i}:00Z", "user", f"b{i}")
        _ev(conn, "am", f"2026-05-16T15:0{i}:30Z", "assistant", "ok")
    conn.commit()
    monkeypatch.setattr(diary, "_OVER_VOLUME_CHARS", 1)  # force fallback
    f = FakeLLM()
    assert diary.run_day(conn, "2026-05-16", f, db=p) is True
    body = f.stitch_bodies[0]
    assert "05-16 14:00" in body and "05-17 01:00" in body
    assert body.index("05-16 14:00") < body.index("05-17 01:00")


def test_oversized_session_in_fallback_is_chunked(db, monkeypatch):
    # Over-volume fallback: chunked digest for an oversized session.
    p, conn = db
    monkeypatch.setattr(diary, "_OVER_VOLUME_CHARS", 1)
    big = "x" * (diary._SESSION_CHAR_CAP + diary._CHUNK_CHARS)
    _ev(conn, "s3", "2026-05-16T10:00:00Z", "user", big)
    for i in range(1, 4):
        _ev(conn, "s3", f"2026-05-16T10:0{i}:00Z", "user", "more")
    conn.commit()
    f = FakeLLM()
    diary.run_day(conn, "2026-05-16", f, db=p)
    # s1 (1) + s2 (1) + s3 chunked (>=2) -> more than 3 digest calls in fallback
    assert f.n("day-digest") >= 4


def test_idempotent_skip(db):
    p, conn = db
    diary.run_day(conn, "2026-05-16", FakeLLM(), db=p)
    f2 = FakeLLM()
    assert diary.run_day(conn, "2026-05-16", f2, db=p) is False
    assert f2.calls == []


# ── same-day correction (force overwrite) vs catchup idempotency ──────────────

def test_force_overwrites_existing_diary(db):
    # A late session closes after the 04:00 routine already wrote the day.
    # An explicit forced re-run MUST replace the row + lessons-free content,
    # keeping date PK stable; catchup default path stays skip-if-exists.
    p, conn = db
    diary.run_day(conn, "2026-05-16", FakeLLM(digest="first pass"), db=p)
    first = conn.execute(
        "SELECT content FROM diary WHERE date='2026-05-16'").fetchone()
    f2 = FakeLLM()
    assert diary.run_day(conn, "2026-05-16", f2, db=p, force=True) is True
    assert f2.n("diary") == 1                       # actually re-wrote
    rows = conn.execute(
        "SELECT COUNT(*) c FROM diary WHERE date='2026-05-16'").fetchone()
    assert rows["c"] == 1                           # PK stable, no dup
    row = conn.execute(
        "SELECT content,updated_at,created_at FROM diary "
        "WHERE date='2026-05-16'").fetchone()
    assert row["content"] != first["content"]       # overwritten
    aud = conn.execute(
        "SELECT action FROM audit_log WHERE target_table='diary' "
        "AND target_id='2026-05-16' ORDER BY id DESC LIMIT 1").fetchone()
    assert aud["action"] == "update"                # not a silent insert


def test_catchup_default_still_idempotent(db):
    # Without force, an existing row is never overwritten — unattended
    # catchup/routine stays idempotent (no LLM spent).
    p, conn = db
    diary.run_day(conn, "2026-05-16", FakeLLM(), db=p)
    f2 = FakeLLM()
    assert diary.run_day(conn, "2026-05-16", f2, db=p) is False
    assert f2.calls == []
    assert diary.run(conn, f2, db=p, catchup=True) == []
    assert f2.calls == []


def test_run_force_flag_threads_to_run_day(db):
    p, conn = db
    diary.run_day(conn, "2026-05-16", FakeLLM(), db=p)
    out = diary.run(conn, FakeLLM(), db=p, day="2026-05-16", force=True)
    assert out == ["2026-05-16"]


# ── multi-process app-lock ────────────────────────────────────────────────────

def test_lock_serializes_separate_holders(tmp_path):
    import os
    lf = str(tmp_path / "diary.lock")
    with diary._app_lock(lf):
        # a second non-blocking acquire from another fd must fail while held
        with pytest.raises(BlockingIOError):
            with diary._app_lock(lf, blocking=False):
                pass
    # released after the block — re-acquire succeeds
    with diary._app_lock(lf, blocking=False):
        pass
    assert os.path.exists(lf)


def test_lock_releases_on_exception(tmp_path):
    lf = str(tmp_path / "diary.lock")
    with pytest.raises(RuntimeError):
        with diary._app_lock(lf):
            raise RuntimeError("boom")
    # lock must be free again despite the exception
    with diary._app_lock(lf, blocking=False):
        pass


def test_main_holds_lock_around_run(db, monkeypatch, tmp_path):
    # main() must wrap run() in the app-lock so routine/catchup/manual
    # serialize instead of colliding on the diary date PK.
    p, conn = db
    seen = {}
    real = diary._app_lock

    def spy(path=None, blocking=True):
        from pathlib import Path
        seen["path"] = path or str(
            Path(diary.config.DATA_DIR) / "diary.lock")
        return real(path, blocking=blocking)

    monkeypatch.setattr(diary, "_app_lock", spy)
    monkeypatch.setattr(diary.config, "db_path", lambda: p)
    monkeypatch.setattr(diary.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(diary.storage, "connect", lambda _p: conn)
    monkeypatch.setattr(diary, "LLMClient", lambda **k: FakeLLM())
    # main() closes conn in finally; assertions only read `seen`
    assert diary.main(["--day", "2026-05-16"]) == 0
    assert "path" in seen and seen["path"].endswith(".lock")


# ── _routine_target boundary (00:00–03:59 belongs to previous diary day) ──────

def _freeze_now(monkeypatch, when):
    class _DT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(diary._dt, "datetime", _DT)


def test_routine_target_at_0359_is_two_days_back(monkeypatch):
    # 03:59 May 18 local is still inside diary day May 17's window
    # ([May17 04:00, May18 04:00)); the last FULLY closed day is May 16.
    _freeze_now(monkeypatch, dt.datetime(2026, 5, 18, 3, 59, tzinfo=diary._TZ))
    assert diary._routine_target() == "2026-05-16"


def test_routine_target_at_0401_is_just_closed_day(monkeypatch):
    # 04:01 May 18: diary day May 17 just closed at 04:00 -> target May 17.
    _freeze_now(monkeypatch, dt.datetime(2026, 5, 18, 4, 1, tzinfo=diary._TZ))
    assert diary._routine_target() == "2026-05-17"




# ── dual triggers ─────────────────────────────────────────────────────────────

def test_catchup_caps_and_alerts(db, monkeypatch):
    # Pin "today" to 2026-05-17 so the 7d window deterministically covers
    # the fixture's 2026-05-16 base + 5 days before it, independent of when
    # the test runs.
    p, conn = db
    pinned = dt.date(2026, 5, 17)

    class _D(dt.date):
        @classmethod
        def today(cls):
            return pinned

    class _DT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 5, 17, 4, 30, tzinfo=diary._TZ)

    monkeypatch.setattr(diary._dt, "date", _D)
    monkeypatch.setattr(diary._dt, "datetime", _DT)
    base = dt.date(2026, 5, 16)
    for i in range(1, 6):  # 5 extra missing diary days
        d = base - dt.timedelta(days=i)
        _ev(conn, "s", f"{d.isoformat()}T08:00:00Z", "user", "x")
    conn.commit()
    written = diary.run(conn, FakeLLM(), db=p, catchup=True)
    assert len(written) == diary.CATCHUP_MAX
    al = conn.execute(
        "SELECT message FROM alerts WHERE type='routine'").fetchone()
    assert al and "still pending" in al["message"]


def test_run_explicit_day(db):
    p, conn = db
    assert diary.run(conn, FakeLLM(), db=p, day="2026-05-16") == ["2026-05-16"]


# ── skip / code-drop filter ───────────────────────────────────────────────────

def test_low_turn_session_hard_dropped(tmp_path):
    # <= _SKIP_DROP_MAX user turns -> code-drops before single call, no LLM.
    p = str(tmp_path / "lo.db")
    conn = storage.init_db(p)
    _session(conn, "lo", 3, n_user=diary._SKIP_DROP_MAX)
    conn.commit()
    f = FakeLLM()
    assert diary.run_day(conn, "2026-05-16", f, db=p) is True
    assert f.n("diary") == 0           # whole day code-dropped -> placeholder
    row = conn.execute(
        "SELECT content,session_ids FROM diary WHERE date='2026-05-16'"
    ).fetchone()
    assert row["content"] == "—"
    assert row["session_ids"] == ""


def test_fallback_short_session_skip_drops(db, monkeypatch):
    # Fallback path: 4-turn sessions hit DIGEST_SHORT; SKIP honoured -> dropped.
    # Simulate fallback by forcing over-volume.
    p, conn = db
    monkeypatch.setattr(diary, "_OVER_VOLUME_CHARS", 1)
    f = FakeLLM(digest="SKIP")
    assert diary.run_day(conn, "2026-05-16", f, db=p) is True
    assert f.n("day-digest") == 2
    assert conn.execute(
        "SELECT content FROM diary WHERE date='2026-05-16'"
    ).fetchone()["content"] == "—"


def test_fallback_long_session_skip_not_honoured(tmp_path, monkeypatch):
    # Fallback path: >_SKIP_JUDGE_MAX turns -> DIGEST_LONG; SKIP not honoured.
    p = str(tmp_path / "long.db")
    conn = storage.init_db(p)
    _session(conn, "lng", 2, n_user=diary._SKIP_JUDGE_MAX + 5)
    conn.commit()
    monkeypatch.setattr(diary, "_OVER_VOLUME_CHARS", 1)
    f = FakeLLM(digest="SKIP")
    assert diary.run_day(conn, "2026-05-16", f, db=p) is True
    row = conn.execute(
        "SELECT content,session_ids FROM diary WHERE date='2026-05-16'"
    ).fetchone()
    assert row["content"] != "—" and row["session_ids"] == "lng"


def test_fallback_routes_by_turn_count(tmp_path, monkeypatch):
    # Fallback routes DIGEST_SHORT vs DIGEST_LONG by turn count.
    p = str(tmp_path / "r.db")
    conn = storage.init_db(p)
    _session(conn, "sh", 2, n_user=6)
    _session(conn, "lg", 9, n_user=diary._SKIP_JUDGE_MAX + 5)
    conn.commit()
    monkeypatch.setattr(diary, "_OVER_VOLUME_CHARS", 1)
    f = FakeLLM()
    diary.run_day(conn, "2026-05-16", f, db=p)
    short_in = any("short session" in b for b in f.digest_bodies)
    long_in = any("long session" in b for b in f.digest_bodies)
    assert short_in and long_in


# ── Phase 2: _parse_single_call unit tests ────────────────────────────────────

def test_parse_single_call_prose_and_affect():
    prose, aff, outcome, _err = diary._parse_single_call(
        "段落一。\n---\n段落二。\n===AFFECT===\n"
        '[{"ep":1,"valence":0.8,"arousal":0.5,"importance":7,'
        '"label":"开心","entities":["Lumi"],"event_hint":"早上好"},'
        '{"ep":2,"valence":0.4,"arousal":0.3,"importance":5,'
        '"label":"平静","entities":[],"event_hint":""}]\n===END==="'
    )
    assert "段落一" in prose and "段落二" in prose
    assert "===AFFECT===" not in prose
    assert len(aff) == 2
    assert aff[0]["ep"] == 1
    assert outcome == "ok"


def test_parse_single_call_bad_json_returns_empty_affect():
    prose, aff, outcome, err = diary._parse_single_call(
        "一些日记内容。\n===AFFECT===\n{not valid json}\n===END==="
    )
    assert "日记内容" in prose
    assert aff == []
    assert outcome == "parse_fail"
    assert err  # non-empty excerpt


def test_parse_single_call_no_affect_block():
    prose, aff, outcome, _err = diary._parse_single_call("完全没有情感数据的正文。")
    assert prose == "完全没有情感数据的正文。"
    assert aff == []
    assert outcome == "no_marker"


def test_parse_single_call_missing_end_sentinel():
    # ===END=== absent -> still attempt parse up to EOF
    prose, aff, outcome, _err = diary._parse_single_call(
        "正文。\n===AFFECT===\n[]\n"
    )
    assert aff == []  # empty list is valid, though unusual
    assert outcome == "ok"


# ── Phase 2: _resolve_event_hint unit test ────────────────────────────────────

def test_resolve_event_hint_no_match(tmp_path):
    p = str(tmp_path / "h.db")
    conn = storage.init_db(p)
    assert diary._resolve_event_hint(conn, "不存在的关键词XYZ") is None


def test_resolve_event_hint_unique_match(tmp_path):
    # FTS5 unicode61 tokenizes ASCII; unique match -> event_id returned.
    p = str(tmp_path / "h2.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO events(session_id,timestamp,role,content) "
        "VALUES('s1','2026-05-16T02:00:00Z','user','only this event ZZZQ')"
    )
    conn.commit()
    eid = diary._resolve_event_hint(conn, "ZZZQ")
    assert eid is not None


def test_resolve_event_hint_multi_match_returns_null(tmp_path):
    # Two events both match the hint -> NULL, never first-match.
    p = str(tmp_path / "h3.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO events(session_id,timestamp,role,content) "
        "VALUES('s1','2026-05-16T02:00:00Z','user','common keyword MATCHKEY first')"
    )
    conn.execute(
        "INSERT INTO events(session_id,timestamp,role,content) "
        "VALUES('s2','2026-05-16T03:00:00Z','user','common keyword MATCHKEY second')"
    )
    conn.commit()
    assert diary._resolve_event_hint(conn, "MATCHKEY") is None
