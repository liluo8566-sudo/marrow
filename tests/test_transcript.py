"""Tests for marrow/transcript.py — SessionEnd code-only transcript clean.

Contract: a CC .jsonl session log -> event rows ready for repo.archive_events.
Keep human dialogue (user + assistant text) verbatim; drop tool/thinking/
system/attachment/meta/sidechain noise. Deterministic, no LLM.

is_headless: True iff the first user/queue-operation content head matches a
known spawn prompt head. Assistant model names are ignored; conservative:
no prompt-head match -> not headless (keep).
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



def test_missing_file_returns_empty(tmp_path):
    assert transcript.clean(str(tmp_path / "never-written.jsonl")) == []


def test_skips_malformed_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"broken\n{"type":"user","sessionId":"s1","timestamp":"t",'
                 '"message":{"role":"user","content":"ok"}}')
    assert [r["content"] for r in transcript.clean(str(p))] == ["ok"]


# ── is_headless(): spawn prompt-head predicate ───────────────────────────────

def test_spawn_prompt_head_is_headless_even_with_haiku_assistant(tmp_path):
    jl = _w(tmp_path / "h.jsonl", [
        _user("Compress this file per the rules. Output ONLY"),
        _asst("claude-haiku-4-5-20251001"),
        _asst("claude-haiku-4-5-20251001"),
    ])
    assert transcript.is_headless(jl) is True
    assert transcript.clean(jl) == []


def test_sonnet_assistant_with_normal_user_content_is_not_headless(tmp_path):
    jl = _w(tmp_path / "s.jsonl", [
        _user("你是褚屿忱，你要以第一人称写一篇日记"),
        _asst("claude-sonnet-4-6"),
    ])
    assert transcript.is_headless(jl) is False
    assert [r["content"] for r in transcript.clean(jl)] == [
        "你是褚屿忱，你要以第一人称写一篇日记", "reply"]


def test_haiku_assistant_with_normal_user_content_is_not_headless(tmp_path):
    jl = _w(tmp_path / "h-real.jsonl", [
        _user("real human prompt"),
        _asst("claude-haiku-4-5-20251001"),
    ])
    assert transcript.is_headless(jl) is False
    assert [r["content"] for r in transcript.clean(jl)] == [
        "real human prompt", "reply"]


def test_mixed_assistant_models_with_normal_user_content_is_not_headless(tmp_path):
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
    # Models are ignored; the first user prompt-head is the headless signal.
    jl = _w(tmp_path / "syn.jsonl", [
        _user("Compress NEW per the rules. Output ONLY"),
        _asst("<synthetic>", text="injected"),
        _asst("claude-haiku-4-5-20251001"),
    ])
    assert transcript.is_headless(jl) is True


def test_only_synthetic_model_falls_back_to_backstop(tmp_path):
    # Models are ignored; the first user prompt-head is the headless signal.
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


# ── /rewind: reconstruct active chain via parentUuid ─────────────────────────
#
# CC `/rewind` does NOT set isSidechain on rewound turns — it writes a new
# branch whose first turn's parentUuid points back above the rewind point.
# transcript.clean() must walk parentUuid from the file's last uuid back to
# root and keep only that active chain; rewound turns must drop out.

def _u(uuid, parent, content, role="user"):
    """user/assistant turn with uuid+parentUuid wiring."""
    t = "user" if role == "user" else "assistant"
    msg = ({"role": "user", "content": content} if role == "user"
           else {"role": "assistant", "model": "claude-opus-4-7",
                 "content": [{"type": "text", "text": content}]})
    return {"type": t, "sessionId": "s1", "timestamp": "t",
            "uuid": uuid, "parentUuid": parent, "isSidechain": False,
            "message": msg}


def test_no_rewind_keeps_every_turn(tmp_path):
    # baseline: linear chain u1 -> a1 -> u2 -> a2, output identical to before
    jl = _w(tmp_path / "n.jsonl", [
        _u("u1", None, "hi"),
        _u("a1", "u1", "hello", role="assistant"),
        _u("u2", "a1", "more?"),
        _u("a2", "u2", "sure", role="assistant"),
    ])
    assert [r["content"] for r in transcript.clean(jl)] == [
        "hi", "hello", "more?", "sure"]


def test_single_rewind_drops_rewound_branch(tmp_path):
    # chain u1 -> a1 -> u2 -> a2; then user /rewind to a1 and types u3 -> a3.
    # u3.parentUuid jumps back to a1 (skipping u2/a2). u2 and a2 must drop.
    jl = _w(tmp_path / "r.jsonl", [
        _u("u1", None, "hi"),
        _u("a1", "u1", "hello", role="assistant"),
        _u("u2", "a1", "REWOUND-Q"),
        _u("a2", "u2", "REWOUND-A", role="assistant"),
        _u("u3", "a1", "kept-q"),
        _u("a3", "u3", "kept-a", role="assistant"),
    ])
    contents = [r["content"] for r in transcript.clean(jl)]
    assert contents == ["hi", "hello", "kept-q", "kept-a"]
    assert "REWOUND-Q" not in contents and "REWOUND-A" not in contents


def test_nested_rewind_keeps_only_final_chain(tmp_path):
    # u1 -> a1 -> u2 -> a2 (first branch, rewound)
    # then rewind to a1: u3 -> a3 (second branch, also rewound)
    # then rewind to u1: u4 -> a4 (final live chain).
    # Only u1, u4, a4 survive (a1 is the parent of both rewound branches and
    # of u4 — wait: u4 rewinds to u1, so a1 is dropped too).
    jl = _w(tmp_path / "nr.jsonl", [
        _u("u1", None, "root-q"),
        _u("a1", "u1", "DROP-a1", role="assistant"),
        _u("u2", "a1", "DROP-u2"),
        _u("a2", "u2", "DROP-a2", role="assistant"),
        _u("u3", "a1", "DROP-u3"),
        _u("a3", "u3", "DROP-a3", role="assistant"),
        _u("u4", "u1", "final-q"),
        _u("a4", "u4", "final-a", role="assistant"),
    ])
    contents = [r["content"] for r in transcript.clean(jl)]
    assert contents == ["root-q", "final-q", "final-a"]
    assert not any(c.startswith("DROP") for c in contents)


def test_tail_with_null_parent_yields_single_turn(tmp_path):
    # only the root user turn exists; chain = {u1}
    jl = _w(tmp_path / "s1.jsonl", [_u("u1", None, "alone")])
    assert [r["content"] for r in transcript.clean(jl)] == ["alone"]


def test_record_without_uuid_is_not_chain_filtered(tmp_path):
    # a legacy user line without uuid (no parentUuid wiring at all) sits
    # alongside a normal chain. The unwired line must NOT be dropped by the
    # chain filter — only the type / isSidechain / isMeta filters apply.
    jl = _w(tmp_path / "nu.jsonl", [
        {"type": "user", "sessionId": "s1", "timestamp": "t",
         "message": {"role": "user", "content": "legacy-no-uuid"}},
        _u("u1", None, "wired-q"),
        _u("a1", "u1", "wired-a", role="assistant"),
    ])
    contents = [r["content"] for r in transcript.clean(jl)]
    assert "legacy-no-uuid" in contents
    assert contents == ["legacy-no-uuid", "wired-q", "wired-a"]


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


# ── strip_harness_markers ────────────────────────────────────────────────────

def test_command_message_stripped_entirely():
    text = "before <command-message>do not keep this</command-message> after"
    assert transcript.strip_harness_markers(text) == "before after"


def test_command_name_inner_text_kept():
    text = "<command-name>foo</command-name>"
    assert transcript.strip_harness_markers(text) == "foo"


def test_command_args_inner_text_kept():
    text = "<command-args>bar baz</command-args>"
    assert transcript.strip_harness_markers(text) == "bar baz"


def test_image_ref_stripped():
    text = "hello [Image #1] world"
    assert transcript.strip_harness_markers(text) == "hello world"


def test_image_source_stripped():
    text = "see [Image: source: https://example.com/img.png] here"
    assert transcript.strip_harness_markers(text) == "see here"


def test_local_stdout_stripped_entirely():
    text = "start <local-command-stdout>some output\nmore output</local-command-stdout> end"
    assert transcript.strip_harness_markers(text) == "start end"


def test_mixed_markers_only_real_text_remains():
    text = (
        "<command-message>noise</command-message>"
        "actual text "
        "[Image #3]"
        " <command-name>myskill</command-name>"
        " <local-command-stdout>stdout</local-command-stdout>"
    )
    assert transcript.strip_harness_markers(text) == "actual text myskill"


def test_empty_after_stripping_returns_empty():
    text = "<command-message>everything</command-message>"
    assert transcript.strip_harness_markers(text) == ""


def test_no_markers_passthrough():
    text = "hello 你好 world"
    assert transcript.strip_harness_markers(text) == "hello 你好 world"


def test_command_message_multiline_stripped():
    text = "hi\n<command-message>\nline1\nline2\n</command-message>\nbye"
    result = transcript.strip_harness_markers(text)
    assert "line1" not in result and "line2" not in result
    assert "hi" in result and "bye" in result


def test_clean_drops_empty_content_after_stripping(tmp_path):
    # A row whose content is only a command-message tag becomes empty after
    # strip_harness_markers; the `if not text: continue` guard at
    # transcript.py:208 drops it so no empty event row is archived.
    jl = _w(tmp_path / "s.jsonl", [
        {"type": "user", "sessionId": "s1", "timestamp": "t",
         "message": {"role": "user",
                     "content": "<command-message>skip me</command-message>"}},
        {"type": "assistant", "sessionId": "s1", "timestamp": "t",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text",
                                  "text": "real answer"}]}},
    ])
    rows = transcript.clean(jl)
    assert [r["content"] for r in rows] == ["real answer"]
