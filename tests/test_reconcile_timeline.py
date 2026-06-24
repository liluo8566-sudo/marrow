"""Tests for reconcile_timeline — anchor presence/hidden sweep and aff deletion semantics.

Covers:
- Present session anchor → unchanged count (no tl_line write-back since Phase 5)
- Present diary anchor → unchanged count (no tl_line write-back since Phase 5)
- Delete a tl line → no-op (deleted line = no-op, next render restores)
- Unchanged text → unchanged count, no DB write
- Unknown sid → conflict reported
- aff:ids deletion: pending-row deletion marks resolved (existing behaviour)
- aff:ids deletion: two episodes on one line, delete one → only that id superseded
  (deferred full implementation; test documents current behaviour)
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from marrow import dashboard, storage
import marrow.reconcile as reconcile_mod
from marrow.reconcile import reconcile_timeline, ReconcileReport


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def conn(tmp_path):
    db = str(tmp_path / "rt.db")
    c = storage.init_db(db)
    yield c
    c.close()


@pytest.fixture()
def dash_path(tmp_path) -> Path:
    return tmp_path / "dashboard.md"


# ── helpers ──────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_digest(
    conn, sid: str, tl: str | None = "原始TL", segment_seq: int = 0
) -> str:
    ts = _now_utc()
    conn.execute(
        "INSERT INTO session_digests"
        " (sid, segment_seq, date, ts, text, kind, tl_line)"
        " VALUES (?, ?, ?, ?, 'body', 'casual', ?)",
        (sid, segment_seq, ts[:10], ts, tl),
    )
    conn.commit()
    return ts


def _insert_diary(conn, date: str, tl: str | None = "日记TL") -> None:
    conn.execute(
        "INSERT INTO diary (date, content, tl_line) VALUES (?, 'body', ?)",
        (date, tl),
    )
    conn.commit()


def _make_timeline_block(sid: str, tl: str, date: str | None = None,
                         diary_tl: str | None = None) -> str:
    """Build a minimal ## Timeline block with anchors."""
    lines = ["## Timeline", f"14:00 {tl} <!-- tl:{sid} -->"]
    if date and diary_tl:
        lines.append(f"06-07 Day 【平淡】 {diary_tl} <!-- tl:d:{date} -->")
    return "\n".join(lines)


def _freeze_reconcile_now(monkeypatch, melb_dt: _dt.datetime) -> None:
    class FrozenDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return melb_dt.replace(tzinfo=None)
            return melb_dt.astimezone(tz)

    utc_iso = melb_dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(reconcile_mod._dt, "datetime", FrozenDateTime)
    monkeypatch.setattr(reconcile_mod, "_now", lambda: utc_iso)


# ── session anchor presence (tl_line write-back removed in Phase 5) ───────────

def test_reconcile_tl_session_anchor_present_counts_unchanged(conn, dash_path):
    """Present session anchor → unchanged count; tl_line not mutated (Phase 5)."""
    sid = "sid-edit-1"
    _insert_digest(conn, sid, tl="原始TL")
    dash_path.write_text(_make_timeline_block(sid, "用户修改的TL"))

    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated == 0
    assert rpt.unchanged >= 1
    row = conn.execute(
        "SELECT tl_line FROM session_digests WHERE sid=?", (sid,)
    ).fetchone()
    assert row["tl_line"] == "原始TL"  # unchanged — write-back removed


def test_reconcile_tl_session_edit_composite_marker(conn, dash_path):
    """Present <!-- tl:sid:1 --> anchor → unchanged count; no tl_line mutation."""
    sid = "sid-edit-segment"
    _insert_digest(conn, sid, tl="seq0", segment_seq=0)
    _insert_digest(conn, sid, tl="seq1", segment_seq=1)
    dash_path.write_text(
        "## Timeline\n14:00 segment edit <!-- tl:sid-edit-segment:1 -->"
    )

    rpt = reconcile_timeline(conn, dash_path)

    assert rpt.updated == 0
    rows = conn.execute(
        "SELECT segment_seq, tl_line FROM session_digests"
        " WHERE sid=? ORDER BY segment_seq",
        (sid,),
    ).fetchall()
    assert [(r["segment_seq"], r["tl_line"]) for r in rows] == [
        (0, "seq0"),
        (1, "seq1"),
    ]


