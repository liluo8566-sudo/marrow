"""Tests for marrow/cleanup.py — standalone sdk-cli jsonl disk reaper.

Contract: delete a CC .jsonl iff transcript.is_headless()==True (spawned
`claude -p`, entrypoint=sdk-cli) AND the file is older than grace_days.
Interactive (cli) and legacy (no entrypoint) sessions are always kept.
Pure scan() + run(apply); dry-run default.
"""
from __future__ import annotations

import json
import os

from marrow import cleanup

DAY = 86400.0
NOW = 1_700_000_000.0


def _jsonl(p, entrypoint, mtime_age_days):
    line = {"type": "user", "message": {"role": "user", "content": "x"}}
    if entrypoint is not None:
        line["entrypoint"] = entrypoint
    p.write_text(json.dumps(line))
    t = NOW - mtime_age_days * DAY
    os.utime(p, (t, t))
    return p


def test_old_sdk_cli_jsonl_is_deleted(tmp_path):
    f = _jsonl(tmp_path / "a.jsonl", "sdk-cli", mtime_age_days=5)
    to_delete, skipped = cleanup.scan(tmp_path, grace_days=1, now=NOW)
    assert f in to_delete
    assert skipped == []


def test_young_sdk_cli_jsonl_is_protected(tmp_path):
    f = _jsonl(tmp_path / "a.jsonl", "sdk-cli", mtime_age_days=0.5)
    to_delete, skipped = cleanup.scan(tmp_path, grace_days=1, now=NOW)
    assert to_delete == []
    assert (f, "too young") in skipped


def test_interactive_and_legacy_sessions_always_kept(tmp_path):
    cli = _jsonl(tmp_path / "cli.jsonl", "cli", mtime_age_days=99)
    legacy = _jsonl(tmp_path / "legacy.jsonl", None, mtime_age_days=99)
    to_delete, skipped = cleanup.scan(tmp_path, grace_days=1, now=NOW)
    assert to_delete == []
    assert (cli, "kept") in skipped
    assert (legacy, "kept") in skipped


def test_scans_nested_project_subdirs(tmp_path):
    sub = tmp_path / "-Users-Gabrielle-cc-lab-marrow"
    sub.mkdir()
    f = _jsonl(sub / "deep.jsonl", "sdk-cli", mtime_age_days=5)
    to_delete, _ = cleanup.scan(tmp_path, grace_days=1, now=NOW)
    assert f in to_delete


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


def test_dry_run_deletes_nothing(tmp_path):
    f = _jsonl(tmp_path / "a.jsonl", "sdk-cli", mtime_age_days=5)
    rep = cleanup.run(apply=False, projects_dir=tmp_path, grace_days=1, now=NOW)
    assert f.exists()
    assert rep["applied"] is False
    assert rep["would_delete"] == 1


def test_apply_deletes_only_headless_then_idempotent(tmp_path):
    doomed = _jsonl(tmp_path / "a.jsonl", "sdk-cli", mtime_age_days=5)
    keep = _jsonl(tmp_path / "b.jsonl", "cli", mtime_age_days=5)
    rep = cleanup.run(apply=True, projects_dir=tmp_path, grace_days=1, now=NOW)
    assert not doomed.exists()
    assert keep.exists()
    assert rep["deleted"] == 1
    rep2 = cleanup.run(apply=True, projects_dir=tmp_path, grace_days=1, now=NOW)
    assert rep2["deleted"] == 0
