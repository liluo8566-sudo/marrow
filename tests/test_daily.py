"""Tests for marrow/daily.py. LLM faked — prompt quality not under test;
read-from-digests path, day-boundary 6AM, idempotency, force-overwrite,
catchup loop, and lock contention are.
"""
from __future__ import annotations

import datetime as dt

import pytest

from marrow import daily, daily_catchup, storage
from marrow.llm import LLMError


class FakeLLM:
    def __init__(self, prose="今天和念念过得很开心。", raise_on_call=False,
                 per_role: dict[str, str] | None = None,
                 raise_roles: set[str] | None = None):
        self.prose = prose
        self.raise_on_call = raise_on_call
        self.per_role = per_role or {}
        self.raise_roles = raise_roles or set()
        self.calls: list[str] = []

    def call(self, role, body, *, tier="cheap"):
        self.calls.append(role)
        if self.raise_on_call or role in self.raise_roles:
            raise LLMError("fake failure")
        return self.per_role.get(role, self.prose)

    def n(self, role):
        return self.calls.count(role)


def _ev(conn, sid, ts, role, content):
    conn.execute("INSERT INTO events(session_id,timestamp,role,content) "
                 "VALUES(?,?,?,?)", (sid, ts, role, content))


def _digest(conn, sid, date, text):
    conn.execute(
        "INSERT INTO session_digests (sid, date, text, ts)"
        " VALUES (?, ?, ?, ?)",
        (sid, date, text, "2026-05-23T00:00:00Z"))


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    _ev(conn, "s1", "2026-05-16T02:00:00Z", "user", "hi")
    _ev(conn, "s2", "2026-05-16T09:00:00Z", "user", "later")
    _digest(conn, "s1", "2026-05-16", "morning chat")
    _digest(conn, "s2", "2026-05-16", "afternoon work")
    conn.commit()
    return p, conn


# ── digest source ────────────────────────────────────────────────────────────

def test_read_digests_uses_session_digests_table(db):
    """daily._read_digests must read from session_digests, not audit_log."""
    p, conn = db
    out = daily._read_digests(conn, "2026-05-16")
    sids = {sid for sid, _ in out}
    assert sids == {"s1", "s2"}
    texts = {text for _, text in out}
    assert "morning chat" in texts and "afternoon work" in texts


def test_read_digests_ignores_audit_log_legacy(tmp_path):
    """Legacy audit_log session_digest rows must NOT feed daily."""
    p = str(tmp_path / "leg.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO audit_log (target_table, target_id, action, summary)"
        " VALUES ('session_digest', 'old-sid', 'digest',"
        " '{\"sid\":\"old-sid\",\"date\":\"2026-05-16\",\"text\":\"legacy\"}')"
    )
    conn.commit()
    assert daily._read_digests(conn, "2026-05-16") == []
    conn.close()


# ── 6AM day boundary ─────────────────────────────────────────────────────────

def test_diary_day_local_0600_cutoff():
    assert daily_catchup.diary_day("2026-05-16T19:00:00Z") == "2026-05-16"
    assert daily_catchup.diary_day("2026-05-16T20:00:00Z") == "2026-05-17"
    assert daily_catchup.diary_day("2026-05-16T18:00:00Z") == "2026-05-16"


def test_routine_target_is_yesterday(monkeypatch):
    fixed = dt.datetime(2026, 5, 18, 7, 0, tzinfo=daily_catchup._TZ)

    class _DT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(daily_catchup._dt, "datetime", _DT)
    assert daily_catchup.routine_target() == "2026-05-17"


# ── daily.run_day reads digests + writes diary ───────────────────────────────

def test_run_day_writes_diary_from_digests(db):
    p, conn = db
    f = FakeLLM()
    assert daily.run_day(conn, "2026-05-16", f, db=p) is True
    assert f.n("daily") == 1
    # candidate extraction call also fires (one per day with digests)
    assert f.n("daily_cand") == 1
    row = conn.execute(
        "SELECT content, session_ids FROM diary WHERE date='2026-05-16'"
    ).fetchone()
    assert "念念" in row["content"]
    assert row["session_ids"] == "s1,s2"


def test_run_day_idempotent_skip(db):
    p, conn = db
    daily.run_day(conn, "2026-05-16", FakeLLM(), db=p)
    f2 = FakeLLM()
    assert daily.run_day(conn, "2026-05-16", f2, db=p) is False
    assert f2.calls == []


def test_run_day_force_overwrites(db):
    p, conn = db
    daily.run_day(conn, "2026-05-16", FakeLLM(prose="first"), db=p)
    first = conn.execute(
        "SELECT content FROM diary WHERE date='2026-05-16'"
    ).fetchone()["content"]
    daily.run_day(conn, "2026-05-16", FakeLLM(prose="second"), db=p, force=True)
    row = conn.execute(
        "SELECT content FROM diary WHERE date='2026-05-16'"
    ).fetchone()
    assert row["content"] != first
    assert "second" in row["content"]


