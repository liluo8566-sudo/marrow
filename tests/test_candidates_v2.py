"""Tests for the v2 candidate writer rules:
- milestone dedup on (scope, title, date)
- memes type whitelist + 7d events_fts frequency gate
- memes pinned defaults by type
- daily passes structured affect (importance=5) into sonnet material
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from marrow import candidates, daily, storage


def _ev(conn, sid, ts, role, content):
    conn.execute(
        "INSERT INTO events(session_id,timestamp,role,content)"
        " VALUES(?,?,?,?)", (sid, ts, role, content),
    )


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    return conn


@pytest.fixture(autouse=True)
def _disable_cosine_layer(monkeypatch):
    """Tests in this file target the string/alias dedup layer. The cosine
    layer (added later for tasks/milestones/entities) is exercised in
    tests/test_semantic_dedup.py with explicit stubs. Force it off here so
    bge-m3 paraphrase scoring can't swallow rows that the string layer
    intends to insert (e.g. '阿屿' vs '阿屿新' score 0.87).
    """
    from marrow import semantic_dedup
    monkeypatch.setattr(
        semantic_dedup, "cosine_max", lambda conn, q, t: 0.0,
    )
    monkeypatch.setattr(
        semantic_dedup, "cosine_top_match", lambda conn, q, t: (-1, 0.0),
    )


# ── milestone dedup ─────────────────────────────────────────────────────────

def test_milestone_dedup_same_key_noop(db):
    raw = (
        "===MILESTONE_CAND===\n"
        "[{\"title\":\"trop pass\",\"scope\":\"me\","
        " \"date\":\"2026-05-16\","
        " \"description\":\"念念 cleared trop.\",\"conf\":0.9}]\n"
        "===END===\n"
    )
    n1 = candidates.write_milestone_cand(db, raw, "2026-05-16")
    n2 = candidates.write_milestone_cand(db, raw, "2026-05-16")
    assert n1 == 1 and n2 == 0
    cnt = db.execute(
        "SELECT COUNT(*) FROM milestones WHERE title='trop pass'"
        " AND scope='me' AND date='2026-05-16'"
    ).fetchone()[0]
    assert cnt == 1


# ── memes type whitelist ────────────────────────────────────────────────────

def test_memes_invalid_type_rejected(db):
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"鬼故事\",\"type\":\"phrase\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw)
    assert n == 0
    row = db.execute("SELECT 1 FROM memes WHERE key='鬼故事'").fetchone()
    assert row is None


# ── memes 7d frequency gate ─────────────────────────────────────────────────

def _seed_events_with_key(conn, key: str, count: int, date: str):
    """Insert `count` events containing `key` within the 7d window ending at
    `date`. Trigram FTS needs ≥3-char phrase for CN; key must be ≥3 chars."""
    base = dt.date.fromisoformat(date)
    for i in range(count):
        ts = (base - dt.timedelta(days=i % 6)).isoformat() + "T10:00:00Z"
        _ev(conn, f"s{i}", ts, "user", f"random {key} more random text {i}")
    conn.commit()


def test_memes_meme_type_freq_gate_drops_low_count(db):
    """type=meme + key with 0 events in last 7d → dropped."""
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"绝绝子xy\",\"type\":\"meme\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 0
    assert db.execute(
        "SELECT 1 FROM memes WHERE key='绝绝子xy'").fetchone() is None


def test_memes_meme_type_freq_gate_passes_high_count(db):
    """type=meme + key seen ≥3 times in 7d events → inserted."""
    _seed_events_with_key(db, "绝绝子yy", 4, "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"绝绝子yy\",\"type\":\"meme\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 1
    row = db.execute(
        "SELECT type, pinned FROM memes WHERE key='绝绝子yy'"
    ).fetchone()
    assert row["type"] == "meme" and row["pinned"] == 0


def test_memes_paw_freq_gate_drops_low_count(db):
    """type=paw — inside jokes must also be repeated ≥3 times in 7d events.
    A dyad-private catchphrase that only shows up once is noise, not a meme.
    """
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"绿茶豹\",\"type\":\"paw\","
        " \"value\":\"私\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 0
    assert db.execute(
        "SELECT 1 FROM memes WHERE key='绿茶豹'").fetchone() is None


def test_memes_paw_freq_gate_passes_high_count(db):
    """type=paw + key seen ≥3 times in 7d events → inserted (auto-pinned)."""
    _seed_events_with_key(db, "绿茶豹yy", 4, "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"绿茶豹yy\",\"type\":\"paw\","
        " \"value\":\"私\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 1
    row = db.execute(
        "SELECT type, pinned FROM memes WHERE key='绿茶豹yy'"
    ).fetchone()
    assert row["type"] == "paw" and row["pinned"] == 1


def test_memes_fact_requires_freq_gate(db):
    """type=fact now gated — 0 events → dropped."""
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"Plan tier\",\"type\":\"fact\","
        " \"value\":\"Max 5x\",\"context\":\"\","
        " \"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 0
    assert db.execute(
        "SELECT 1 FROM memes WHERE key='Plan tier'").fetchone() is None


def test_memes_fact_passes_freq_gate_with_3_days(db):
    """type=fact passes when key seen on ≥3 distinct days in 14d window."""
    _seed_events_with_key(db, "Plan tier", 4, "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"Plan tier\",\"type\":\"fact\","
        " \"value\":\"Max 5x\",\"context\":\"\","
        " \"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 1


def test_memes_others_requires_freq_gate(db):
    """type=others now gated — 0 events → dropped."""
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"some-edge\",\"type\":\"others\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.8}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 0
    assert db.execute(
        "SELECT 1 FROM memes WHERE key='some-edge'").fetchone() is None


def test_memes_others_passes_freq_gate_with_3_days(db):
    """type=others passes when key seen on ≥3 distinct days in 14d window."""
    _seed_events_with_key(db, "some-edge", 4, "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"some-edge\",\"type\":\"others\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.8}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 1


# ── memes pinned defaults by type ───────────────────────────────────────────

def test_memes_paw_auto_pinned(db):
    _seed_events_with_key(db, "大笨鸭子", 4, "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"大笨鸭子\",\"type\":\"paw\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    candidates.write_memes_cand(db, raw, date="2026-05-16")
    row = db.execute(
        "SELECT pinned FROM memes WHERE key='大笨鸭子'"
    ).fetchone()
    assert row["pinned"] == 1


def test_memes_fact_auto_pinned(db):
    _seed_events_with_key(db, "keep14", 4, "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"keep14\",\"type\":\"fact\","
        " \"value\":\"backup 14d rolling\",\"context\":\"\","
        " \"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    candidates.write_memes_cand(db, raw, date="2026-05-16")
    row = db.execute(
        "SELECT pinned FROM memes WHERE key='keep14'"
    ).fetchone()
    assert row is not None and row["pinned"] == 1


def test_memes_meme_default_unpinned(db):
    _seed_events_with_key(db, "小笼包yy", 4, "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"小笼包yy\",\"type\":\"meme\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.8}]\n"
        "===END===\n"
    )
    candidates.write_memes_cand(db, raw, date="2026-05-16")
    row = db.execute(
        "SELECT pinned FROM memes WHERE key='小笼包yy'"
    ).fetchone()
    assert row["pinned"] == 0


def test_memes_others_default_unpinned(db):
    _seed_events_with_key(db, "edge-kind", 4, "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"edge-kind\",\"type\":\"others\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.8}]\n"
        "===END===\n"
    )
    candidates.write_memes_cand(db, raw, date="2026-05-16")
    row = db.execute(
        "SELECT pinned FROM memes WHERE key='edge-kind'"
    ).fetchone()
    assert row is not None and row["pinned"] == 0


# ── daily includes structured affect block ──────────────────────────────────

class _RecordingLLM:
    def __init__(self, per_role=None):
        self.per_role = per_role or {}
        self.bodies: dict[str, str] = {}

    def call(self, role, body, *, tier="cheap"):
        self.bodies[role] = body
        return self.per_role.get(role, "—")


# ── memes 7d gate edge cases ────────────────────────────────────────────────

def test_memes_gate_short_cjk_key_uses_like_not_fts(db):
    """2-char CJK keys (野鸡) — FTS5 trigram tokenizer can't match phrases
    shorter than 3 chars and silently returns 0. The gate must fall back to
    LIKE for short keys, otherwise valid-but-short keys are auto-dropped.
    Here 4 events mention 野鸡 → gate must accept.
    """
    _seed_events_with_key(db, "野鸡", 4, "2026-05-16")
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"野鸡\",\"type\":\"paw\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 1


def test_memes_gate_excludes_events_older_than_14d(db):
    """Event 15 days before ref_date must NOT count toward the gate."""
    base = dt.date.fromisoformat("2026-05-16")
    # 4 hits, all 15+ days before the ref_date → all out of window.
    for i in range(4):
        ts = (base - dt.timedelta(days=15 + i)).isoformat() + "T10:00:00Z"
        _ev(db, f"old{i}", ts, "user", f"random oldmeme tail {i}")
    db.commit()
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"oldmeme\",\"type\":\"meme\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    n = candidates.write_memes_cand(db, raw, date="2026-05-16")
    assert n == 0


def test_memes_gate_two_then_three_days(db):
    """2 distinct days → drop; add a 3rd-day event → next run inserts once.
    Gate counts distinct calendar days, not raw events: a key only sticks
    once it earns ≥3 separate days of organic repetition.
    """
    _seed_events_with_key(db, "newmeme", 2, "2026-05-16")  # days 0 + -1
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"newmeme\",\"type\":\"meme\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    assert candidates.write_memes_cand(db, raw, date="2026-05-16") == 0
    # Add a 3rd distinct day → meme now lands.
    _ev(db, "s_d2", "2026-05-14T11:00:00Z", "user", "another newmeme line")
    db.commit()
    assert candidates.write_memes_cand(db, raw, date="2026-05-16") == 1
    row = db.execute(
        "SELECT use_count FROM memes WHERE key='newmeme'"
    ).fetchone()
    assert row is not None and row["use_count"] == 1


def test_memes_gate_same_day_repeats_dont_count(db):
    """5 events on the same calendar day → still fails the 3-day gate.
    Prevents a meme from sticking just because one session was chatty.
    """
    for i in range(5):
        _ev(db, f"s_same{i}", f"2026-05-16T{10+i:02d}:00:00Z",
            "user", f"chatter samedaymeme blah {i}")
    db.commit()
    raw = (
        "===MEMES_CAND===\n"
        "[{\"key\":\"samedaymeme\",\"type\":\"meme\","
        " \"value\":\"x\",\"context\":\"\",\"pinned\":0,\"conf\":0.9}]\n"
        "===END===\n"
    )
    assert candidates.write_memes_cand(db, raw, date="2026-05-16") == 0
    assert db.execute(
        "SELECT 1 FROM memes WHERE key='samedaymeme'"
    ).fetchone() is None


# ── bump_use_counts ─────────────────────────────────────────────────────────

def _seed_meme(conn, key, *, vtype="paw", status="active", use_count=0):
    cur = conn.execute(
        "INSERT INTO memes (type, key, value, use_count, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (vtype, key, "v", use_count, status),
    )
    conn.commit()
    return cur.lastrowid


def _uc(conn, mid):
    return conn.execute(
        "SELECT use_count FROM memes WHERE id=?", (mid,)
    ).fetchone()["use_count"]


def test_bump_use_counts_single_event_single_meme(db):
    mid = _seed_meme(db, "野鸡")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T01:00:00Z",
             "role": "user", "content": "又是一个野鸡 codex"}]
    n = candidates.bump_use_counts(db, rows)
    assert n == 1 and _uc(db, mid) == 1


def test_bump_use_counts_same_event_double_mention_counts_once(db):
    mid = _seed_meme(db, "野鸡")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T02:00:00Z",
             "role": "user", "content": "野鸡野鸡都是野鸡"}]
    n = candidates.bump_use_counts(db, rows)
    assert n == 1 and _uc(db, mid) == 1


def test_bump_use_counts_multiple_events_accumulate(db):
    mid = _seed_meme(db, "野鸡")
    rows = [
        {"session_id": "s1", "timestamp": "2026-05-17T03:00:00Z",
         "role": "user", "content": "野鸡 one"},
        {"session_id": "s1", "timestamp": "2026-05-17T03:01:00Z",
         "role": "assistant", "content": "野鸡 two"},
    ]
    candidates.bump_use_counts(db, rows)
    assert _uc(db, mid) == 2


def test_bump_use_counts_non_matching_no_bump(db):
    mid = _seed_meme(db, "野鸡")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T04:00:00Z",
             "role": "user", "content": "完全没有这个词"}]
    candidates.bump_use_counts(db, rows)
    assert _uc(db, mid) == 0


def test_bump_use_counts_skips_non_user_assistant(db):
    mid = _seed_meme(db, "野鸡")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T05:00:00Z",
             "role": "system", "content": "野鸡 system noise"}]
    candidates.bump_use_counts(db, rows)
    assert _uc(db, mid) == 0


def test_bump_use_counts_case_insensitive(db):
    mid = _seed_meme(db, "Codex")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T06:00:00Z",
             "role": "user", "content": "tried codex today"}]
    candidates.bump_use_counts(db, rows)
    assert _uc(db, mid) == 1


def test_bump_use_counts_dormant_meme_skipped(db):
    mid = _seed_meme(db, "野鸡", status="dormant")
    rows = [{"session_id": "s1", "timestamp": "2026-05-17T07:00:00Z",
             "role": "user", "content": "野鸡 again"}]
    candidates.bump_use_counts(db, rows)
    assert _uc(db, mid) == 0


def test_archive_events_bumps_use_count_end_to_end(db):
    """Real wiring via repo.archive_events — single inserted event matching
    a seeded meme key must bump use_count.
    """
    from marrow import repo
    mid = _seed_meme(db, "野鸡", use_count=5)
    rows = [{"session_id": "s_wire", "timestamp": "2026-05-17T08:00:00Z",
             "role": "user", "content": "又来一只野鸡"}]
    repo.archive_events(db, rows)
    assert _uc(db, mid) == 6


def test_archive_events_idempotent_rerun_no_double_bump(db):
    """Re-archiving the same event (dedup by source_hash) must NOT re-bump."""
    from marrow import repo
    mid = _seed_meme(db, "野鸡", use_count=0)
    rows = [{"session_id": "s_idem", "timestamp": "2026-05-17T09:00:00Z",
             "role": "user", "content": "野鸡 only once"}]
    repo.archive_events(db, rows)
    repo.archive_events(db, rows)
    assert _uc(db, mid) == 1


def test_daily_material_includes_importance5_affect_ep(tmp_path):
    p = str(tmp_path / "aff.db")
    conn = storage.init_db(p)
    conn.execute(
        "INSERT INTO affect(date,ep,valence,arousal,importance,label,"
        "description) VALUES('2026-05-16',1,0.2,0.85,5,'窘迫',"
        "'工签签证被拒')"
    )
    conn.execute(
        "INSERT INTO session_digests (sid,date,text,ts)"
        " VALUES ('s1','2026-05-16','content for the day','2026-05-16T10:00Z')"
    )
    conn.commit()
    fake = _RecordingLLM(per_role={"daily": "diary text",
                                   "daily_cand": ""})
    assert daily.run_day(conn, "2026-05-16", fake, db=p) is True
    body = fake.bodies.get("daily_cand", "")
    assert "AFFECT episodes for 2026-05-16:" in body
    # New format: "- epl5 [label] description" (side+importance, no importance= key)
    assert "epl5" in body  # valence 0.2 < 0.5 → epl; importance=5
    assert "窘迫" in body
    assert "工签签证被拒" in body
    # diary call also receives the same material
    assert "epl5" in fake.bodies.get("daily", "")


# ── entity alias-aware dedup ────────────────────────────────────────────────

def _entity_raw(name, kind="person", aliases=None, conf=0.9, note=None):
    import json as _j
    obj = {"name": name, "kind": kind, "conf": conf}
    if aliases is not None:
        obj["aliases"] = aliases
    if note is not None:
        obj["note"] = note
    return ("===ENTITY_CAND===\n" + _j.dumps([obj], ensure_ascii=False)
            + "\n===END===\n")


def test_entity_new_name_hits_existing_alias_skips_and_merges(db):
    # Seed: row(name=阿屿, aliases=[屿忱, Stellan])
    candidates.write_entity_cand(
        db, _entity_raw("阿屿", aliases=["屿忱", "Stellan"]))
    n = candidates.write_entity_cand(
        db, _entity_raw("屿忱", aliases=["小屿"]))
    assert n == 0  # no new insert
    rows = db.execute(
        "SELECT name, aliases FROM entities WHERE kind='person'"
    ).fetchall()
    assert len(rows) == 1
    aliases = json.loads(rows[0]["aliases"])
    # 小屿 merged; 屿忱 already in aliases (dedup); name 阿屿 unchanged
    assert rows[0]["name"] == "阿屿"
    assert "小屿" in aliases
    assert aliases.count("屿忱") == 1


def test_entity_new_aliases_contain_existing_name_skips_and_merges(db):
    candidates.write_entity_cand(db, _entity_raw("阿屿"))
    n = candidates.write_entity_cand(
        db, _entity_raw("屿忱", aliases=["阿屿", "Stellan"]))
    assert n == 0
    rows = db.execute(
        "SELECT name, aliases FROM entities WHERE kind='person'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "阿屿"
    aliases = json.loads(rows[0]["aliases"])
    assert "屿忱" in aliases and "Stellan" in aliases
    assert "阿屿" not in aliases  # canonical name not duplicated into aliases


def test_entity_unrelated_name_inserts_new_row(db):
    candidates.write_entity_cand(
        db, _entity_raw("阿屿", aliases=["屿忱"]))
    n = candidates.write_entity_cand(
        db, _entity_raw("陈奶奶", aliases=["邻居陈"]))
    assert n == 1
    cnt = db.execute(
        "SELECT COUNT(*) FROM entities WHERE kind='person'"
    ).fetchone()[0]
    assert cnt == 2


def test_entity_dedup_case_insensitive(db):
    candidates.write_entity_cand(
        db, _entity_raw("Stellan", aliases=["屿忱"]))
    n = candidates.write_entity_cand(
        db, _entity_raw("stellan", aliases=["雪狼"]))
    assert n == 0
    rows = db.execute(
        "SELECT name, aliases FROM entities WHERE kind='person'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "Stellan"
    aliases = json.loads(rows[0]["aliases"])
    assert "雪狼" in aliases


def test_entity_dedup_scoped_by_kind(db):
    # Same name in different kinds is a legitimate distinct entity.
    candidates.write_entity_cand(
        db, _entity_raw("Bendigo", kind="place"))
    n = candidates.write_entity_cand(
        db, _entity_raw("Bendigo", kind="pref"))
    assert n == 1
    cnt = db.execute(
        "SELECT COUNT(*) FROM entities WHERE name='Bendigo'"
    ).fetchone()[0]
    assert cnt == 2


def test_entity_superseded_row_does_not_block_insert(db):
    # Seed two rows: row1=阿屿(aliases=屿忱), row2=阿屿新; mark row1 superseded by row2.
    candidates.write_entity_cand(db, _entity_raw("阿屿", aliases=["屿忱"]))
    candidates.write_entity_cand(db, _entity_raw("阿屿新"))
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM entities WHERE kind='person' ORDER BY id").fetchall()]
    db.execute("UPDATE entities SET superseded_by=? WHERE id=?",
               (ids[1], ids[0]))
    db.commit()
    n = candidates.write_entity_cand(db, _entity_raw("屿忱"))
    assert n == 1  # superseded row should not gate fresh insert
