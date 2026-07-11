"""Tests for reconcile_tasks and related render/sessionend fixes."""
from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from marrow import reconcile, storage
from marrow import dashboard, top_sections

_TZ = ZoneInfo("Australia/Melbourne")


@pytest.fixture()
def conn(tmp_path):
    db = str(tmp_path / "t.db")
    c = storage.init_db(db)
    yield c
    c.close()


def _insert_task(conn, title: str, status: str = "active",
                 category: str = "Study", updated_at: str | None = None) -> int:
    ts = updated_at or datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    cur = conn.execute(
        "INSERT INTO tasks (category, title, status, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (category, title, status, ts),
    )
    conn.commit()
    return cur.lastrowid


def _render_dashboard(conn, tmp_path) -> Path:
    """Write a fresh dashboard and return its path."""
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    dashboard.write_dashboard(str(dash), conn, state_dir=str(state))
    return dash


# ── 1. tick: [ ] -> [x] flips DB to done ─────────────────────────────────────

def test_tick_sets_done(conn, tmp_path):
    tid = _insert_task(conn, "Write notes")
    dash = _render_dashboard(conn, tmp_path)
    text = dash.read_text()
    # Simulate the user ticking the checkbox.
    text = text.replace(f"- [ ] [Study] Write notes", f"- [x] [Study] Write notes")
    dash.write_text(text)

    rpt = reconcile.reconcile_tasks(conn, dash)

    assert rpt.updated == 1
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "done"


# ── 2. untick: [x] -> [ ] flips DB back to active ────────────────────────────

