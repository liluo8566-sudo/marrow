"""Action-dispatch coverage for the 12-tool MCP surface rebuild (07-06):
tl clear, dim upsert/query/delete (all kinds), sticker/sticker_admin/alert
dispatch validation, event_clear filters, first untick/list.
"""
from __future__ import annotations

import pytest

from marrow import config, cortex_bridge, daemon, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    storage.init_db(db).close()
    monkeypatch.setattr(daemon, "_DB", db)
    monkeypatch.setattr(cortex_bridge, "_DB", db)
    monkeypatch.setattr(config, "db_path", lambda: db)
    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: None)
    return db


def _insert_tl(db, body, sid="s1", ts=None):
    conn = storage.connect(db)
    try:
        ts = ts or "2026-07-01T00:00:00Z"
        with conn:
            cur = conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content,"
                " channel, imp) VALUES (?, ?, 'tl', ?, 'cli', 3)",
                (sid, ts, body),
            )
        return cur.lastrowid
    finally:
        conn.close()


def _event_count(db):
    conn = storage.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


# ── tl clear ─────────────────────────────────────────────────────────────────

def test_tl_clear_by_event_id(env):
    eid = _insert_tl(env, "row one")
    _insert_tl(env, "row two")
    out = daemon.tl("clear", event_id=eid)
    assert out["ok"] is True
    assert out["cleared"] == 1
    assert _event_count(env) == 1


def test_tl_clear_by_sid(env):
    _insert_tl(env, "a", sid="sess-x")
    _insert_tl(env, "b", sid="sess-x")
    _insert_tl(env, "c", sid="sess-y")
    out = daemon.tl("clear", sid="sess-x")
    assert out["cleared"] == 2
    assert _event_count(env) == 1


def test_tl_clear_by_range(env):
    _insert_tl(env, "old", ts="2026-06-01T00:00:00Z")
    _insert_tl(env, "new", ts="2026-07-01T00:00:00Z")
    out = daemon.tl("clear", before="2026-06-15T00:00:00Z")
    assert out["cleared"] == 1
    assert _event_count(env) == 1


def test_tl_clear_single_row_no_backup_returns_line(env):
    eid = _insert_tl(env, "【N愉悦】只有一行 [3]")
    out = daemon.tl("clear", event_id=eid)
    assert out["ok"] is True
    assert out["cleared"] == 1
    assert "backup" not in out
    assert len(out["deleted"]) == 1
    assert "只有一行" in out["deleted"][0]


def test_tl_clear_multi_row_backs_up_and_returns_lines(env, tmp_path):
    import os
    _insert_tl(env, "【N愉悦】行一 [3]", sid="sess-x")
    _insert_tl(env, "【N愉悦】行二 [3]", sid="sess-x")
    out = daemon.tl("clear", sid="sess-x")
    assert out["ok"] is True
    assert out["cleared"] == 2
    assert "backup" in out
    assert out["backup"].startswith("/tmp/marrow-backup-tlclear-")
    assert os.path.exists(out["backup"])
    assert len(out["deleted"]) == 2
    assert {"行一" in l or "行二" in l for l in out["deleted"]} == {True}
    os.remove(out["backup"])


def test_tl_clear_requires_selector(env):
    out = daemon.tl("clear")
    assert out["ok"] is False


def test_tl_clear_rejects_multiple_selectors(env):
    out = daemon.tl("clear", event_id=1, sid="x")
    assert out["ok"] is False


def test_tl_clear_ignores_non_tl_rows(env):
    conn = storage.connect(env)
    try:
        with conn:
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content,"
                " channel) VALUES ('m', '2026-07-01T00:00:00Z', 'user',"
                " 'not tl', 'manual')")
    finally:
        conn.close()
    out = daemon.tl("clear", before="2026-08-01T00:00:00Z")
    assert out["cleared"] == 0
    assert _event_count(env) == 1


def test_tl_unknown_action(env):
    out = daemon.tl("bogus")
    assert out["ok"] is False


# ── tl query + content-based (match/date) addressing ─────────────────────────

def _no_dashboard(monkeypatch, tmp_path):
    """Point tl_writer's dashboard path at a scratch file so update tests never
    touch the real ~/Desktop/NY/dashboard.md."""
    from marrow import tl_writer
    monkeypatch.setattr(tl_writer, "_dashboard_path",
                        lambda: tmp_path / "dashboard.md")


