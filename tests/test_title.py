"""LLM-summarised session title — length cap, turn gathering, dedup, end-to-end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marrow import config, repo, storage, title


@pytest.fixture(autouse=True)
def _seed_db(tmp_path, monkeypatch):
    """Per-test marrow.db with the full schema applied.

    conftest's session-wide DATA_DIR keeps writes off the real db; we
    further pin DATA_DIR to this test's tmp so each test gets a fresh
    schema (no row carry-over between tests).
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    storage.init_db(None).close()
    return tmp_path


# ── truncate_units ─────────────────────────────────────────────────────────


def test_truncate_units_cn_caps_at_eight_chars() -> None:
    text = "微信气泡切割任务完成度报告"  # 14 CJK chars
    out = title.truncate_units(text)
    assert out == "微信气泡切割任务"


def test_truncate_units_en_caps_at_eight_words() -> None:
    text = "refactor wechat split function with bracket depth tracking and merge"
    out = title.truncate_units(text)
    # 8 words including their joining spaces (spaces don't consume a unit).
    assert out.split() == [
        "refactor", "wechat", "split", "function",
        "with", "bracket", "depth", "tracking",
    ]


def test_truncate_units_mixed_cjk_and_ascii() -> None:
    # Mixing CJK chars and ASCII tokens — each counts as 1 unit.
    text = "修复 wx bubble 拆分 bug 的小活儿在这里"
    out = title.truncate_units(text)
    # 修(1) 复(2) wx(3) bubble(4) 拆(5) 分(6) bug(7) 的(8) — cap at 8.
    assert out == "修复 wx bubble 拆分 bug 的"


def test_truncate_units_strips_trailing_punctuation() -> None:
    text = "微信气泡切割。"  # 6 CJK + 1 period
    assert title.truncate_units(text) == "微信气泡切割"
    assert title.truncate_units("hello world.") == "hello world"
    assert title.truncate_units("'quoted'") == "quoted"


def test_truncate_units_empty_safe() -> None:
    assert title.truncate_units("") == ""
    assert title.truncate_units("   ") == ""


# ── gather_turns ───────────────────────────────────────────────────────────


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def test_gather_turns_reads_user_and_assistant(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "hi"}]}},
        {"type": "user", "message": {"role": "user",
                                     "content": [{"type": "text", "text": "fix the split"}]}},
    ])
    turns = title.gather_turns(p)
    assert turns == [("user", "hello"), ("assistant", "hi"), ("user", "fix the split")]


def test_gather_turns_skips_unknown_types_and_malformed(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    with p.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "mode", "mode": "default"}) + "\n")
        f.write("not json line\n")
        f.write(json.dumps({"type": "user", "message": {"content": "good"}}) + "\n")
    turns = title.gather_turns(p)
    assert turns == [("user", "good")]


def test_gather_turns_caps_at_max_turns(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        {"type": "user", "message": {"content": f"u{i}"}} for i in range(10)
    ])
    turns = title.gather_turns(p, max_turns=3)
    assert turns == [("user", "u0"), ("user", "u1"), ("user", "u2")]


def test_gather_turns_missing_path_returns_empty(tmp_path: Path) -> None:
    assert title.gather_turns(tmp_path / "nope.jsonl") == []
    assert title.gather_turns("") == []


# ── summarize end-to-end (LLM mocked) ──────────────────────────────────────


def _seed_session(sid: str, channel: str = "cli", current_title: str = "") -> None:
    repo.upsert_session(sid, "claude-opus-4-7[1m]", channel, title=current_title)


def test_summarize_writes_title_and_audit(monkeypatch, tmp_path: Path) -> None:
    sid = "test-sid-1"
    _seed_session(sid)
    jsonl = tmp_path / "s.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "message": {"content": "帮我改一下微信的split逻辑"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "好的"}]}},
        {"type": "user", "message": {"content": "短句不要合并"}},
    ])

    class _StubClient:
        def __init__(self, *a, **kw) -> None:
            pass

        def call(self, role, body, *, tier="cheap"):
            return "微信气泡切割重构"

    monkeypatch.setattr(title, "LLMClient", _StubClient, raising=False)
    # The module imports LLMClient lazily inside summarize — patch via attr.
    import marrow.llm as llm_mod
    monkeypatch.setattr(llm_mod, "LLMClient", _StubClient)

    result = title.summarize(sid, jsonl_path=str(jsonl))
    assert result == "微信气泡切割重构"

    # sessions.title updated
    row = repo.get_session(sid)
    assert row["title"] == "微信气泡切割重构"

    # audit_log dedup row written
    conn = storage.connect(config.db_path())
    try:
        audit = conn.execute(
            "SELECT action, target_table, target_id, summary FROM audit_log "
            "WHERE action='title_summarize' AND target_id=?",
            (sid,),
        ).fetchone()
    finally:
        conn.close()
    assert audit is not None
    assert audit["target_table"] == "sessions"
    assert audit["summary"] == "微信气泡切割重构"


