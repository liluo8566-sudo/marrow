"""Tests for marrow/cleanup.py — spawned sdk-cli .jsonl reaper.

cleanup reuses transcript.is_headless (ADR-0004): delete iff is_headless
AND older than grace_days. Real sessions (opus model, or human first
message with no assistant) are always kept. Idempotent.
"""
from __future__ import annotations

import json
import os

from marrow import cleanup

DAY = 86400.0
NOW = 1_700_000_000.0


def _jsonl(p, lines, mtime_age_days):
    p.write_text("\n".join(json.dumps(o) for o in lines))
    t = NOW - mtime_age_days * DAY
    os.utime(p, (t, t))
    return p


def _headless(p, age):
    return _jsonl(p, [
        {"type": "user", "message": {"role": "user",
         "content": "Compress this file per the rules"}},
        {"type": "assistant", "message": {"role": "assistant",
         "model": "claude-haiku-4-5-20251001",
         "content": [{"type": "text", "text": "x"}]}},
    ], age)


def _real(p, age):
    return _jsonl(p, [
        {"type": "user", "message": {"role": "user", "content": "老公"}},
        {"type": "assistant", "message": {"role": "assistant",
         "model": "claude-opus-4-7",
         "content": [{"type": "text", "text": "hi"}]}},
    ], age)


def test_old_headless_marked_real_kept(tmp_path):
    sub = tmp_path / "-Users-Gabrielle-cc-lab-marrow"
    sub.mkdir()
    h_old = _headless(tmp_path / "h_old.jsonl", 99)
    h_young = _headless(tmp_path / "h_young.jsonl", 0.1)
    r_old = _real(tmp_path / "r_old.jsonl", 99)
    h_deep = _headless(sub / "deep.jsonl", 99)
    to_delete, skipped = cleanup.scan(tmp_path, grace_days=1, now=NOW)
    assert set(to_delete) == {h_old, h_deep}
    skipped_paths = {p for p, _ in skipped}
    assert h_young in skipped_paths and r_old in skipped_paths


def test_apply_unlinks_only_old_headless(tmp_path):
    h = _headless(tmp_path / "h.jsonl", 99)
    r = _real(tmp_path / "r.jsonl", 99)
    rep = cleanup.run(apply=True, projects_dir=tmp_path, grace_days=1, now=NOW)
    assert not h.exists()
    assert r.exists()
    assert rep["deleted"] == 1


def test_idempotent_second_run_deletes_nothing(tmp_path):
    _headless(tmp_path / "h.jsonl", 99)
    cleanup.run(apply=True, projects_dir=tmp_path, grace_days=1, now=NOW)
    rep = cleanup.run(apply=True, projects_dir=tmp_path, grace_days=1, now=NOW)
    assert rep["deleted"] == 0
    assert rep["would_delete"] == 0


def test_empty_or_malformed_jsonl_is_kept_not_deleted(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n\n")
    for p in (empty, bad):
        os.utime(p, (NOW - 99 * DAY, NOW - 99 * DAY))
    to_delete, skipped = cleanup.scan(tmp_path, grace_days=1, now=NOW)
    assert to_delete == []
    assert {p for p, _ in skipped} == {empty, bad}