def test_tl_query_by_match(env):
    eid = _insert_tl(env, "买千层蛋糕", ts="2026-07-05T05:00:00Z")
    _insert_tl(env, "别的事情", ts="2026-07-05T06:00:00Z")
    out = daemon.tl("query", match="千层")
    assert out["ok"] is True
    assert len(out["matches"]) == 1
    assert out["matches"][0]["event_id"] == eid
    assert "千层" in out["matches"][0]["line"]


def test_tl_query_match_percent_is_literal(env):
    """A bare '%' in the match string must only hit content containing a
    literal '%' — unescaped, LIKE would treat it as a wildcard matching
    every row and silently broaden the match (a wrong-row hazard for the
    update/clear paths that share this same resolver)."""
    eid = _insert_tl(env, "50% off coupon", ts="2026-07-05T05:00:00Z")
    _insert_tl(env, "totally unrelated row", ts="2026-07-05T06:00:00Z")
    out = daemon.tl("query", match="%")
    assert out["ok"] is True
    assert len(out["matches"]) == 1
    assert out["matches"][0]["event_id"] == eid


def test_tl_query_by_date_respects_melb_day(env):
    _insert_tl(env, "day five", ts="2026-07-05T05:00:00Z")
    _insert_tl(env, "day six", ts="2026-07-06T05:00:00Z")
    out = daemon.tl("query", date="2026-07-05")
    assert out["ok"] is True
    assert [m["line"] for m in out["matches"]]
    assert all("day six" not in m["line"] for m in out["matches"])


def test_tl_query_requires_param(env):
    out = daemon.tl("query")
    assert out["ok"] is False


def test_tl_query_bad_date(env):
    out = daemon.tl("query", date="not-a-date")
    assert out["ok"] is False


def test_tl_update_by_match_single_hit(env, monkeypatch, tmp_path):
    _no_dashboard(monkeypatch, tmp_path)
    eid = _insert_tl(env, "【N愉悦】买千层 [3]", ts="2026-07-05T05:00:00Z")
    out = daemon.tl("update", match="千层", body="买千层蛋糕")
    assert out["ok"] is True
    assert out["event_id"] == eid
    conn = storage.connect(env)
    try:
        content = conn.execute(
            "SELECT content FROM events WHERE id=?", (eid,)).fetchone()[0]
    finally:
        conn.close()
    assert "买千层蛋糕" in content


def test_tl_update_by_match_multiple_does_not_execute(env, monkeypatch, tmp_path):
    _no_dashboard(monkeypatch, tmp_path)
    _insert_tl(env, "千层 one", ts="2026-07-05T05:00:00Z")
    _insert_tl(env, "千层 two", ts="2026-07-05T06:00:00Z")
    out = daemon.tl("update", match="千层", body="changed")
    assert out["ok"] is False
    assert len(out["matches"]) == 2
    conn = storage.connect(env)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE content LIKE '%changed%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_tl_update_by_match_zero_hits(env):
    out = daemon.tl("update", match="nonexistent")
    assert out["ok"] is False


def test_tl_update_still_requires_addressing(env):
    out = daemon.tl("update", body="x")
    assert out["ok"] is False


def test_tl_update_resets_current_session_nudge_counter(env, monkeypatch, tmp_path):
    from marrow import tl_nudge
    monkeypatch.setattr(tl_nudge.config, "DATA_DIR", tmp_path / "nudge_data")

    eid = _insert_tl(env, "row one", sid="sess-cur")
    conn = storage.connect(env)
    try:
        with conn:
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES ('session', 'sess-cur', 'session_lifecycle:start', '')")
    finally:
        conn.close()

    tl_nudge._save_count("sess-cur", 7)
    out = daemon.tl("update", event_id=eid, body="edited")
    assert out["ok"] is True
    assert tl_nudge._load_count("sess-cur") == 0


def test_tl_clear_by_match_single(env):
    _insert_tl(env, "千层 unique", ts="2026-07-05T05:00:00Z")
    _insert_tl(env, "other row", ts="2026-07-05T06:00:00Z")
    out = daemon.tl("clear", match="千层")
    assert out["ok"] is True
    assert out["cleared"] == 1
    assert _event_count(env) == 1


