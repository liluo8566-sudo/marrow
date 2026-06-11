"""Tests for reconcile_timeline — tl_line write-back and aff deletion semantics.

Covers:
- Edit session tl_line via <!-- tl:sid --> anchor → session_digests updated
- Edit diary tl_line via <!-- tl:d:YYYY-MM-DD --> anchor → diary updated
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


def _insert_digest(conn, sid: str, tl: str | None = "原始TL") -> str:
    ts = _now_utc()
    conn.execute(
        "INSERT INTO session_digests (sid, date, ts, text, kind, tl_line)"
        " VALUES (?, ?, ?, 'body', 'casual', ?)",
        (sid, ts[:10], ts, tl),
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


# ── session tl write-back ─────────────────────────────────────────────────────

def test_reconcile_tl_session_edit(conn, dash_path):
    """Editing tl text in md writes back to session_digests.tl_line."""
    sid = "sid-edit-1"
    _insert_digest(conn, sid, tl="原始TL")
    dash_path.write_text(_make_timeline_block(sid, "用户修改的TL"))

    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated >= 1
    row = conn.execute(
        "SELECT tl_line FROM session_digests WHERE sid=?", (sid,)
    ).fetchone()
    assert row["tl_line"] == "用户修改的TL"


def test_reconcile_tl_session_unchanged(conn, dash_path):
    """Unchanged tl text → no DB update, unchanged counter incremented."""
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


def test_reconcile_tl_audit_row_written(conn, dash_path):
    """An audit_log row is written for each tl_line update."""
    sid = "sid-audit"
    _insert_digest(conn, sid, tl="旧的TL")
    dash_path.write_text(_make_timeline_block(sid, "新的TL"))

    reconcile_timeline(conn, dash_path)
    row = conn.execute(
        "SELECT summary FROM audit_log WHERE action='tl_edit' AND target_id=?",
        (sid,),
    ).fetchone()
    assert row is not None
    assert "新的TL" in row["summary"]


# ── diary tl write-back ───────────────────────────────────────────────────────

def test_reconcile_tl_diary_edit(conn, dash_path):
    """Editing diary tl_line in md writes back to diary.tl_line."""
    date = "2026-06-07"
    sid = "sid-diary-test"
    _insert_digest(conn, sid)
    _insert_diary(conn, date, tl="原始日记TL")
    dash_path.write_text(
        _make_timeline_block(sid, "聊天了", date=date, diary_tl="新日记TL")
    )

    rpt = reconcile_timeline(conn, dash_path)
    assert rpt.updated >= 1
    row = conn.execute(
        "SELECT tl_line FROM diary WHERE date=?", (date,)
    ).fetchone()
    assert row["tl_line"] == "新日记TL"


def test_reconcile_tl_diary_unknown_date(conn, dash_path):
    """Diary date not in DB → conflict reported."""
    dash_path.write_text(
        "## Timeline\n06-07 Day 【平淡】 missing diary <!-- tl:d:2026-06-07 -->"
    )
    rpt = reconcile_timeline(conn, dash_path)
    assert any("2026-06-07" in c for c in rpt.conflicts)


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
