"""Tests for reconcile_affect — md description/label edits flow to DB.

Coverage:
- Anchored row with edited description → affect.description UPDATE.
- Anchored row with edited label → affect.label UPDATE.
- End-to-end: edit affect in md, write_dashboard reconciles + re-renders;
  Lumi's text survives.
- New affect row from DB appears in next render even when Lumi has edits.
- No-op when block has no anchored lines (cold start).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from marrow import dashboard, reconcile, storage, top_sections


@pytest.fixture()
def conn(tmp_path):
    c = storage.init_db(str(tmp_path / "t.db"))
    yield c
    c.close()


def _insert_affect(conn, *, date: str, ep: int, v: float, a: float,
                    importance: int, label: str, description: str,
                    created_at: str | None = None) -> int:
    ts = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "INSERT INTO affect (date, ep, valence, arousal, importance, "
        "label, description, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (date, ep, v, a, importance, label, description, ts),
    )
    conn.commit()
    return cur.lastrowid


def _replace_in_anchored(text: str, aid: int, old_sub: str, new_sub: str) -> str:
    """Edit the bullet line whose NEXT line's trail marker contains `aid`.
    Pending rows fall back to the inline `<!-- id:affect.N -->` anchor on
    the same line. Mirrors the new render layout (anchor lives below).
    """
    inline_needle = f"<!-- id:affect.{aid} -->"
    trail_needle = re.compile(r"<!--\s*aff:([0-9,\s]*)-->")
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if i + 1 < len(lines):
            m = trail_needle.search(lines[i + 1])
            if m:
                ids = [t.strip() for t in m.group(1).split(",") if t.strip()]
                if str(aid) in ids and old_sub in ln:
                    ln = ln.replace(old_sub, new_sub, 1)
        if inline_needle in ln and old_sub in ln:
            ln = ln.replace(old_sub, new_sub, 1)
        out.append(ln)
        i += 1
    return "\n".join(out)


def test_description_edit_updates_db(conn, tmp_path):
    today = datetime.now(timezone.utc).date().isoformat()
    aid = _insert_affect(conn, date=today, ep=1, v=0.7, a=0.7, importance=3,
                          label="开心", description="项目过审")
    dash = tmp_path / "dashboard.md"
    dashboard.write_dashboard(str(dash), conn, state_dir=str(tmp_path / "s"))
    text = dash.read_text()
    # 项目过审 → 论文过审
    edited = _replace_in_anchored(text, aid, "项目过审", "论文过审")
    dash.write_text(edited)

    rpt = reconcile.reconcile_affect(conn, dash)
    assert rpt.updated == 1
    row = conn.execute(
        "SELECT description, label FROM affect WHERE id=?", (aid,)
    ).fetchone()
    assert row["description"] == "论文过审"
    assert row["label"] == "开心"


def test_label_edit_updates_db(conn, tmp_path):
    today = datetime.now(timezone.utc).date().isoformat()
    aid = _insert_affect(conn, date=today, ep=1, v=0.7, a=0.7, importance=3,
                          label="开心", description="项目过审")
    dash = tmp_path / "dashboard.md"
    dashboard.write_dashboard(str(dash), conn, state_dir=str(tmp_path / "s"))
    text = dash.read_text()
    edited = _replace_in_anchored(text, aid, "开心 |", "雀跃 |")
    dash.write_text(edited)

    reconcile.reconcile_affect(conn, dash)
    row = conn.execute(
        "SELECT label, description FROM affect WHERE id=?", (aid,)
    ).fetchone()
    assert row["label"] == "雀跃"
    assert row["description"] == "项目过审"


def test_edit_survives_full_dashboard_refresh(conn, tmp_path):
    """Critical regression: edit affect in md, run write_dashboard → edit must
    survive in the re-rendered body (reconcile absorbs it, render reproduces)."""
    today = datetime.now(timezone.utc).date().isoformat()
    aid = _insert_affect(conn, date=today, ep=1, v=0.7, a=0.7, importance=3,
                          label="开心", description="项目过审")
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "s"
    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    text = dash.read_text()
    edited = _replace_in_anchored(text, aid, "项目过审", "论文过审 (lumi note)")
    dash.write_text(edited)

    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    result = dash.read_text()
    assert "论文过审 (lumi note)" in result
    assert "项目过审" not in result


def test_new_affect_appears_after_edit(conn, tmp_path):
    """Lumi edits one affect line; sessionend later inserts a new ep with
    higher importance → next render shows the new ep without clobbering her
    edit on the old one (the edit lives in DB, the new ep displaces it
    visually as the new eph but the OLD row's description carries her text)."""
    today = datetime.now(timezone.utc).date().isoformat()
    aid1 = _insert_affect(conn, date=today, ep=1, v=0.7, a=0.7, importance=3,
                           label="开心", description="原始描述")
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "s"
    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    edited = _replace_in_anchored(
        dash.read_text(), aid1, "原始描述", "lumi 改写的描述"
    )
    dash.write_text(edited)
    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    # Sessionend writes a new, higher-importance ep.
    later_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_affect(conn, date=today, ep=2, v=0.9, a=0.7, importance=5,
                    label="兴奋", description="新事件",
                    created_at=later_ts)
    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))

    result = dash.read_text()
    assert "新事件" in result, "new affect ep must surface"
    # Lumi's edit on the old row is preserved at the DB level.
    db_desc = conn.execute(
        "SELECT description FROM affect WHERE id=?", (aid1,)
    ).fetchone()["description"]
    assert db_desc == "lumi 改写的描述"