def test_tl_clear_by_match_multiple_does_not_execute(env):
    _insert_tl(env, "千层 one")
    _insert_tl(env, "千层 two")
    out = daemon.tl("clear", match="千层")
    assert out["ok"] is False
    assert len(out["matches"]) == 2
    assert _event_count(env) == 2


# ── dim: entities already covered in test_daemon_entity_upsert.py ────────────

def test_dim_upsert_meme_create_and_update(env):
    out = daemon.dim("upsert", kind="meme", name="绿茶豹", fact="loving nickname",
                      meme_type="paw")
    assert out["ok"] is True
    assert out["action"] == "create"
    mid = out["id"]
    conn = storage.connect(env)
    try:
        row = conn.execute(
            "SELECT type, key, value, pinned, updated_at FROM memes"
            " WHERE id=?", (mid,)).fetchone()
    finally:
        conn.close()
    assert row["type"] == "paw"
    assert row["key"] == "绿茶豹"
    assert row["pinned"] == 1
    assert row["updated_at"] is not None  # explicit UTC stamp, never NULL

    out2 = daemon.dim("upsert", kind="meme", name="绿茶豹", fact="updated meaning")
    assert out2["action"] == "update"
    assert out2["id"] == mid


def test_dim_upsert_meme_rejects_bad_type(env):
    out = daemon.dim("upsert", kind="meme", name="x", meme_type="bogus")
    assert out["ok"] is False


def test_dim_upsert_meme_string_dedup_against_milestone(env):
    daemon.dim("upsert", kind="milestone", name="毕业", date="2026-01-01")
    out = daemon.dim("upsert", kind="meme", name="毕业", fact="dup of milestone")
    assert out["ok"] is False
    assert "dedup" in out["error"]


def test_dim_upsert_milestone_create_and_update(env):
    out = daemon.dim("upsert", kind="milestone", name="搬家", fact="moved to Clayton",
                      date="2026-02-01")
    assert out["ok"] is True
    assert out["action"] == "create"
    mid = out["id"]
    out2 = daemon.dim("upsert", kind="milestone", name="搬家", date="2026-02-01",
                      fact="updated desc")
    assert out2["action"] == "update"
    assert out2["id"] == mid
    conn = storage.connect(env)
    try:
        row = conn.execute("SELECT description, pinned, scope FROM milestones"
                           " WHERE id=?", (mid,)).fetchone()
    finally:
        conn.close()
    assert row["description"] == "updated desc"
    assert row["pinned"] == 1
    assert row["scope"] == "me"  # default scope, see report ambiguity note


def test_dim_upsert_milestone_requires_valid_date(env):
    out = daemon.dim("upsert", kind="milestone", name="x", date="not-a-date")
    assert out["ok"] is False


def test_dim_upsert_unknown_kind(env):
    out = daemon.dim("upsert", kind="gadget", name="x")
    assert out["ok"] is False


def test_dim_query_by_kind_and_name(env):
    daemon.dim("upsert", kind="person", name="王医生", fact="ED consultant")
    daemon.dim("upsert", kind="meme", name="绿茶豹", fact="nickname")
    daemon.dim("upsert", kind="milestone", name="搬家", date="2026-02-01")

    people = daemon.dim("query", kind="person")
    assert len(people) == 1
    assert people[0]["name"] == "王医生"

    memes = daemon.dim("query", kind="meme", name="绿茶")
    assert len(memes) == 1
    assert memes[0]["name"] == "绿茶豹"

    milestones = daemon.dim("query", kind="milestone")
    assert len(milestones) == 1
    assert milestones[0]["name"] == "搬家"

    everything = daemon.dim("query", name="搬家")
    assert any(r["kind"] == "milestone" for r in everything)