def test_reconcile_tl_session_edit_legacy_marker_uses_seq_zero(conn, dash_path):
    """Present <!-- tl:sid --> anchor → unchanged count; backward-compat lookup still works."""
    sid = "sid-edit-legacy"
    _insert_digest(conn, sid, tl="seq0", segment_seq=0)
    dash_path.write_text(
        "## Timeline\n14:00 legacy edit <!-- tl:sid-edit-legacy -->"
    )

    rpt = reconcile_timeline(conn, dash_path)

    assert rpt.updated == 0
    assert rpt.unchanged >= 1
    row = conn.execute(
        "SELECT tl_line FROM session_digests WHERE sid=? AND segment_seq=0",
        (sid,),
    ).fetchone()
    assert row["tl_line"] == "seq0"  # unchanged


def test_reconcile_tl_session_unchanged(conn, dash_path):
    """Present anchor → unchanged counter incremented, no DB update."""
    sid = "sid-unchanged"
    _insert_digest(conn, sid, tl="原始TL")
    dash_path.write_text(_make_timeline_block(sid, "原始TL"))

    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated == 0
    assert rpt.unchanged >= 1


def test_reconcile_tl_unknown_sid(conn, dash_path):
    """Unknown sid → conflict reported, no crash."""
    dash_path.write_text(
        "## Timeline\n14:00 text <!-- tl:nonexistent-sid -->"
    )
    rpt = reconcile_timeline(conn, dash_path)
    assert any("nonexistent-sid" in c for c in rpt.conflicts)


def test_reconcile_tl_audit_row_not_written(conn, dash_path):
    """No audit_log row for tl_edit since write-back is removed (Phase 5)."""
    sid = "sid-audit"
    _insert_digest(conn, sid, tl="旧的TL")
    dash_path.write_text(_make_timeline_block(sid, "新的TL"))

    reconcile_timeline(conn, dash_path)
    row = conn.execute(
        "SELECT summary FROM audit_log WHERE action='tl_edit' AND target_id=?",
        (sid,),
    ).fetchone()
    assert row is None


# ── diary anchor presence (tl_line write-back removed in Phase 5) ────────────

def test_reconcile_tl_diary_edit(conn, dash_path):
    """Present diary anchor → unchanged count; diary.tl_line not mutated (Phase 5)."""
    date = "2026-06-07"
    sid = "sid-diary-test"
    _insert_digest(conn, sid)
    _insert_diary(conn, date, tl="原始日记TL")
    dash_path.write_text(
        _make_timeline_block(sid, "聊天了", date=date, diary_tl="新日记TL")
    )

    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated == 0
    assert rpt.unchanged >= 1
    row = conn.execute(
        "SELECT tl_line FROM diary WHERE date=?", (date,)
    ).fetchone()
    assert row["tl_line"] == "原始日记TL"  # unchanged — write-back removed


def test_reconcile_tl_diary_unknown_date(conn, dash_path):
    """Diary date not in DB → conflict reported."""
    dash_path.write_text(
        "## Timeline\n06-07 Day 【平淡】 missing diary <!-- tl:d:2026-06-07 -->"
    )
    rpt = reconcile_timeline(conn, dash_path)
    assert any("2026-06-07" in c for c in rpt.conflicts)


# ── prefix-only / tone-tag lines must not write back ─────────────────────────

def test_reconcile_tl_stub_day_line_no_writeback(conn, dash_path):
    """Prefix-only stub day line (NULL tl_line) strips to empty → no write-back."""
    date = "2026-06-07"
    _insert_diary(conn, date, tl=None)
    dash_path.write_text(
        f"## Timeline\n06-07 Day 【平淡】 <!-- tl:d:{date} -->"
    )
    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated == 0
    row = conn.execute(
        "SELECT tl_line FROM diary WHERE date=?", (date,)
    ).fetchone()
    assert row["tl_line"] is None


def test_reconcile_tl_tone_tagged_line_no_writeback(conn, dash_path):
    """Tone-tagged anchor counts as unchanged; tl_line not mutated (Phase 5)."""
    sid = "sid-tone"
    _insert_digest(conn, sid, tl="原始TL")
    dash_path.write_text(
        f"## Timeline\n17:46【释怀】 用户改的TL <!-- tl:{sid} -->"
    )
    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated == 0
    row = conn.execute(
        "SELECT tl_line FROM session_digests WHERE sid=?", (sid,)
    ).fetchone()
    assert row["tl_line"] == "原始TL"  # unchanged


