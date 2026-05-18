"""Tests for marrow/cleanup.py — bleed-stop contract.

The old contract (delete iff entrypoint==sdk-cli) was wrong: sdk-cli
covers real clawbot/Task-agent/worktree human sessions too, so the
reaper would unlink real conversation .jsonl. Until a true headless
signal exists (step4 / ADR-0003), transcript.is_headless is hard-False
and cleanup MUST be a guaranteed no-op for every input — no real
session can ever be lost. Delete-behaviour tests are intentionally
gone; they will be rewritten against the real signal in step4.
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


def test_no_input_is_ever_marked_for_deletion(tmp_path):
    sub = tmp_path / "-Users-Gabrielle-cc-lab-marrow"
    sub.mkdir()
    files = [
        _jsonl(tmp_path / "sdk_old.jsonl", "sdk-cli", mtime_age_days=99),
        _jsonl(tmp_path / "sdk_young.jsonl", "sdk-cli", mtime_age_days=0.1),
        _jsonl(tmp_path / "cli.jsonl", "cli", mtime_age_days=99),
        _jsonl(tmp_path / "legacy.jsonl", None, mtime_age_days=99),
        _jsonl(sub / "deep_sdk.jsonl", "sdk-cli", mtime_age_days=99),
    ]
    to_delete, skipped = cleanup.scan(tmp_path, grace_days=1, now=NOW)
    assert to_delete == []
    assert {p for p, _ in skipped} == set(files)


def test_apply_unlinks_nothing(tmp_path):
    f = _jsonl(tmp_path / "a.jsonl", "sdk-cli", mtime_age_days=99)
    rep = cleanup.run(apply=True, projects_dir=tmp_path, grace_days=1, now=NOW)
    assert f.exists()
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