def test_dim_delete_entity_removes_row_md_and_tombstone(env, tmp_path, monkeypatch):
    pages = tmp_path / "pages"
    pages.mkdir()
    monkeypatch.setattr(config, "db_pages_path", lambda: str(pages))
    out = daemon.dim("upsert", kind="person", name="王医生", fact="ED consultant")
    pid = out["id"]
    (pages / "profile.md").write_text(
        f"## Person\n\n- [person] **王医生** — ED consultant <!-- id:{pid} -->\n",
        encoding="utf-8",
    )
    del_out = daemon.dim("delete", kind="person", id=pid)
    assert del_out == {"ok": True, "kind": "person", "id": pid, "deleted": True}
    conn = storage.connect(env)
    try:
        assert conn.execute("SELECT COUNT(*) FROM entities WHERE id=?",
                            (pid,)).fetchone()[0] == 0
        tomb = conn.execute(
            "SELECT tombstone_at FROM md_index WHERE block_id=?", (str(pid),)
        ).fetchone()
        assert tomb is not None and tomb["tombstone_at"] is not None
    finally:
        conn.close()
    md = (pages / "profile.md").read_text(encoding="utf-8")
    assert "王医生" not in md


def test_dim_delete_milestone_removes_two_line_block(env, tmp_path, monkeypatch):
    pages = tmp_path / "pages"
    pages.mkdir()
    monkeypatch.setattr(config, "db_pages_path", lambda: str(pages))
    out = daemon.dim("upsert", kind="milestone", name="搬家", fact="moved",
                      date="2026-02-01")
    mid = out["id"]
    (pages / "milestone.md").write_text(
        f"##### [2026-02-01] 搬家\nmoved <!-- id:{mid} -->\n\n"
        "##### [2026-03-01] other\nother <!-- id:999 -->\n",
        encoding="utf-8",
    )
    del_out = daemon.dim("delete", kind="milestone", id=mid)
    assert del_out["ok"] is True
    md = (pages / "milestone.md").read_text(encoding="utf-8")
    assert "搬家" not in md
    assert "other" in md  # sibling block untouched


def test_dim_delete_not_found(env):
    out = daemon.dim("delete", kind="meme", id=99999)
    assert out["ok"] is False


def test_dim_delete_requires_id(env):
    out = daemon.dim("delete", kind="meme")
    assert out["ok"] is False


def test_dim_unknown_action(env):
    out = daemon.dim("bogus", kind="person", name="x")
    assert out["ok"] is False


# ── sticker / sticker_admin dispatch ──────────────────────────────────────────

def test_sticker_unknown_action(env):
    out = daemon.sticker("bogus")
    assert out["ok"] is False


def test_sticker_search_empty_query_returns_empty_list(env):
    assert daemon.sticker("search", query="") == []


def test_sticker_pick_requires_id(env):
    out = daemon.sticker("pick")
    assert out["ok"] is False


def test_sticker_pick_updates_last_used(env):
    conn = storage.connect(env)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO stickers (path, sha256, desc, source)"
                " VALUES ('/x.png', 'abc', 'happy', 'wechat')")
        sid = cur.lastrowid
    finally:
        conn.close()
    out = daemon.sticker("pick", sticker_id=sid)
    assert out == {"ok": True, "id": sid}


def test_sticker_admin_unknown_action(env):
    out = daemon.sticker_admin("bogus")
    assert out["ok"] is False


def test_sticker_admin_pending_lists_missing_desc(env):
    conn = storage.connect(env)
    try:
        with conn:
            conn.execute(
                "INSERT INTO stickers (path, sha256, desc, source)"
                " VALUES ('/x.png', 'abc', '(pending)', 'wechat')")
    finally:
        conn.close()
    rows = daemon.sticker_admin("pending")
    assert len(rows) == 1
    assert rows[0]["desc"] == "(pending)"


def test_sticker_admin_update_requires_fields(env):
    out = daemon.sticker_admin("update")
    assert out["ok"] is False


def test_sticker_admin_delete_requires_id(env):
    out = daemon.sticker_admin("delete")
    assert out["ok"] is False


def test_sticker_admin_ingest_requires_fields(env):
    out = daemon.sticker_admin("ingest")
    assert out["ok"] is False


def test_sticker_admin_update_returns_old_desc(env, tmp_path, monkeypatch):
    pages = tmp_path / "pages"
    pages.mkdir()
    monkeypatch.setattr(config, "db_pages_path", lambda: str(pages))
    monkeypatch.setattr(daemon, "_write_stickers_subpage", lambda conn: None)
    conn = storage.connect(env)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO stickers (path, sha256, desc, source)"
                " VALUES ('/x.png', 'abc', 'old desc', 'wechat')")
        sid = cur.lastrowid
    finally:
        conn.close()
    out = daemon.sticker_admin("update", sticker_id=sid, desc="new desc")
    assert out["ok"] is True
    assert out["desc"] == "new desc"
    assert out["old_desc"] == "old desc"