def test_summarize_skips_when_already_summarized(monkeypatch, tmp_path: Path) -> None:
    sid = "test-sid-2"
    _seed_session(sid, current_title="老 title")
    # Pre-write the dedup audit row.
    conn = storage.connect(config.db_path())
    try:
        conn.execute(
            "INSERT INTO audit_log (target_table, target_id, action, summary) "
            "VALUES ('sessions', ?, 'title_summarize', '老 title')",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()

    called = {"n": 0}

    class _StubClient:
        def __init__(self, *a, **kw) -> None:
            pass

        def call(self, *a, **kw):
            called["n"] += 1
            return "should-not-be-called"

    import marrow.llm as llm_mod
    monkeypatch.setattr(llm_mod, "LLMClient", _StubClient)

    jsonl = tmp_path / "s.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "message": {"content": "x"}},
        {"type": "user", "message": {"content": "y"}},
    ])
    assert title.summarize(sid, jsonl_path=str(jsonl)) is None
    assert called["n"] == 0  # LLM never invoked
    # sessions.title left alone
    assert repo.get_session(sid)["title"] == "老 title"


def test_summarize_skips_when_too_few_user_prompts(monkeypatch, tmp_path: Path) -> None:
    sid = "test-sid-3"
    _seed_session(sid)
    jsonl = tmp_path / "s.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "message": {"content": "only one prompt"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
    ])

    called = {"n": 0}

    class _StubClient:
        def __init__(self, *a, **kw) -> None:
            pass

        def call(self, *a, **kw):
            called["n"] += 1
            return "x"

    import marrow.llm as llm_mod
    monkeypatch.setattr(llm_mod, "LLMClient", _StubClient)

    assert title.summarize(sid, jsonl_path=str(jsonl)) is None
    assert called["n"] == 0


def test_summarize_llm_failure_leaves_row_retry_eligible(monkeypatch, tmp_path: Path) -> None:
    sid = "test-sid-4"
    _seed_session(sid, current_title="fallback head")
    jsonl = tmp_path / "s.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "message": {"content": "u1"}},
        {"type": "user", "message": {"content": "u2"}},
    ])

    class _BoomClient:
        def __init__(self, *a, **kw) -> None:
            pass

        def call(self, *a, **kw):
            raise RuntimeError("provider down")

    import marrow.llm as llm_mod
    monkeypatch.setattr(llm_mod, "LLMClient", _BoomClient)

    assert title.summarize(sid, jsonl_path=str(jsonl)) is None
    # No audit row → next call will retry.
    conn = storage.connect(config.db_path())
    try:
        audit = conn.execute(
            "SELECT 1 FROM audit_log WHERE action='title_summarize' AND target_id=?",
            (sid,),
        ).fetchone()
    finally:
        conn.close()
    assert audit is None
    # Fallback title remains.
    assert repo.get_session(sid)["title"] == "fallback head"


def test_summarize_truncates_overlong_llm_output(monkeypatch, tmp_path: Path) -> None:
    sid = "test-sid-5"
    _seed_session(sid)
    jsonl = tmp_path / "s.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "message": {"content": "u1"}},
        {"type": "user", "message": {"content": "u2"}},
    ])

    class _ChattyClient:
        def __init__(self, *a, **kw) -> None:
            pass

        def call(self, *a, **kw):
            return "这是一个很长的标题超过了八个字的限制"

    import marrow.llm as llm_mod
    monkeypatch.setattr(llm_mod, "LLMClient", _ChattyClient)

    result = title.summarize(sid, jsonl_path=str(jsonl))
    assert result is not None
    assert len(result) <= 8  # truncation enforced
