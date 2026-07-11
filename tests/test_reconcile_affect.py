"""Tests for reconcile_affect — md description/label edits flow to DB.

Coverage:
- Anchored row with edited description → affect.description UPDATE.
- Anchored row with edited label → affect.label UPDATE.
- End-to-end: edit affect in md, write_dashboard reconciles + re-renders;
  the user's text survives.
- New affect row from DB appears in next render even when the user has edits.
- No-op when block has no anchored lines (cold start).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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
    """Edit the bullet line whose inline `<!-- aff:... -->` anchor contains
    `aid`. Pending rows fall back to the per-row `<!-- id:affect.N -->`
    anchor on the same line. Mirrors the inline end-of-line render layout.
    """
    inline_needle = f"<!-- id:affect.{aid} -->"
    trail_needle = re.compile(r"<!--\s*aff:([0-9,\s]*)-->")
    out: list[str] = []
    for ln in text.splitlines():
        m = trail_needle.search(ln)
        if m:
            ids = [t.strip() for t in m.group(1).split(",") if t.strip()]
            if str(aid) in ids and old_sub in ln:
                ln = ln.replace(old_sub, new_sub, 1)
        if inline_needle in ln and old_sub in ln:
            ln = ln.replace(old_sub, new_sub, 1)
        out.append(ln)
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
    edited = _replace_in_anchored(text, aid, "项目过审", "论文过审 (user note)")
    dash.write_text(edited)

    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    result = dash.read_text()
    assert "论文过审 (user note)" in result
    assert "项目过审" not in result


def test_new_affect_appears_after_edit(conn, tmp_path):
    """User edits one affect line; sessionend later inserts a new ep with
    higher importance → next render shows the new ep without clobbering the
    edit on the old one (the edit lives in DB, the new ep displaces it
    visually as the new eph but the OLD row's description carries the
    user's text)."""
    today = datetime.now(timezone.utc).date().isoformat()
    aid1 = _insert_affect(conn, date=today, ep=1, v=0.7, a=0.7, importance=3,
                           label="开心", description="原始描述")
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "s"
    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    edited = _replace_in_anchored(
        dash.read_text(), aid1, "原始描述", "user 改写的描述"
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
    # User's edit on the old row is preserved at the DB level.
    db_desc = conn.execute(
        "SELECT description FROM affect WHERE id=?", (aid1,)
    ).fetchone()["description"]
    assert db_desc == "user 改写的描述"


def test_reconcile_noop_when_no_anchors(conn, tmp_path):
    dash = tmp_path / "dashboard.md"
    dash.write_text("## Affect\n_none_\n## Content\n")
    rpt = reconcile.reconcile_affect(conn, dash)
    assert rpt.updated == 0 and rpt.unchanged == 0
    assert not rpt.conflicts




def test_render_each_ep_carries_inline_anchor(conn, tmp_path):
    """Each bullet line carries an inline end-of-line `<!-- aff:<ids> -->`
    anchor whose id count matches the number of ep segments on the bullet,
    in left-to-right order. Parity with task `<!-- id:N -->` shape.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    aid_h = _insert_affect(conn, date=today, ep=1, v=0.8, a=0.6, importance=3,
                            label="开心", description="A 事件")
    aid_l = _insert_affect(conn, date=today, ep=2, v=0.2, a=0.5, importance=3,
                            label="低落", description="B 事件")
    out = top_sections.render_affect(conn)
    lines = out.splitlines()
    # Bullet body MUST NOT carry the per-row `<!-- id:affect.N -->` form here.
    bullets = [ln for ln in lines
                if ln.startswith("- 【") and ("eph" in ln or "epl" in ln)]
    assert bullets, "expected eph/epl bullets"
    trail_re = re.compile(r"<!--\s*aff:([0-9,\s]*)-->")
    for ln in bullets:
        assert "<!-- id:affect." not in ln, \
            f"per-row anchor should not appear on Today/Week bullets: {ln}"
        m = trail_re.search(ln)
        assert m, f"missing inline anchor on bullet: {ln}"
        # Anchor must be glued to end-of-line (only optional trailing space).
        assert ln.rstrip().endswith("-->"), \
            f"anchor must sit at end-of-line: {ln!r}"
        ids = [t.strip() for t in m.group(1).split(",") if t.strip()]
        eps = ln.count(" eph") + ln.count(" epl")
        assert len(ids) == eps, \
            f"anchor ids ({ids}) must match seg count ({eps}) on bullet: {ln}"
    assert f"{aid_h}" in out and f"{aid_l}" in out


# ── New coverage: single-ep side, dedup, rolling windows, sanitizer ─────────


def test_single_ep_low_valence_renders_epl(conn):
    """One ep with v < 0.5 must render as `epl…` (not forced to `eph`)."""
    today = datetime.now(timezone.utc).date().isoformat()
    _insert_affect(conn, date=today, ep=1, v=0.2, a=0.5, importance=3,
                    label="低落", description="只一个 ep")
    out = top_sections.render_affect(conn)
    today_block = out.split("### Today")[1].split("###")[0]
    assert " epl3 " in today_block, f"expected epl segment, got: {today_block!r}"
    assert " eph" not in today_block


def test_dedup_across_three_lines(conn):
    """Same id never appears in the trail-marker anchors of two lines.
    Line 1 picks 2 ids, Line 2 picks ids not in Line 1, Line 3 picks ids
    not in Line 1 or Line 2.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    base = datetime.now(timezone.utc)
    # Fan ids across staggered created_at within the 24h window so Line 1
    # (latest batch) only captures the most recent few, Lines 2/3 still have
    # rows to surface after dedup.
    for i in range(5):
        ts = (base - timedelta(hours=i + 1, minutes=10 * i)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        v = 0.85 if i % 2 == 0 else 0.15
        _insert_affect(conn, date=today, ep=i + 1, v=v, a=0.5, importance=3,
                        label=("雀跃" if v > 0.5 else "低落"),
                        description=f"事件 {i}", created_at=ts)
    out = top_sections.render_affect(conn)
    trail_re = re.compile(r"<!--\s*aff:([0-9,\s]*)-->")
    bullet_ids: list[set[int]] = []
    for ln in out.splitlines():
        m = trail_re.search(ln)
        if m:
            ids = {int(t.strip()) for t in m.group(1).split(",") if t.strip()}
            bullet_ids.append(ids)
    # Pairwise disjoint.
    for i in range(len(bullet_ids)):
        for j in range(i + 1, len(bullet_ids)):
            assert bullet_ids[i].isdisjoint(bullet_ids[j]), (
                f"line {i} {bullet_ids[i]} and line {j} {bullet_ids[j]} share "
                "an id; dedup broken"
            )


def test_rolling_7d_cutoff_excludes_old(conn):
    """A row created 10 days ago must not surface in any line."""
    today = datetime.now(timezone.utc).date().isoformat()
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    new_ts = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    aid_old = _insert_affect(conn, date=today, ep=1, v=0.8, a=0.5, importance=3,
                              label="远古", description="陈年旧事",
                              created_at=old_ts)
    aid_new = _insert_affect(conn, date=today, ep=2, v=0.7, a=0.5, importance=3,
                              label="新事", description="刚发生",
                              created_at=new_ts)
    out = top_sections.render_affect(conn)
    assert "陈年旧事" not in out, "10-day-old row must not appear"
    assert "刚发生" in out
    # Old id absent from any anchor; new id present.
    assert f"aff:{aid_old}" not in out and f",{aid_old}" not in out
    assert str(aid_new) in out


def test_rolling_24h_cutoff_excludes_yesterday(conn):
    """A row created 30 hours ago must not appear in Line 2 (24h window).
    It can still show up in Line 3 if within the 7d window.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    far_ts = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    near_ts = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    aid_far = _insert_affect(conn, date=today, ep=1, v=0.8, a=0.5, importance=3,
                              label="远", description="昨天的事",
                              created_at=far_ts)
    aid_near = _insert_affect(conn, date=today, ep=2, v=0.7, a=0.5, importance=3,
                               label="近", description="刚刚的事",
                               created_at=near_ts)
    out = top_sections.render_affect(conn)
    today_block = out.split("### Today")[1].split("### This Week")[0]
    week_block = out.split("### This Week")[1].split("### Pending")[0] \
        if "### Pending" in out else out.split("### This Week")[1]
    # Yesterday's row out of Today/24h.
    assert "昨天的事" not in today_block
    # But surfaces in the 7d Week line.
    assert str(aid_far) in week_block


def test_sanitizer_strips_anchor_and_tag_suffix():
    s = reconcile._sanitize_affect_text(
        "以为provider接口没做 [25m ago] <!-- aff:67 -->")
    assert s == "以为provider接口没做"

    s2 = reconcile._sanitize_affect_text(
        "演讲前夜 [24h] <!-- aff:1,2 -->")
    assert s2 == "演讲前夜"

    s3 = reconcile._sanitize_affect_text(
        "干净文本 <!-- id:affect.5 -->")
    assert s3 == "干净文本"

    # No-op on clean text.
    assert reconcile._sanitize_affect_text("纯净描述") == "纯净描述"
    assert reconcile._sanitize_affect_text(None) is None


def test_scrub_pollution_idempotent(conn):
    """Inserting a polluted description; scrub must clean it and a second
    pass must be a no-op (returns 0 touched).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    aid = _insert_affect(
        conn, date=today, ep=1, v=0.5, a=0.5, importance=2,
        label="开心", description="以为provider接口没做 [25m ago] <!-- aff:67 -->",
    )
    touched = reconcile._scrub_affect_pollution(conn)
    assert touched == 1
    row = conn.execute(
        "SELECT description FROM affect WHERE id=?", (aid,)
    ).fetchone()
    assert row["description"] == "以为provider接口没做"
    # Second pass is no-op.
    again = reconcile._scrub_affect_pollution(conn)
    assert again == 0


def test_inline_anchor_parsed_by_reconcile(conn, tmp_path):
    """End-to-end: dashboard with inline `<!-- aff:N -->` on bullet line
    is parsed by reconcile_affect; description edit lands in DB.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    aid = _insert_affect(conn, date=today, ep=1, v=0.7, a=0.7, importance=3,
                          label="开心", description="原始")
    dash = tmp_path / "dashboard.md"
    dashboard.write_dashboard(str(dash), conn, state_dir=str(tmp_path / "s"))
    text = dash.read_text()
    # Sanity: anchor is end-of-line (one space before `<!--`, then `-->`).
    bullet = next(ln for ln in text.splitlines()
                   if ln.startswith("- 【") and "eph" in ln)
    assert bullet.rstrip().endswith(f"<!-- aff:{aid} -->"), \
        f"anchor must be inline end-of-line: {bullet!r}"
    edited = text.replace("原始", "改过")
    dash.write_text(edited)
    rpt = reconcile.reconcile_affect(conn, dash)
    assert rpt.updated == 1
    desc = conn.execute(
        "SELECT description FROM affect WHERE id=?", (aid,)
    ).fetchone()["description"]
    assert desc == "改过"


def test_aff_anchor_deletion_supersedes_row(conn, tmp_path):
    """User removes an ep id from the aff anchor → row marked superseded."""
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now.strftime("%Y-%m-%d")
    aid1 = _insert_affect(conn, date=today, ep=1, v=0.8, a=0.6,
                          importance=3, label="a", description="keep",
                          created_at=ts)
    aid2 = _insert_affect(conn, date=today, ep=2, v=0.2, a=0.5,
                          importance=3, label="b", description="delete-me",
                          created_at=ts)
    dash = tmp_path / "dashboard.md"
    rendered = top_sections.render_affect(conn)
    dash.write_text(rendered + "\n## Content\n")
    text = dash.read_text()
    assert f"<!-- aff-rendered:" in text
    assert str(aid1) in text and str(aid2) in text

    # Simulate user removing aid2 from the visible bullet only.
    # The <!-- aff-rendered:... --> comment stays untouched (user won't edit it).
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if f"<!-- aff:" in ln and "aff-rendered" not in ln:
            lines[i] = re.sub(r" · epl\d+ b \| delete-me", "", ln)
            lines[i] = lines[i].replace(f",{aid2}", "")
            break
    text = "\n".join(lines)
    dash.write_text(text)

    rpt = reconcile.reconcile_affect(conn, dash)
    row = conn.execute(
        "SELECT superseded_by FROM affect WHERE id=?", (aid2,)
    ).fetchone()
    assert row["superseded_by"] == aid2, "deleted ep should self-supersede"
    assert rpt.updated >= 1


def test_aff_anchor_deletion_no_false_positive_on_unrendered(conn, tmp_path):
    """Ids NOT in aff-rendered should NOT be superseded even if absent."""
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now.strftime("%Y-%m-%d")
    aid1 = _insert_affect(conn, date=today, ep=1, v=0.8, a=0.6,
                          importance=3, label="a", description="shown",
                          created_at=ts)
    # aid2 exists in DB but won't be rendered (too many eps, only eph/epl)
    aid_hidden = _insert_affect(conn, date=today, ep=3, v=0.5, a=0.5,
                                importance=1, label="c", description="mid",
                                created_at=ts)
    dash = tmp_path / "dashboard.md"
    rendered = top_sections.render_affect(conn)
    dash.write_text(rendered + "\n## Content\n")
    text = dash.read_text()
    # aid_hidden might or might not be in the rendered anchors depending on
    # eph/epl selection; if it IS rendered, this test is vacuous. Guard:
    if str(aid_hidden) not in text:
        rpt = reconcile.reconcile_affect(conn, dash)
        row = conn.execute(
            "SELECT superseded_by FROM affect WHERE id=?", (aid_hidden,)
        ).fetchone()
        assert row["superseded_by"] is None, "unrendered id must not be superseded"