def test_sticker_admin_delete_strips_md_line_and_unlinks(env, tmp_path, monkeypatch):
    pages = tmp_path / "pages"
    pages.mkdir()
    monkeypatch.setattr(config, "db_pages_path", lambda: str(pages))
    # Isolate the fix under test (the stale-line purge); the full subpage
    # re-render exercises unrelated builders, so stub it out.
    monkeypatch.setattr(daemon, "_write_stickers_subpage", lambda conn: None)
    img = tmp_path / "stk_001.png"
    img.write_bytes(b"png")
    conn = storage.connect(env)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO stickers (path, sha256, desc, source)"
                " VALUES (?, 'abc', 'happy', 'wechat')", (str(img),))
        sid = cur.lastrowid
    finally:
        conn.close()
    (pages / "stickers.md").write_text(
        f"- stk_{sid:03d} happy <!-- id:{sid} -->\n"
        "- stk_999 keep <!-- id:999 -->\n", encoding="utf-8")
    out = daemon.sticker_admin("delete", sticker_id=sid)
    assert out["ok"] is True
    assert not img.exists()  # image unlinked
    md = (pages / "stickers.md").read_text(encoding="utf-8")
    assert f"id:{sid} " not in md and f"id:{sid} -->" not in md
    assert "id:999" in md  # sibling line untouched


# ── alert dispatch ─────────────────────────────────────────────────────────────

def test_alert_unknown_action(env):
    out = daemon.alert("bogus")
    assert out["ok"] is False


def test_alert_list_unresolved_only(env):
    conn = storage.connect(env)
    try:
        with conn:
            conn.execute(
                "INSERT INTO alerts (severity, type, message, resolved)"
                " VALUES ('warn', 'x', 'open one', 0)")
            conn.execute(
                "INSERT INTO alerts (severity, type, message, resolved)"
                " VALUES ('warn', 'y', 'resolved one', 1)")
    finally:
        conn.close()
    rows = daemon.alert("list")
    assert len(rows) == 1
    assert rows[0]["message"] == "open one"


def test_alert_resolve_requires_id(env):
    out = daemon.alert("resolve")
    assert out["ok"] is False


# ── first untick/list ────────────────────────────────────────────────────────

def test_first_untick_removes_row(env):
    cortex_bridge.first("tick", item="gym-reminder", note="x", sid="s1")
    out = cortex_bridge.first("untick", item="gym-reminder")
    assert out == {"ok": True, "item": "gym-reminder"}
    conn = storage.connect(env)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ct_first_tick").fetchone()[0] == 0
    finally:
        conn.close()


def test_first_untick_missing_item_reports_false(env):
    out = cortex_bridge.first("untick", item="nope")
    assert out == {"ok": False, "item": "nope"}


def test_first_list_shows_acks(env):
    cortex_bridge.first("tick", item="a", note="n1", sid="s1")
    cortex_bridge.first("tick", item="b", note="n2", sid="s2")
    rows = cortex_bridge.first("list")
    assert {r["item"] for r in rows} == {"a", "b"}


def test_first_unknown_action(env):
    out = cortex_bridge.first("bogus")
    assert out["ok"] is False


def test_first_tick_rejects_bad_status(env):
    out = cortex_bridge.first("tick", item="x", note="n", sid="s1", status="bogus")
    assert out["ok"] is False


def test_first_tick_status_tried_stored(env):
    cortex_bridge.first("tick", item="x", note="blocked", sid="s1", status="tried")
    conn = storage.connect(env)
    try:
        row = conn.execute(
            "SELECT status FROM ct_first_tick WHERE item='x'").fetchone()
    finally:
        conn.close()
    assert row["status"] == "tried"


# ── tl silence enforcement ───────────────────────────────────────────────────

def test_tl_silence_removed_from_actions():
    assert "silence" not in daemon._TL_ACTIONS


def test_tl_add_refuses_when_silenced(env, monkeypatch):
    from marrow import tl_nudge
    monkeypatch.setattr(tl_nudge, "is_silent", lambda sid: True)
    out = daemon.tl("add", timerange="10:00", body="test", n_word="calm")
    assert out == {"ok": False, "silenced": True,
                   "error": "session is silenced (/tl-)"}
    assert _event_count(env) == 0


