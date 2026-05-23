"""Tests for marrow/transcript.py — SessionEnd code-only transcript clean.

Contract: a CC .jsonl session log -> event rows ready for repo.archive_events.
Keep human dialogue (user + assistant text) verbatim; drop tool/thinking/
system/attachment/meta/sidechain noise. Deterministic, no LLM.

is_headless (ADR-0004): True iff the set of assistant .message.model
(type=="assistant", excluding "<synthetic>") is non-empty AND every model
prefix-matches some config [transcript].worker_models entry. Empty model
set -> backstop on first user/queue-operation content head vs known spawn
prompt heads. Conservative: no match -> not headless (keep).
"""
from __future__ import annotations

import json

from marrow import transcript


def _w(p, lines):
    p.write_text("\n".join(json.dumps(o) for o in lines))
    return str(p)


def _user(content, **kw):
    o = {"type": "user", "sessionId": "s1", "timestamp": "t",
         "message": {"role": "user", "content": content}}
    o.update(kw)
    return o


def _asst(model, text="reply", **kw):
    o = {"type": "assistant", "sessionId": "s1", "timestamp": "t",
         "message": {"role": "assistant", "model": model,
                     "content": [{"type": "text", "text": text}]}}
    o.update(kw)
    return o


# ── clean(): keep human dialogue verbatim ────────────────────────────────────

def test_keeps_user_and_assistant_text(tmp_path):
    jl = _w(tmp_path / "s.jsonl", [
        {"type": "user", "sessionId": "s1", "timestamp": "2026-05-17T01:00:00Z",
         "message": {"role": "user", "content": "hello marrow"}},
        {"type": "assistant", "sessionId": "s1",
         "timestamp": "2026-05-17T01:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
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
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [
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
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text",
             "text": "PONG\n\n<!-- buddy: *adjusts crown* nice -->"}]}},
        {"type": "assistant", "sessionId": "s1", "timestamp": "t",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text",
             "text": "<!-- BUDDY:\nmultiline\n-->"}]}},
    ])
    rows = transcript.clean(jl)
    assert [r["content"] for r in rows] == ["PONG"]


def test_missing_file_returns_empty(tmp_path):
    assert transcript.clean(str(tmp_path / "never-written.jsonl")) == []


def test_skips_malformed_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"broken\n{"type":"user","sessionId":"s1","timestamp":"t",'
                 '"message":{"role":"user","content":"ok"}}')
    assert [r["content"] for r in transcript.clean(str(p))] == ["ok"]


# ── is_headless(): ADR-0004 assistant-model-set predicate ────────────────────

def test_all_haiku_assistant_is_headless(tmp_path):
    jl = _w(tmp_path / "h.jsonl", [
        _user("Compress this file per the rules. Output ONLY"),
        _asst("claude-haiku-4-5-20251001"),
        _asst("claude-haiku-4-5-20251001"),
    ])
    assert transcript.is_headless(jl) is True
    assert transcript.clean(jl) == []


def test_all_sonnet_assistant_is_headless(tmp_path):
    jl = _w(tmp_path / "s.jsonl", [
        _user("你是褚屿忱，你要以第一人称写一篇日记"),
        _asst("claude-sonnet-4-6"),
    ])
    assert transcript.is_headless(jl) is True
    assert transcript.clean(jl) == []


def test_mixed_worker_and_opus_is_not_headless(tmp_path):
    # real session: opus present alongside a worker model -> keep
    jl = _w(tmp_path / "m.jsonl", [
        _user("real human prompt"),
        _asst("claude-haiku-4-5-20251001", text="cheap aside"),
        _asst("claude-opus-4-7", text="the real work"),
    ])
    assert transcript.is_headless(jl) is False
    assert [r["content"] for r in transcript.clean(jl)] == [
        "real human prompt", "cheap aside", "the real work"]


def test_all_opus_is_not_headless(tmp_path):
    jl = _w(tmp_path / "o.jsonl", [
        _user("老公 clawbot 真实对话"),
        _asst("claude-opus-4-7", text="real reply"),
        _asst("claude-opus-4-6", text="legacy opus reply"),
    ])
    assert transcript.is_headless(jl) is False
    assert [r["content"] for r in transcript.clean(jl)] == [
        "老公 clawbot 真实对话", "real reply", "legacy opus reply"]


def test_synthetic_model_is_dropped_from_set(tmp_path):
    # <synthetic> excluded; remaining set is all-haiku -> headless
    jl = _w(tmp_path / "syn.jsonl", [
        _user("Compress NEW per the rules. Output ONLY"),
        _asst("<synthetic>", text="injected"),
        _asst("claude-haiku-4-5-20251001"),
    ])
    assert transcript.is_headless(jl) is True


def test_only_synthetic_model_falls_back_to_backstop(tmp_path):
    # set empties to {} after dropping <synthetic> -> backstop on prompt head
    jl = _w(tmp_path / "syn2.jsonl", [
        _user("You are a ruthless markdown compressor for instruction"),
        _asst("<synthetic>", text="injected"),
    ])
    assert transcript.is_headless(jl) is True


def test_empty_model_set_spawn_prompt_head_is_headless(tmp_path):
    # spawn exited before any assistant flush; first user is a spawn prompt
    jl = _w(tmp_path / "e.jsonl", [
        {"type": "queue-operation", "content":
            "You compress ONE long session of dialogue into a digest"},
        _user("You compress ONE long session of dialogue into a digest"),
    ])
    assert transcript.is_headless(jl) is True
    assert transcript.clean(jl) == []


def test_empty_model_set_stitch_prompt_head_is_headless(tmp_path):
    jl = _w(tmp_path / "st.jsonl", [
        _user("Extract per-episode affect from the session below."),
    ])
    assert transcript.is_headless(jl) is True


def test_empty_model_set_human_first_message_is_not_headless(tmp_path):
    jl = _w(tmp_path / "hu.jsonl", [
        _user("老公帮我看下这个 bug 好不好"),
    ])
    assert transcript.is_headless(jl) is False
    assert [r["content"] for r in transcript.clean(jl)] == [
        "老公帮我看下这个 bug 好不好"]


def test_no_assistant_no_user_is_not_headless(tmp_path):
    jl = _w(tmp_path / "n.jsonl", [
        {"type": "system", "content": "session start"},
        {"type": "file-history-snapshot", "snapshot": {}},
    ])
    assert transcript.is_headless(jl) is False


def test_missing_file_is_not_headless(tmp_path):
    # safest: a missing/unflushed transcript is kept, never auto-deleted
    assert transcript.is_headless(str(tmp_path / "nope.jsonl")) is False


def test_garbage_file_is_not_headless(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text("{not json\n\nstill not json\n")
    assert transcript.is_headless(str(p)) is False


def test_sdk_cli_real_session_kept(tmp_path):
    # entrypoint sdk-cli is NOT a headless marker; opus model -> keep
    jl = _w(tmp_path / "c.jsonl", [
        {"type": "user", "sessionId": "h1", "timestamp": "t",
         "entrypoint": "sdk-cli",
         "message": {"role": "user", "content": "老公 clawbot 真实对话"}},
        {"type": "assistant", "sessionId": "h1", "timestamp": "t",
         "entrypoint": "sdk-cli",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text", "text": "real reply"}]}},
    ])
    assert transcript.is_headless(jl) is False
    assert [r["content"] for r in transcript.clean(jl)] == [
        "老公 clawbot 真实对话", "real reply"]