def test_run_day_stub_when_no_digests_no_affect(tmp_path):
    p = str(tmp_path / "empty.db")
    conn = storage.init_db(p)
    f = FakeLLM()
    assert daily.run_day(conn, "2026-05-16", f, db=p) is True
    row = conn.execute(
        "SELECT content FROM diary WHERE date='2026-05-16'"
    ).fetchone()
    assert row["content"] == "—"
    assert f.calls == []


def test_run_day_affect_only_no_digest(tmp_path):
    p = str(tmp_path / "aff.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO affect(date,ep,valence,arousal,importance,label)"
        " VALUES('2026-05-16',1,0.7,0.5,3,'温馨')")
    conn.commit()
    f = FakeLLM(prose="一段温馨的回忆。")
    assert daily.run_day(conn, "2026-05-16", f, db=p) is True
    assert f.n("daily") == 1


def test_run_day_llm_failure_alerts(db):
    p, conn = db
    f = FakeLLM(raise_on_call=True)
    assert daily.run_day(conn, "2026-05-16", f, db=p) is False
    al = conn.execute(
        "SELECT message FROM alerts WHERE type='routine'"
    ).fetchone()
    assert al and "failed" in al["message"]


# ── candidate extraction (entity / milestone / vocab) ───────────────────────

_CAND_RAW = (
    "===ENTITY_CAND===\n"
    "[{\"name\": \"陈奶奶\", \"kind\": \"person\", \"conf\": 0.9,"
    " \"note\": \"邻居\"}]\n"
    "===END===\n"
    "===MILESTONE_CAND===\n"
    "[{\"title\": \"GAMSAT pass\", \"scope\": \"me\","
    " \"date\": \"2026-05-16\", \"description\": \"念念 passed GAMSAT.\","
    " \"conf\": 0.9}]\n"
    "===END===\n"
    "===VOCAB_CAND===\n"
    "[{\"key\": \"小笼包\", \"type\": \"meme\","
    " \"value\": \"周末早茶专属梗\", \"context\": \"老婆点了 8 笼\","
    " \"pinned\": 0, \"conf\": 0.8}]\n"
    "===END===\n"
)


def test_run_day_extracts_three_candidate_blocks(db):
    p, conn = db
    f = FakeLLM(per_role={"daily_cand": _CAND_RAW,
                          "daily": "diary prose"})
    assert daily.run_day(conn, "2026-05-16", f, db=p) is True
    assert f.n("daily_cand") == 1 and f.n("daily") == 1
    ent = conn.execute(
        "SELECT name, kind FROM entities WHERE name='陈奶奶'"
    ).fetchone()
    assert ent is not None and ent["kind"] == "person"
    ms = conn.execute(
        "SELECT title, scope FROM milestones WHERE title='GAMSAT pass'"
    ).fetchone()
    assert ms is not None and ms["scope"] == "me"
    vc = conn.execute(
        "SELECT key, pinned, use_count FROM vocab WHERE key='小笼包'"
    ).fetchone()
    assert vc is not None
    assert vc["pinned"] == 0  # public meme — not anchor, not cipher
    assert vc["use_count"] == 1
    audit = conn.execute(
        "SELECT summary FROM audit_log WHERE action='cand_extract'"
        " AND target_id='2026-05-16'"
    ).fetchone()
    assert audit and "entity=1" in audit["summary"]
    assert "milestone=1" in audit["summary"] and "vocab=1" in audit["summary"]


def test_run_day_vocab_anchor_forces_pinned(db):
    """LLM emits pinned=0 on an anchor key → writer forces pinned=1."""
    p, conn = db
    raw = (
        "===VOCAB_CAND===\n"
        "[{\"key\": \"鸭子\", \"type\": \"nickname\","
        " \"value\": \"屿忱昵称\", \"context\": \"\","
        " \"pinned\": 0, \"conf\": 0.9}]\n"
        "===END===\n"
    )
    f = FakeLLM(per_role={"daily_cand": raw, "daily": "x"})
    daily.run_day(conn, "2026-05-16", f, db=p)
    vc = conn.execute(
        "SELECT pinned FROM vocab WHERE key='鸭子'"
    ).fetchone()
    assert vc is not None and vc["pinned"] == 1


def test_run_day_vocab_cipher_type_forces_pinned(db):
    """type='cipher' is always pinned regardless of key / LLM flag."""
    p, conn = db
    raw = (
        "===VOCAB_CAND===\n"
        "[{\"key\": \"sec_anchor\", \"type\": \"cipher\","
        " \"value\": \"x\", \"context\": \"\","
        " \"pinned\": 0, \"conf\": 0.9}]\n"
        "===END===\n"
    )
    f = FakeLLM(per_role={"daily_cand": raw, "daily": "x"})
    daily.run_day(conn, "2026-05-16", f, db=p)
    vc = conn.execute(
        "SELECT pinned FROM vocab WHERE key='sec_anchor'"
    ).fetchone()
    assert vc is not None and vc["pinned"] == 1


