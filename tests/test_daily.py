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


# ── candidate extraction (entity / milestone / memes) ──────────────────────

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
    "===MEMES_CAND===\n"
    "[{\"key\": \"小笼包\", \"type\": \"paw\","
    " \"value\": \"周末早茶专属梗\", \"context\": \"老婆点了 8 笼\","
    " \"pinned\": 0, \"conf\": 0.8}]\n"
    "===END===\n"
)


def test_run_day_extracts_three_candidate_blocks(db):
    p, conn = db
    # Seed 3 events with the paw key so it passes the 7d frequency gate.
    for i in range(3):
        _ev(conn, f"s_seed{i}", f"2026-05-{12+i:02d}T10:00:00Z",
            "user", f"今天又吃了小笼包 {i}")
    conn.commit()
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
        "SELECT key, pinned, use_count FROM memes WHERE key='小笼包'"
    ).fetchone()
    assert vc is not None
    # type=paw is auto-pinned by the writer (dyad-exclusive inside joke)
    assert vc["pinned"] == 1
    assert vc["use_count"] == 1
    audit = conn.execute(
        "SELECT summary FROM audit_log WHERE action='cand_extract'"
        " AND target_id='2026-05-16'"
    ).fetchone()
    assert audit and "entity=1" in audit["summary"]
    assert "milestone=1" in audit["summary"] and "memes=1" in audit["summary"]


def _seed_key_events(conn, key: str, ref_date: str, count: int = 4) -> None:
    """Seed `count` events containing `key` on distinct days within 14d window."""
    import datetime as _dt
    base = _dt.date.fromisoformat(ref_date)
    for i in range(count):
        ts = (base - _dt.timedelta(days=i % 13)).isoformat() + "T08:00:00Z"
        conn.execute(
            "INSERT INTO events(session_id,timestamp,role,content)"
            " VALUES(?,?,?,?)",
            (f"seed_{key}_{i}", ts, "user", f"mention of {key} today {i}"),
        )
    conn.commit()


def test_run_day_memes_anchor_forces_pinned(db, monkeypatch):
    """LLM emits pinned=0 on an anchor key → writer forces pinned=1.
    Using type=others so the anchor list is the sole pinning trigger
    (paw/fact would auto-pin regardless and obscure the assertion).
    Requires 14d freq gate to pass — seed events containing the key.
    """
    from marrow import config
    monkeypatch.setattr(config, "anchor_keys_set",
                        lambda: frozenset({"鸭子"}))
    p, conn = db
    _seed_key_events(conn, "鸭子", "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\": \"鸭子\", \"type\": \"others\","
        " \"value\": \"屿忱昵称\", \"context\": \"\","
        " \"pinned\": 0, \"conf\": 0.9}]\n"
        "===END===\n"
    )
    f = FakeLLM(per_role={"daily_cand": raw, "daily": "x"})
    daily.run_day(conn, "2026-05-16", f, db=p)
    vc = conn.execute(
        "SELECT pinned FROM memes WHERE key='鸭子'"
    ).fetchone()
    assert vc is not None and vc["pinned"] == 1


