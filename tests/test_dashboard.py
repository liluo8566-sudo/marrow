"""Tests for marrow/dashboard.py — code-only dashboard top render.

Contract: deterministic 4-section block between markers, atomic write,
hand-written zone outside markers never touched. Free-form hand-edits
inside the rendered block are silently overwritten on next render. No LLM.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from marrow import dashboard, storage

M0 = "<!-- marrow:top:start -->"
M1 = "<!-- marrow:top:end -->"


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.db")
    conn = storage.init_db(p)
    conn.execute("INSERT INTO threads(category,title,status,due,next_step) "
                 "VALUES('study','Essay 370','active','2026-05-20','write intro')")
    conn.execute("INSERT INTO alerts(severity,type,message) "
                 "VALUES('warn','bug','recall returned 0')")
    conn.commit()
    conn.close()
    return p


def test_render_top_has_alerts_and_threads(db):
    conn = storage.connect(db)
    try:
        block = dashboard.render_top(conn)
    finally:
        conn.close()
    assert "Essay 370" in block
    assert "recall returned 0" in block
    assert M0 in block and M1 in block


def test_alert_rendered_with_severity(db):
    # Format changed in 2.5b: severity: message (no id prefix, per template spec).
    conn = storage.connect(db)
    try:
        block = dashboard.render_top(conn)
    finally:
        conn.close()
    line = next(ln for ln in block.splitlines() if "recall returned 0" in ln)
    assert line == "- warn: recall returned 0"


def test_write_creates_file_with_block(db, tmp_path):
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    conn = storage.connect(db)
    try:
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
    finally:
        conn.close()
    txt = dash.read_text()
    assert M0 in txt and "Essay 370" in txt


def test_write_preserves_hand_zone(db, tmp_path):
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    dash.write_text(f"{M0}\nOLD BLOCK\n{M1}\n\n## My notes\nkeep me\n")
    conn = storage.connect(db)
    try:
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
    finally:
        conn.close()
    txt = dash.read_text()
    assert "## My notes\nkeep me" in txt
    assert "OLD BLOCK" not in txt
    assert "Essay 370" in txt


def test_hand_edit_in_block_silently_overwritten(db, tmp_path):
    dash = tmp_path / "dashboard.md"
    state = tmp_path / "state"
    conn = storage.connect(db)
    try:
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        # Lumi edits inside the system block by hand
        t = dash.read_text().replace("Essay 370", "Essay 370 EDITED BY LUMI")
        dash.write_text(t)
        dashboard.write_dashboard(str(dash), conn, state_dir=str(state), db=db)
        result = dash.read_text()
        alerts = [a["message"] for a in
                  __import__("marrow.repo", fromlist=["x"]).open_alerts(conn)]
    finally:
        conn.close()
    # Hand-edit overwritten silently: edited text gone, no alert, no .bak.
    assert "EDITED BY LUMI" not in result
    assert not any("dashboard" in m.lower() and "hand-edited" in m.lower()
                   for m in alerts)
    assert not list(Path(state).glob("dashboard*.bak"))