def test_reconcile_tl_tone_only_line_no_writeback(conn, dash_path):
    """Tone-only anchor counts as unchanged; tl_line not mutated (Phase 5)."""
    sid = "sid-tone-only"
    _insert_digest(conn, sid, tl="原始TL")
    dash_path.write_text(f"## Timeline\n17:46【释怀】 <!-- tl:{sid} -->")
    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated == 0
    assert rpt.unchanged >= 1
    row = conn.execute(
        "SELECT tl_line FROM session_digests WHERE sid=?", (sid,)
    ).fetchone()
    assert row["tl_line"] == "原始TL"  # unchanged


# ── deleted line = no-op ─────────────────────────────────────────────────────

def test_reconcile_tl_deleted_line_noop(conn, dash_path):
    """A tl line absent from md (deleted) → no DB change (next render restores)."""
    sid = "sid-deleted"
    _insert_digest(conn, sid, tl="要被删的TL")
    # Block has no tl anchor for this sid
    dash_path.write_text("## Timeline\n_none_")

    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated == 0
    row = conn.execute(
        "SELECT tl_line FROM session_digests WHERE sid=?", (sid,)
    ).fetchone()
    assert row["tl_line"] == "要被删的TL"  # unchanged


# ── no timeline block ─────────────────────────────────────────────────────────

def test_reconcile_tl_no_block(conn, dash_path):
    """No ## Timeline block in md → no-op."""
    dash_path.write_text("## Alerts\n_none_\n\n## Tasks\n_none_")
    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated == 0
    assert rpt.conflicts == []


def test_reconcile_tl_no_file(conn, dash_path):
    """Missing md file → no-op."""
    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated == 0


# ── aff pending-row deletion (existing behaviour, regression) ────────────────

def test_aff_pending_deletion_marks_resolved(conn, tmp_path):
    """Deleting a Pending row from ## Affect block marks it resolved=0→
    unresolved=0 (existing behaviour, should not regress)."""
    state = tmp_path / "s"
    dash = tmp_path / "dashboard.md"
    ts = _now_utc()
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label,"
        " description, source, unresolved, created_at)"
        " VALUES (?, 1, 0.2, 0.7, 3, '委屈', '等回复', 'test', 1, ?)",
        (ts[:10], ts),
    )
    conn.commit()

    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    content = dash.read_text()
    assert "等回复" in content

    # Delete the Pending bullet
    lines = [ln for ln in content.splitlines() if "等回复" not in ln]
    dash.write_text("\n".join(lines))

    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    row = conn.execute(
        "SELECT unresolved FROM affect WHERE description='等回复'"
    ).fetchone()
    assert row["unresolved"] == 0


# ── episode text edit (existing behaviour, regression) ───────────────────────

def test_aff_episode_text_edit_survives(conn, tmp_path):
    """Editing an ep description in ## Affect block writes back to DB
    and survives subsequent renders (regression guard)."""
    state = tmp_path / "s"
    dash = tmp_path / "dashboard.md"
    ts = _now_utc()
    conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, label,"
        " description, source, created_at)"
        " VALUES (?, 1, 0.7, 0.5, 3, '开心', '项目过审', 'test', ?)",
        (ts[:10], ts),
    )
    conn.commit()

    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    content = dash.read_text()
    assert "项目过审" in content

    # Edit the description in md
    edited = content.replace("项目过审", "大项目过审了")
    dash.write_text(edited)

    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    row = conn.execute(
        "SELECT description FROM affect WHERE label='开心'"
    ).fetchone()
    assert row["description"] == "大项目过审了"


# ── tl_hidden migration idempotent ───────────────────────────────────────────

def test_migration_tl_hidden_columns(conn):
    cols_sd = {r[1] for r in conn.execute("PRAGMA table_info(session_digests)")}
    cols_d  = {r[1] for r in conn.execute("PRAGMA table_info(diary)")}
    assert "tl_hidden" in cols_sd
    assert "tl_hidden" in cols_d
    import marrow.storage as _s
    _s._migrate_to_v18(conn)  # must not raise


def test_delete_sid_line_sets_hidden(conn, dash_path):
    sid = "sid-del-1"
    _insert_digest(conn, sid, tl="要删的TL")
    dash_path.write_text(f"## Timeline\n_none_\n<!-- tl-rendered:s={sid} -->")
    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated >= 1
    row = conn.execute("SELECT tl_hidden FROM session_digests WHERE sid=?", (sid,)).fetchone()
    assert row["tl_hidden"] == 1


def test_delete_sid_hidden_excludes_from_render(conn):
    from marrow import timeline
    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO session_digests (sid, date, ts, text, kind, tl_line, tl_hidden)"
        " VALUES (?, ?, ?, 'body', 'casual', '隐藏TL', 1)",
        ("sid-hidden", ts[:10], ts),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    assert "隐藏TL" not in result
    assert "sid-hidden" not in result