def test_run_day_memes_fact_type_forces_pinned(db):
    """type='fact' is always pinned regardless of LLM flag / anchor list.
    Requires 14d freq gate to pass — seed events containing the key.
    """
    p, conn = db
    _seed_key_events(conn, "sec_anchor", "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\": \"sec_anchor\", \"type\": \"fact\","
        " \"value\": \"x\", \"context\": \"\","
        " \"pinned\": 0, \"conf\": 0.9}]\n"
        "===END===\n"
    )
    f = FakeLLM(per_role={"daily_cand": raw, "daily": "x"})
    daily.run_day(conn, "2026-05-16", f, db=p)
    vc = conn.execute(
        "SELECT pinned FROM memes WHERE key='sec_anchor'"
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


def test_memes_writer_upgrades_pinned_but_never_downgrades(db):
    """Existing pinned=1 row stays pinned even if a later session emits 0."""
    p, conn = db
    from marrow import candidates as cmod
    # paw type now goes through the freq gate — seed 3 events with the key
    # within the 7d window ending at the test's reference date.
    for i in range(3):
        _ev(conn, f"s_seed{i}", f"2026-05-{12+i:02d}T10:00:00Z",
            "user", f"叫老公叫得真甜 {i}")
    conn.commit()
    # First insert: anchor + paw both pin → pinned=1
    raw1 = (
        "===MEMES_CAND===\n"
        "[{\"key\": \"老公\", \"type\": \"paw\","
        " \"value\": \"x\", \"context\": \"\","
        " \"pinned\": 1, \"conf\": 0.9}]\n"
        "===END===\n"
    )
    cmod.write_memes_cand(conn, raw1, date="2026-05-16")
    # Second insert: LLM emits pinned=0, but paw auto-pins → stays 1.
    raw2 = raw1.replace("\"pinned\": 1", "\"pinned\": 0")
    cmod.write_memes_cand(conn, raw2, date="2026-05-16")
    vc = conn.execute(
        "SELECT pinned, use_count FROM memes WHERE key='老公'"
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

    sub_folder = tmp_path / "sub_pages"
    sub_state = tmp_path / "sub_state"
    monkeypatch.setattr(daily.daily_catchup, "app_lock", spy)
    monkeypatch.setattr(daily.config, "db_path", lambda: p)
    monkeypatch.setattr(daily.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(daily.config, "sub_pages_path", lambda: str(sub_folder))
    monkeypatch.setattr(daily.config, "sub_pages_state_path", lambda: str(sub_state))
    monkeypatch.setattr(daily, "LLMClient", lambda **k: FakeLLM(prose="forced"))
    assert daily.main(["--day", "2026-05-16", "--force"]) == 0
    assert (sub_folder / "diary.md").exists()
    assert "path" in seen and seen["path"].endswith(".lock")
    fresh = real_connect(p)
    try:
        row = fresh.execute(
            "SELECT content FROM diary WHERE date='2026-05-16'"
        ).fetchone()
        assert "forced" in row["content"]
    finally:
        fresh.close()


def test_main_alerts_when_written_day_silently_deleted(
    db, monkeypatch, tmp_path
):
    """Post-condition: a day daily.run claimed to write must still be in
    diary after write_all_subpages returns. If reconcile (or anything else)
    sweeps it in the same pass, emit a critical alert. Regression for
    2026-06-04 silent delete (caused by reconcile_diary DELETE pass on a
    row that hadn't yet been rendered to md).
    """
    p, conn = db
    conn.close()

    sub_folder = tmp_path / "sub_pages"
    sub_state = tmp_path / "sub_state"
    monkeypatch.setattr(daily.config, "db_path", lambda: p)
    monkeypatch.setattr(daily.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(daily.config, "sub_pages_path",
                        lambda: str(sub_folder))
    monkeypatch.setattr(daily.config, "sub_pages_state_path",
                        lambda: str(sub_state))
    monkeypatch.setattr(daily, "LLMClient",
                        lambda **k: FakeLLM(prose="alpha"))

    # Simulate the bug: pretend write_all_subpages succeeds, but during
    # that call something deletes the row daily.py just wrote.
    real_write = daily.subpages.write_all_subpages

    def evil_write(conn, *, folder, state_dir, db=None):
        real_write(conn, folder=folder, state_dir=state_dir, db=db)
        conn.execute("DELETE FROM diary WHERE date='2026-05-16'")
        conn.commit()

    monkeypatch.setattr(daily.subpages, "write_all_subpages", evil_write)
    rc = daily.main(["--day", "2026-05-16"])
    assert rc == 0

    fresh = storage.connect(p)
    try:
        row = fresh.execute(
            "SELECT severity, type, message FROM alerts"
            " WHERE type='routine' AND severity='critical'"
            " AND message LIKE '%silent-delete%'"
        ).fetchone()
    finally:
        fresh.close()
    assert row is not None
    assert "2026-05-16" in row["message"]


# ── _parse_tl_line ─────────────────────────────────────────────────────────────

def test_parse_tl_line_extracts_and_strips():
    narrative = "日记正文在这里。\n\nTL_LINE: 今天陪老婆改recall机制，深夜还在聊天"
    diary_text, tl = daily._parse_tl_line(narrative)
    assert tl == "今天陪老婆改recall机制，深夜还在聊天"
    assert "TL_LINE" not in diary_text
    assert "日记正文" in diary_text


def test_parse_tl_line_fullwidth_colon():
    narrative = "正文。\nTL_LINE：和老婆一起吃了拿铁"
    _, tl = daily._parse_tl_line(narrative)
    assert tl == "和老婆一起吃了拿铁"


def test_parse_tl_line_missing():
    narrative = "纯日记，没有TL行"
    diary_text, tl = daily._parse_tl_line(narrative)
    assert tl == ""
    assert diary_text == narrative


def test_parse_tl_line_uses_last_occurrence():
    """When TL_LINE appears twice, only the last one is extracted."""
    narrative = "TL_LINE: 第一行\n正文\nTL_LINE: 最后一行"
    _, tl = daily._parse_tl_line(narrative)
    assert tl == "最后一行"


# ── diary tl_line persisted ───────────────────────────────────────────────────

def test_run_day_persists_tl_line(tmp_path):
    """run_day writes tl_line to diary row when LLM includes TL_LINE:."""
    p = str(tmp_path / "tl.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO session_digests (sid,date,text,ts)"
        " VALUES ('s1','2026-05-16','content','2026-05-16T10:00Z')"
    )
    conn.commit()

    llm = FakeLLM(prose="日记正文。\n\nTL_LINE: 今天和老婆写了很多代码")
    assert daily.run_day(conn, "2026-05-16", llm, db=p) is True

    row = conn.execute(
        "SELECT tl_line FROM diary WHERE date='2026-05-16'"
    ).fetchone()
    assert row is not None
    assert row["tl_line"] == "今天和老婆写了很多代码"


def test_run_day_tl_line_missing_still_writes_diary(tmp_path):
    """run_day succeeds even when LLM omits TL_LINE — tl_line stays NULL."""
    p = str(tmp_path / "tl2.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO session_digests (sid,date,text,ts)"
        " VALUES ('s1','2026-05-16','content','2026-05-16T10:00Z')"
    )
    conn.commit()

    llm = FakeLLM(prose="日记正文，没有TL行。")
    assert daily.run_day(conn, "2026-05-16", llm, db=p) is True

    row = conn.execute(
        "SELECT content, tl_line FROM diary WHERE date='2026-05-16'"
    ).fetchone()
    assert row is not None
    assert "日记正文" in row["content"]
    assert row["tl_line"] is None


# ── affect eph/epl format ─────────────────────────────────────────────────────

def test_format_affect_block_eph_epl():
    """High-valence ep → eph; low-valence → epl in material block."""
    from marrow.daily import _format_affect_block
    episodes = [
        {"ep": 1, "importance": 3, "label": "温暖", "description": "聊天",
         "valence": 0.8, "arousal": 0.4, "side": "eph", "unresolved": False},
        {"ep": 2, "importance": 4, "label": "焦虑", "description": "考试",
         "valence": 0.2, "arousal": 0.7, "side": "epl", "unresolved": True},
    ]
    block = _format_affect_block("2026-06-11", episodes)
    assert "eph3" in block
    assert "epl4" in block
    assert "[open]" in block
    assert "[open]" not in block.split("epl4")[0]  # only on epl ep


def test_affect_block_open_mark_only_on_unresolved():
    from marrow.daily import _format_affect_block
    eps = [
        {"ep": 1, "importance": 2, "label": "平淡", "description": "日常",
         "valence": 0.5, "arousal": 0.3, "side": "eph", "unresolved": False},
    ]
    block = _format_affect_block("2026-06-11", eps)
    assert "[open]" not in block