def test_reconcile_noop_when_no_anchors(conn, tmp_path):
    dash = tmp_path / "dashboard.md"
    dash.write_text("## Affect\n_none_\n## Content\n")
    rpt = reconcile.reconcile_affect(conn, dash)
    assert rpt.updated == 0 and rpt.unchanged == 0
    assert not rpt.conflicts


def test_render_each_ep_carries_trail_marker(conn, tmp_path):
    """Each bullet line is followed by a `<!-- aff:<ids> -->` trail marker
    whose id count matches the number of ep segments on the bullet, in
    left-to-right order.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    aid_h = _insert_affect(conn, date=today, ep=1, v=0.8, a=0.6, importance=3,
                            label="开心", description="A 事件")
    aid_l = _insert_affect(conn, date=today, ep=2, v=0.2, a=0.5, importance=3,
                            label="低落", description="B 事件")
    out = top_sections.render_affect(conn)
    lines = out.splitlines()
    # Bullet body MUST NOT carry the inline `<!-- id:affect.N -->` form anymore.
    bullets = [ln for ln in lines
                if ln.startswith("- 【") and ("eph" in ln or "epl" in ln)]
    assert bullets, "expected eph/epl bullets"
    for ln in bullets:
        assert "<!-- id:affect." not in ln, \
            f"inline anchor should have moved to trail line: {ln}"
    # For each bullet, the next line must be a trail marker covering each ep.
    trail_re = re.compile(r"<!--\s*aff:([0-9,\s]*)-->")
    for i, ln in enumerate(lines):
        if not ln.startswith("- 【") or ("eph" not in ln and "epl" not in ln):
            continue
        assert i + 1 < len(lines), f"bullet without trail line: {ln}"
        m = trail_re.search(lines[i + 1])
        assert m, f"missing trail marker after bullet: {ln} / {lines[i + 1]!r}"
        ids = [t.strip() for t in m.group(1).split(",") if t.strip()]
        eps = ln.count(" eph") + ln.count(" epl")
        assert len(ids) == eps, \
            f"trail ids ({ids}) must match seg count ({eps}) on bullet: {ln}"
    # Both affect ids appear in the trail markers across the rendered output.
    assert f"{aid_h}" in out and f"{aid_l}" in out