def test_untick_sets_active(conn, tmp_path):
    # Use cutoff+1h (post-6AM) so the row lands in today's Completed.
    after_cutoff = (top_sections._day_cutoff_utc() +
                    datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tid = _insert_task(conn, "Review slides", status="done",
                       updated_at=after_cutoff)
    dash = _render_dashboard(conn, tmp_path)
    text = dash.read_text()
    # Simulate the user un-ticking.
    text = text.replace(
        f"- [x] [Study] Review slides <!-- id:{tid} -->",
        f"- [ ] [Study] Review slides <!-- id:{tid} -->",
    )
    dash.write_text(text)

    rpt = reconcile.reconcile_tasks(conn, dash)

    assert rpt.updated == 1
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "active"


# ── 2b. title text edit absorbed into DB ─────────────────────────────────────

def _swap_title(text: str, tid: int, old: str, new: str) -> str:
    """Find the rendered row by anchor and substitute only the title segment."""
    out_lines = []
    needle = f"<!-- id:{tid} -->"
    for ln in text.splitlines():
        if needle in ln and old in ln:
            ln = ln.replace(old, new, 1)
        out_lines.append(ln)
    return "\n".join(out_lines)


def test_title_edit_updates_db(conn, tmp_path):
    tid = _insert_task(conn, "123")
    dash = _render_dashboard(conn, tmp_path)
    # User rewrites the title — leave the anchor + check intact.
    dash.write_text(_swap_title(dash.read_text(), tid, "] 123 [", "] 321 ["))

    rpt = reconcile.reconcile_tasks(conn, dash)

    assert rpt.updated == 1
    row = conn.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["title"] == "321"


def test_title_edit_then_render_preserves_edit(conn, tmp_path):
    """End-to-end: hand-edit title in md, write_dashboard reconciles +
    re-renders. Re-rendered body must show the user's edited title."""
    tid = _insert_task(conn, "old title")
    dash = _render_dashboard(conn, tmp_path)
    dash.write_text(
        _swap_title(dash.read_text(), tid, "] old title [", "] new title [")
    )
    dashboard.write_dashboard(str(dash), conn, state_dir=str(tmp_path / "s"))
    result = dash.read_text()
    db_title = conn.execute(
        "SELECT title FROM tasks WHERE id=?", (tid,)
    ).fetchone()["title"]
    assert db_title == "new title"
    assert "new title" in result
    assert "old title" not in result


def test_title_edit_with_next_step_suffix(conn, tmp_path):
    """next_step suffix peeling: edit the title text but leave the suffix."""
    tid = _insert_task(conn, "draft")
    conn.execute("UPDATE tasks SET next_step=? WHERE id=?", ("write intro", tid))
    conn.commit()
    dash = _render_dashboard(conn, tmp_path)
    # Rendered row body: `[Study] draft: write intro [<date>] <!-- id:N -->`
    dash.write_text(_swap_title(dash.read_text(), tid,
                                "] draft: write intro [",
                                "] essay: write intro ["))
    reconcile.reconcile_tasks(conn, dash)
    row = conn.execute(
        "SELECT title, next_step FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["title"] == "essay"
    # next_step left untouched.
    assert row["next_step"] == "write intro"


def test_next_step_edit_only(conn, tmp_path):
    """Edit the next_step text inline — title stays, DB next_step updates.

    Repro for the user's task #148: title contains `: ` and the next_step text
    is the part the user wants to rewrite; suffix match fails but prefix match
    on `<title>: ` should still absorb the edit.
    """
    tid = _insert_task(conn, "mw-phase 3: Almost done")
    conn.execute("UPDATE tasks SET next_step=? WHERE id=?",
                 ("Merge wt first; HIGH-2 + MED-2 still pending.", tid))
    conn.commit()
    dash = _render_dashboard(conn, tmp_path)
    dash.write_text(_swap_title(
        dash.read_text(), tid,
        ": Merge wt first; HIGH-2 + MED-2 still pending.",
        ": HIGH-2 patched, MED-2 left.",
    ))
    reconcile.reconcile_tasks(conn, dash)
    row = conn.execute(
        "SELECT title, next_step FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["title"] == "mw-phase 3: Almost done"
    assert row["next_step"] == "HIGH-2 patched, MED-2 left."


def test_next_step_cleared(conn, tmp_path):
    """User deletes the `: <next_step>` segment entirely → next_step NULL."""
    tid = _insert_task(conn, "mw-phase 3")
    conn.execute("UPDATE tasks SET next_step=? WHERE id=?", ("foo bar", tid))
    conn.commit()
    dash = _render_dashboard(conn, tmp_path)
    dash.write_text(_swap_title(
        dash.read_text(), tid, ": foo bar", "",
    ))
    reconcile.reconcile_tasks(conn, dash)
    row = conn.execute(
        "SELECT title, next_step FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["title"] == "mw-phase 3"
    assert row["next_step"] is None


def test_title_with_colon_no_next_step_round_trip(conn, tmp_path):
    """After clearing next_step, a second reconcile pass on the unchanged
    title (which contains `: `) must not split the title in half.

    Regression for task #148: title='mw-phase 3: Almost done', next_step=NULL
    → second reconcile re-rendered as `mw-phase 3: Almost done` and the
    parser wrongly split into ('mw-phase 3', 'Almost done').
    """
    tid = _insert_task(conn, "mw-phase 3: Almost done")
    # next_step starts NULL.
    dash = _render_dashboard(conn, tmp_path)
    reconcile.reconcile_tasks(conn, dash)
    row = conn.execute(
        "SELECT title, next_step FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["title"] == "mw-phase 3: Almost done"
    assert row["next_step"] is None


def test_ambiguous_edit_keeps_db(conn, tmp_path):
    """Both fields edited beyond recognition → conflict, DB unchanged."""
    tid = _insert_task(conn, "alpha")
    conn.execute("UPDATE tasks SET next_step=? WHERE id=?", ("beta", tid))
    conn.commit()
    dash = _render_dashboard(conn, tmp_path)
    dash.write_text(_swap_title(
        dash.read_text(), tid, "] alpha: beta [", "] gamma: delta [",
    ))
    rpt = reconcile.reconcile_tasks(conn, dash)
    row = conn.execute(
        "SELECT title, next_step FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["title"] == "alpha"
    assert row["next_step"] == "beta"
    assert any("ambiguous" in c for c in rpt.conflicts)


# ── 3. delete-by-trail: id in trail, missing from md -> archived ──────────────

def test_delete_by_trail_archives(conn, tmp_path):
    tid = _insert_task(conn, "Finish assignment")
    dash = _render_dashboard(conn, tmp_path)
    text = dash.read_text()
    # Strip the task row but keep the trail marker intact.
    lines = [
        ln for ln in text.splitlines()
        if f"<!-- id:{tid} -->" not in ln
    ]
    dash.write_text("\n".join(lines))

    rpt = reconcile.reconcile_tasks(conn, dash)

    assert rpt.deleted == 1
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "archived"


def test_delete_done_row_archives(conn, tmp_path):
    """Deleting a Completed row mid-day must stick — was a bug: archive
    branch treated `done` as terminal, so the row resurrected on every
    render until the 6AM cutoff window expired."""
    after_cutoff = (top_sections._day_cutoff_utc() +
                    datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tid = _insert_task(conn, "Finished thing", status="done",
                       updated_at=after_cutoff)
    dash = _render_dashboard(conn, tmp_path)
    text = dash.read_text()
    assert "Finished thing" in text
    lines = [ln for ln in text.splitlines() if f"<!-- id:{tid} -->" not in ln]
    dash.write_text("\n".join(lines))

    rpt = reconcile.reconcile_tasks(conn, dash)

    assert rpt.deleted == 1
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "archived"
    # Re-render must not bring it back.
    dashboard.write_dashboard(str(dash), conn, state_dir=str(tmp_path / "s2"))
    assert "Finished thing" not in dash.read_text()


# ── 4. no-op when trail absent ────────────────────────────────────────────────

def test_noop_when_no_trail(conn, tmp_path):
    _insert_task(conn, "Old task")
    # Write a dashboard without anchors (manually constructed, no trail).
    dash = tmp_path / "dashboard.md"
    dash.write_text("## Tasks\n### Completed [0]\n_none_\n### To-Do List [1]\nToday\n- [ ] [Study] Old task\n")

    rpt = reconcile.reconcile_tasks(conn, dash)

    assert rpt.updated == 0
    assert rpt.deleted == 0
    assert not rpt.conflicts


# ── 5. _seg_task_cand: done status UPDATEs active row, no INSERT ──────────────

def test_seg_task_cand_done_updates_not_inserts(conn, tmp_path):
    from marrow.sessionend_writers import seg_task_cand as _seg_task_cand

    tid = _insert_task(conn, "Fix bug", status="active")
    before = conn.execute("SELECT COUNT(*) c FROM tasks WHERE title='Fix bug'").fetchone()["c"]
    assert before == 1

    raw = '===TASK===\n[{"title": "Fix bug", "status": "done", "category": "Project"}]\n===END==='
    _seg_task_cand(conn, raw)

    rows = conn.execute("SELECT status FROM tasks WHERE title='Fix bug'").fetchall()
    assert len(rows) == 1  # no duplicate
    assert rows[0]["status"] == "done"


# ── 6. _seg_task_cand: archived title skips insert ───────────────────────────

def test_seg_task_cand_skips_archived(conn, tmp_path):
    from marrow.sessionend_writers import seg_task_cand as _seg_task_cand

    _insert_task(conn, "Old task", status="archived")

    raw = '===TASK===\n[{"title": "Old task", "status": "active", "category": "Others"}]\n===END==='
    _seg_task_cand(conn, raw)

    count = conn.execute("SELECT COUNT(*) c FROM tasks WHERE title='Old task'").fetchone()["c"]
    assert count == 1  # still only one row


# ── 8. unanchored hand-typed rows are absorbed as new tasks ─────────────────

def _inject_into_tasks_block(text: str, new_line: str) -> str:
    """Insert `new_line` just before the `<!-- cand:task:ids=[...] -->` trail."""
    out = []
    for ln in text.splitlines():
        if ln.startswith("<!-- cand:task:ids="):
            out.append(new_line)
        out.append(ln)
    return "\n".join(out)


def test_unanchored_task_row_inserts_into_db(conn, tmp_path):
    """User types a brand-new task line into the dashboard → reconcile INSERTs."""
    # Start with at least one anchored row so the trail marker renders.
    _insert_task(conn, "seed task")
    dash = _render_dashboard(conn, tmp_path)
    text = dash.read_text()
    dash.write_text(_inject_into_tasks_block(
        text, "- [ ] [Project] my new task [2026-06-01]"
    ))

    rpt = reconcile.reconcile_tasks(conn, dash)

    assert rpt.inserted == 1
    row = conn.execute(
        "SELECT category, title, due, status FROM tasks "
        "WHERE title='my new task'"
    ).fetchone()
    assert row is not None
    assert row["category"] == "Project"
    assert row["due"] == "2026-06-01"
    assert row["status"] == "active"


def test_unanchored_task_survives_full_refresh(conn, tmp_path):
    """End-to-end: type a row, run write_dashboard → re-render shows the row
    with an `<!-- id:N -->` anchor matching the newly inserted DB id."""
    _insert_task(conn, "seed task")
    dash = _render_dashboard(conn, tmp_path)
    text = dash.read_text()
    dash.write_text(_inject_into_tasks_block(
        text, "- [ ] [Project] survives refresh [2026-06-02]"
    ))

    dashboard.write_dashboard(str(dash), conn, state_dir=str(tmp_path / "s"))
    result = dash.read_text()
    new_id = conn.execute(
        "SELECT id FROM tasks WHERE title='survives refresh'"
    ).fetchone()["id"]
    assert f"<!-- id:{new_id} -->" in result
    assert "survives refresh" in result


def test_unanchored_done_row_inserts_as_done(conn, tmp_path):
    """`- [x] [Project] already done` → row inserted with status='done'."""
    _insert_task(conn, "seed task")
    dash = _render_dashboard(conn, tmp_path)
    dash.write_text(_inject_into_tasks_block(
        dash.read_text(), "- [x] [Project] already done [2026-06-01]"
    ))

    reconcile.reconcile_tasks(conn, dash)
    row = conn.execute(
        "SELECT status FROM tasks WHERE title='already done'"
    ).fetchone()
    assert row is not None
    assert row["status"] == "done"


def test_unanchored_default_category_when_bracket_missing(conn, tmp_path):
    """Missing [<tag>] prefix → category falls back to Project."""
    _insert_task(conn, "seed task")
    dash = _render_dashboard(conn, tmp_path)
    dash.write_text(_inject_into_tasks_block(
        dash.read_text(), "- [ ] no bracket here"
    ))

    reconcile.reconcile_tasks(conn, dash)
    row = conn.execute(
        "SELECT category FROM tasks WHERE title='no bracket here'"
    ).fetchone()
    assert row is not None
    assert row["category"] == "Project"


def test_unanchored_dedup_against_active_title(conn, tmp_path):
    """Hand-typed line that matches an existing active task → skip insert,
    silently. No info-level alert: the next render replaces the hand-typed
    line with the canonical anchored row, so the dedup is invisible to the user.
    """
    _insert_task(conn, "dup title", category="Project")
    dash = _render_dashboard(conn, tmp_path)
    dash.write_text(_inject_into_tasks_block(
        dash.read_text(), "- [ ] [Project] dup title"
    ))

    rpt = reconcile.reconcile_tasks(conn, dash)
    assert rpt.inserted == 0
    count = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE title='dup title'"
    ).fetchone()["c"]
    assert count == 1
    # No info alert pollutes the dashboard Alerts block.
    alerts = conn.execute(
        "SELECT severity, message FROM alerts WHERE resolved=0"
    ).fetchall()
    assert not any("dedup" in (a["message"] or "").lower() for a in alerts), \
        f"silent dedup must not write info-level alert: {alerts}"


# ── 9. Completed cutoff: 6AM local boundary ──────────────────────────────────

def test_completed_cutoff_6am(conn, tmp_path):
    # Pre-6AM done timestamp hides; post-6AM shows.
    cutoff_utc = top_sections._day_cutoff_utc()
    before = (cutoff_utc - datetime.timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    after = (cutoff_utc + datetime.timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    _insert_task(conn, "Pre-cutoff task", status="done", updated_at=before)
    _insert_task(conn, "Post-cutoff task", status="done", updated_at=after)

    md = top_sections.render_tasks(conn)
    assert "Post-cutoff task" in md
    assert "Pre-cutoff task" not in md
