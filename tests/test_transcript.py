"""Tests for marrow/transcript.py — SessionEnd code-only transcript clean.

Contract: a CC .jsonl session log -> event rows ready for repo.archive_events.
Keep human dialogue (user + assistant text) verbatim; drop tool/thinking/
system/attachment/meta/sidechain noise. Deterministic, no LLM.
"""
from __future__ import annotations

import json

from marrow import transcript


def _w(p, lines):
    p.write_text("\n".join(json.dumps(o) for o in lines))
    return str(p)


def test_keeps_user_and_assistant_text(tmp_path):
    jl = _w(tmp_path / "s.jsonl", [
        {"type": "user", "sessionId": "s1", "timestamp": "2026-05-17T01:00:00Z",
         "message": {"role": "user", "content": "hello marrow"}},
        {"type": "assistant", "sessionId": "s1",
         "timestamp": "2026-05-17T01:00:05Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "hi there"}]}},
    ])
    rows = transcript.clean(jl)
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "hello marrow"), ("assistant", "hi there")]
    assert all(r["session_id"] == "s1" and r["channel"] == "cli" for r in rows)


def test_drops_noise_and_keeps_text_blocks_only(tmp_path):
    jl = _w(tmp_path / "s.jsonl", [
        {"type": "system", "content": "session start"},
        {"type": "attachment", "attachment": {}},
        {"type": "file-history-snapshot", "snapshot": {}},
        {"type": "user", "isMeta": True,
         "message": {"role": "user", "content": "meta noise"}},
        {"type": "user", "isSidechain": True,
         "message": {"role": "user", "content": "subagent turn"}},
        {"type": "assistant", "sessionId": "s1", "timestamp": "t",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "internal monologue"},
             {"type": "tool_use", "name": "Bash", "input": {}},
             {"type": "text", "text": "the real answer"}]}},
        {"type": "user", "sessionId": "s1", "timestamp": "t",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": "exit 0"}]}},
    ])
    rows = transcript.clean(jl)
    assert [r["content"] for r in rows] == ["the real answer"]


def test_strips_buddy_end_of_turn_comment(tmp_path):
    jl = _w(tmp_path / "s.jsonl", [
        {"type": "assistant", "sessionId": "s1", "timestamp": "t",
         "message": {"role": "assistant", "content": [{"type": "text",
             "text": "PONG\n\n<!-- buddy: *adjusts crown* nice -->"}]}},
        {"type": "assistant", "sessionId": "s1", "timestamp": "t",
         "message": {"role": "assistant", "content": [{"type": "text",
             "text": "<!-- BUDDY:\nmultiline\n-->"}]}},  # whole-line -> empty -> dropped
    ])
    rows = transcript.clean(jl)
    assert [r["content"] for r in rows] == ["PONG"]


def test_skips_malformed_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"broken\n{"type":"user","sessionId":"s1","timestamp":"t",'
                 '"message":{"role":"user","content":"ok"}}')
    assert [r["content"] for r in transcript.clean(str(p))] == ["ok"]