def test_tl_update_refuses_when_silenced(env, monkeypatch):
    from marrow import tl_nudge
    eid = _insert_tl(env, "row one")
    monkeypatch.setattr(tl_nudge, "is_silent", lambda sid: True)
    out = daemon.tl("update", event_id=eid, body="edited")
    assert out == {"ok": False, "silenced": True,
                   "error": "session is silenced (/tl-)"}


# ── event_clear ──────────────────────────────────────────────────────────────

def _insert_event(db, ts, content="x"):
    conn = storage.connect(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content,"
                " channel) VALUES ('s', ?, 'user', ?, 'cli')", (ts, content))
    finally:
        conn.close()


def test_event_clear_no_filter_wipes_everything(env):
    _insert_event(env, "2026-06-01T00:00:00Z")
    _insert_event(env, "2026-07-01T00:00:00Z")
    out = daemon.event_clear()
    assert out["ok"] is True
    assert out["purged"] == ["events"]
    assert _event_count(env) == 0


def test_event_clear_no_filter_clears_vec_meta(env):
    # Gate: no-filter clear drops triggers, so events_ad_vec does NOT cascade.
    # Meta must be wiped explicitly or freed ids inherit orphan meta (poison).
    _insert_event(env, "2026-06-01T00:00:00Z")
    conn = storage.connect(env)
    try:
        with conn:
            eid = conn.execute(
                "SELECT id FROM events LIMIT 1").fetchone()[0]
            conn.execute(
                "INSERT INTO events_vec_meta (rowid, embedder_id, dim) "
                "VALUES (?, 'bge-m3', 1024)", (eid,))
    finally:
        conn.close()
    daemon.event_clear()
    conn = storage.connect(env)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM events_vec_meta").fetchone()[0] == 0
    finally:
        conn.close()


def test_event_clear_before_after_range(env):
    _insert_event(env, "2026-06-01T00:00:00Z")
    _insert_event(env, "2026-07-01T00:00:00Z")
    out = daemon.event_clear(before="2026-06-15")
    assert out["ok"] is True
    assert _event_count(env) == 1


def test_event_clear_last_n(env):
    _insert_event(env, "2026-06-01T00:00:00Z")
    _insert_event(env, "2026-07-01T00:00:00Z")
    out = daemon.event_clear(last=1)
    assert out["counts"]["events"] == 1
    assert _event_count(env) == 1


def test_event_clear_before_and_last_mutually_exclusive(env):
    out = daemon.event_clear(before="2026-06-15", last=1)
    assert out["ok"] is False


def _insert_event_hashed(db, ts, source_hash):
    conn = storage.connect(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO events (session_id, timestamp, role, content,"
                " channel, source_hash) VALUES ('s', ?, 'user', 'x', 'cli', ?)",
                (ts, source_hash))
    finally:
        conn.close()


def _tombstone_count(db, source_hash):
    conn = storage.connect(db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM event_tombstones WHERE source_hash=?",
            (source_hash,)).fetchone()[0]
    finally:
        conn.close()


def test_event_clear_range_writes_tombstone(env):
    _insert_event_hashed(env, "2026-06-01T00:00:00Z", "hash-range")
    daemon.event_clear(before="2026-06-15")
    assert _tombstone_count(env, "hash-range") == 1


def test_event_clear_last_writes_tombstone(env):
    _insert_event_hashed(env, "2026-07-01T00:00:00Z", "hash-last")
    daemon.event_clear(last=1)
    assert _tombstone_count(env, "hash-last") == 1


def test_event_clear_range_skips_null_source_hash(env):
    _insert_event(env, "2026-06-01T00:00:00Z")  # no source_hash
    daemon.event_clear(before="2026-06-15")
    conn = storage.connect(env)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM event_tombstones").fetchone()[0] == 0
    finally:
        conn.close()


def test_event_clear_backs_up_db_first(env, tmp_path):
    _insert_event(env, "2026-06-01T00:00:00Z")
    out = daemon.event_clear()
    assert out["backup"].startswith("/tmp/marrow-backup-purge-")
    import os
    assert os.path.exists(out["backup"])
    os.remove(out["backup"])