def test_run_day_cand_llm_failure_does_not_block_diary(db):
    """daily_cand call raises → alert logged, diary still written."""
    p, conn = db
    f = FakeLLM(prose="diary text",
                raise_roles={"daily_cand"})
    assert daily.run_day(conn, "2026-05-16", f, db=p) is True
    row = conn.execute(
        "SELECT content FROM diary WHERE date='2026-05-16'"
    ).fetchone()
    assert row and "diary text" in row["content"]
    al = conn.execute(
        "SELECT severity, message FROM alerts"
        " WHERE type='routine' AND message LIKE '%candidate%'"
    ).fetchone()
    assert al and al["severity"] == "warn"


def test_vocab_writer_upgrades_pinned_but_never_downgrades(db):
    """Existing pinned=1 row stays pinned even if a later session emits 0."""
    p, conn = db
    from marrow import candidates as cmod
    # First insert: anchor forces pinned=1
    raw1 = (
        "===VOCAB_CAND===\n"
        "[{\"key\": \"老公\", \"type\": \"nickname\","
        " \"value\": \"x\", \"context\": \"\","
        " \"pinned\": 1, \"conf\": 0.9}]\n"
        "===END===\n"
    )
    cmod.write_vocab_cand(conn, raw1)
    # Second insert: LLM emits pinned=0, but anchor still on key — stays 1.
    raw2 = raw1.replace("\"pinned\": 1", "\"pinned\": 0")
    cmod.write_vocab_cand(conn, raw2)
    vc = conn.execute(
        "SELECT pinned, use_count FROM vocab WHERE key='老公'"
    ).fetchone()
    assert vc["pinned"] == 1
    assert vc["use_count"] == 2


# ── dual triggers (routine vs catchup) ───────────────────────────────────────

def test_run_explicit_day(db):
    p, conn = db
    assert daily.run(conn, FakeLLM(), db=p, day="2026-05-16") == ["2026-05-16"]


def test_run_catchup_loops_pending(db, monkeypatch):
    p, conn = db
    pinned = dt.date(2026, 5, 18)

    class _D(dt.date):
        @classmethod
        def today(cls):
            return pinned

    class _DT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 5, 18, 19, 0, tzinfo=daily_catchup._TZ)

    monkeypatch.setattr(daily_catchup._dt, "date", _D)
    monkeypatch.setattr(daily_catchup._dt, "datetime", _DT)
    f = FakeLLM()
    written = daily.run(conn, f, db=p, catchup=True)
    assert "2026-05-16" in written


def test_run_catchup_caps_and_alerts(db, monkeypatch):
    p, conn = db
    pinned = dt.date(2026, 5, 17)

    class _D(dt.date):
        @classmethod
        def today(cls):
            return pinned

    class _DT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 5, 17, 19, 0, tzinfo=daily_catchup._TZ)

    monkeypatch.setattr(daily_catchup._dt, "date", _D)
    monkeypatch.setattr(daily_catchup._dt, "datetime", _DT)
    base = dt.date(2026, 5, 16)
    for i in range(1, 6):
        d = base - dt.timedelta(days=i)
        _ev(conn, f"sx{i}", f"{d.isoformat()}T10:00:00Z", "user", "x")
    conn.commit()
    written = daily.run(conn, FakeLLM(), db=p, catchup=True)
    assert len(written) == daily_catchup.CATCHUP_MAX
    al = conn.execute(
        "SELECT message FROM alerts WHERE type='routine'"
    ).fetchone()
    assert al and "still pending" in al["message"]


# ── main() flag handling ──────────────────────────────────────────────────────

def test_main_force_flag_routes_to_run_day(db, monkeypatch, tmp_path):
    p, conn = db
    daily.run_day(conn, "2026-05-16", FakeLLM(prose="initial"), db=p)
    conn.close()  # main() will open its own
    seen = {}
    real = daily_catchup.app_lock
    real_connect = storage.connect

    def spy(path=None, blocking=True):
        from pathlib import Path
        seen["path"] = path or str(
            Path(daily.config.DATA_DIR) / "daily.lock")
        return real(path, blocking=blocking)

    monkeypatch.setattr(daily.daily_catchup, "app_lock", spy)
    monkeypatch.setattr(daily.config, "db_path", lambda: p)
    monkeypatch.setattr(daily.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(daily, "LLMClient", lambda **k: FakeLLM(prose="forced"))
    assert daily.main(["--day", "2026-05-16", "--force"]) == 0
    assert "path" in seen and seen["path"].endswith(".lock")
    fresh = real_connect(p)
    try:
        row = fresh.execute(
            "SELECT content FROM diary WHERE date='2026-05-16'"
        ).fetchone()
        assert "forced" in row["content"]
    finally:
        fresh.close()