def test_delete_diary_line_sets_hidden(conn, dash_path):
    date = "2026-06-01"
    _insert_diary(conn, date, tl="日记TL")
    dash_path.write_text(f"## Timeline\n_none_\n<!-- tl-rendered:d={date} -->")
    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated >= 1
    row = conn.execute("SELECT tl_hidden FROM diary WHERE date=?", (date,)).fetchone()
    assert row["tl_hidden"] == 1


def test_add_plus_line_with_time_inserts_event(conn, dash_path):
    dash_path.write_text("## Timeline\n+ 14:30 下午喝了咖啡")
    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated >= 1
    row = conn.execute(
        "SELECT id, content, channel, session_id, timestamp FROM events WHERE channel='manual'"
    ).fetchone()
    assert row is not None
    assert row["content"] == "下午喝了咖啡"
    assert row["channel"] == "manual"
    assert row["session_id"].startswith("manual:")
    assert "T" in row["timestamp"]
    from zoneinfo import ZoneInfo
    import datetime as _dt
    tz = ZoneInfo("Australia/Melbourne")
    ts = _dt.datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
    melb_ts = ts.astimezone(tz)
    assert melb_ts.hour == 14
    assert melb_ts.minute == 30


def test_add_plus_line_future_time_rolls_back_one_day(conn, dash_path):
    """Backdating: a future-resolving HH:MM means the previous day."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Australia/Melbourne")
    now_melb = _dt.datetime.now(tz)
    future = now_melb + _dt.timedelta(hours=1)
    hhmm = future.strftime("%H:%M")
    dash_path.write_text(f"## Timeline\n+ {hhmm} 补记昨晚的事")
    reconcile_timeline(conn, dash_path)
    row = conn.execute(
        "SELECT timestamp FROM events WHERE channel='manual'"
    ).fetchone()
    assert row is not None
    ts = _dt.datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
    assert ts <= _dt.datetime.now(_dt.timezone.utc)
    melb_ts = ts.astimezone(tz)
    assert (melb_ts.hour, melb_ts.minute) == (future.hour, future.minute)


def test_add_plus_line_without_time_uses_now(conn, dash_path):
    import datetime as _dt
    # _now() truncates to seconds; truncate before/after to match
    before = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    dash_path.write_text("## Timeline\n+ 随手记录一句话")
    reconcile_timeline(conn, dash_path)
    row = conn.execute("SELECT timestamp FROM events WHERE channel='manual'").fetchone()
    assert row is not None
    ts = _dt.datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
    after = _dt.datetime.now(_dt.timezone.utc)
    assert before <= ts <= after


def test_add_plus_line_uses_day_divider_context(conn, dash_path, monkeypatch):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Australia/Melbourne")
    _freeze_reconcile_now(
        monkeypatch, _dt.datetime(2026, 6, 13, 22, 50, tzinfo=tz)
    )
    dash_path.write_text(
        "## Timeline\n"
        "--- 06-12 ---\n"
        "+AM 吃饭\n"
        "+ 14:30 咖啡"
    )

    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated == 2
    rows = conn.execute(
        "SELECT content, timestamp FROM events WHERE channel='manual'"
        " ORDER BY id"
    ).fetchall()
    got = {r["content"]: r["timestamp"] for r in rows}
    assert got["吃饭"] == "2026-06-11T23:00:00Z"
    assert got["咖啡"] == "2026-06-12T04:30:00Z"


def test_add_plus_line_top_without_time_uses_now(conn, dash_path, monkeypatch):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Australia/Melbourne")
    _freeze_reconcile_now(
        monkeypatch, _dt.datetime(2026, 6, 13, 22, 50, tzinfo=tz)
    )
    dash_path.write_text("## Timeline\n+ 随手记\n--- 06-12 ---")

    reconcile_timeline(conn, dash_path)
    row = conn.execute(
        "SELECT content, timestamp FROM events WHERE channel='manual'"
    ).fetchone()
    assert row["content"] == "随手记"
    assert row["timestamp"] == "2026-06-13T12:50:00Z"


def test_add_plus_line_uses_full_date_anchor_context(conn, dash_path, monkeypatch):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Australia/Melbourne")
    _freeze_reconcile_now(
        monkeypatch, _dt.datetime(2026, 6, 13, 22, 50, tzinfo=tz)
    )
    dash_path.write_text(
        "## Timeline\n"
        "<!-- tl:d:2026-06-09 -->\n"
        "+ND 宵夜"
    )

    reconcile_timeline(conn, dash_path)
    row = conn.execute(
        "SELECT content, timestamp FROM events WHERE channel='manual'"
    ).fetchone()
    assert row["content"] == "宵夜"
    assert row["timestamp"] == "2026-06-09T11:00:00Z"


def test_add_plus_line_yearless_context_uses_recent_past_year(
    conn, dash_path, monkeypatch
):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Australia/Melbourne")
    _freeze_reconcile_now(
        monkeypatch, _dt.datetime(2026, 6, 13, 22, 50, tzinfo=tz)
    )
    dash_path.write_text("## Timeline\n--- 01-15 ---\n+PM 旧事")

    reconcile_timeline(conn, dash_path)
    row = conn.execute(
        "SELECT content, timestamp FROM events WHERE channel='manual'"
    ).fetchone()
    assert row["content"] == "旧事"
    assert row["timestamp"] == "2026-01-15T04:00:00Z"


def test_manual_event_appears_in_render(conn, dash_path):
    from marrow import timeline
    import datetime as _dt
    ts_utc = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel)"
        " VALUES ('manual:aabbccdd', ?, 'user', '手动笔记测试', 'manual')",
        (ts_utc,),
    )
    conn.commit()
    eid = conn.execute("SELECT id FROM events WHERE channel='manual'").fetchone()["id"]
    result = timeline.render_timeline(conn)
    assert "手动笔记测试" in result
    assert f"<!-- tl:e:{eid} -->" in result


def test_edit_manual_event_updates_content(conn, dash_path):
    import datetime as _dt
    ts_utc = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel)"
        " VALUES ('manual:aabbccdd', ?, 'user', '原始内容', 'manual')",
        (ts_utc,),
    )
    conn.commit()
    eid = conn.execute("SELECT id FROM events WHERE channel='manual'").fetchone()["id"]
    dash_path.write_text(f"## Timeline\n14:00 修改后的内容 <!-- tl:e:{eid} -->")
    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated >= 1
    row = conn.execute("SELECT content FROM events WHERE id=?", (eid,)).fetchone()
    assert row["content"] == "修改后的内容"


def test_delete_manual_event_line_removes_row(conn, dash_path):
    import datetime as _dt
    ts_utc = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel)"
        " VALUES ('manual:aabbccdd', ?, 'user', '要删的手动事件', 'manual')",
        (ts_utc,),
    )
    conn.commit()
    eid = conn.execute("SELECT id FROM events WHERE channel='manual'").fetchone()["id"]
    dash_path.write_text(f"## Timeline\n_none_\n<!-- tl-rendered:e={eid} -->")
    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated >= 1
    row = conn.execute("SELECT id FROM events WHERE id=?", (eid,)).fetchone()
    assert row is None


def test_round_trip_no_reingest(conn, dash_path):
    from marrow import timeline
    import datetime as _dt
    ts_utc = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel)"
        " VALUES ('manual:test0001', ?, 'user', '测试循环', 'manual')",
        (ts_utc,),
    )
    conn.commit()
    rendered = timeline.render_timeline(conn)
    dash_path.write_text(rendered)
    count_before = conn.execute("SELECT count(*) FROM events WHERE channel='manual'").fetchone()[0]
    reconcile_timeline(conn, dash_path)
    count_after = conn.execute("SELECT count(*) FROM events WHERE channel='manual'").fetchone()[0]
    assert count_after == count_before


def test_trail_marker_present_in_render(conn):
    from marrow import timeline
    import datetime as _dt
    # Insert 5s in the past so it falls within the 24h window (strict < now)
    ts_utc = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO session_digests (sid, date, ts, text, kind, tl_line)"
        " VALUES ('sid-trail', ?, ?, 'body', 'casual', 'TL行')",
        (ts_utc[:10], ts_utc),
    )
    conn.commit()
    result = timeline.render_timeline(conn)
    assert "<!-- tl-rendered:" in result
    assert "sid-trail" in result


def test_delete_without_trail_is_noop(conn, dash_path):
    sid = "sid-legacy"
    _insert_digest(conn, sid, tl="遗留TL")
    dash_path.write_text("## Timeline\n_none_")
    rpt = reconcile_timeline(conn, dash_path)
    row = conn.execute("SELECT tl_hidden FROM session_digests WHERE sid=?", (sid,)).fetchone()
    assert row["tl_hidden"] == 0
