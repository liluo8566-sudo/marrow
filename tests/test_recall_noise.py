"""Tests for recall noise-reduction changes.

Covers:
  (a) _entity_strong_hits: generic alias fragments must not match
  (b) _milestone_strong_hits: generic title fragments must not match
  (c) _memes_strong_hits: harness-marker-only queries must not match
  (d) _body_needles: 3-4 char CJK window filtering
  (e) _filter_generic_cjk: the helper itself
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from marrow.recall import (
    _body_needles,
    _entity_strong_hits,
    _filter_generic_cjk,
    _memes_strong_hits,
    _milestone_strong_hits,
)
from marrow.transcript import strip_harness_markers


# ── minimal DB fixture ────────────────────────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            kind TEXT,
            name TEXT,
            fact TEXT,
            aliases TEXT,
            mention_count INTEGER DEFAULT 0,
            created_at TEXT,
            superseded_by INTEGER
        );
        CREATE TABLE memes (
            id INTEGER PRIMARY KEY,
            type TEXT,
            key TEXT,
            value TEXT,
            context TEXT,
            pinned INTEGER DEFAULT 0,
            use_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE milestones (
            id INTEGER PRIMARY KEY,
            scope TEXT,
            date TEXT,
            title TEXT,
            description TEXT,
            pinned INTEGER DEFAULT 0
        );
    """)
    return conn


# ── (a) entity: alias containing 不要小孩 must not match query "不要" alone ──

def test_entity_alias_generic_fragment_no_match():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO entities (id, name, fact, aliases) VALUES (1, '丁克', '不要孩子的生活方式', ?)",
        (json.dumps(["不要小孩", "丁克族"]),),
    )
    # "不要" alone: 不 and 要 are both func chars → filtered, must not hit
    hits = _entity_strong_hits(conn, "不要")
    ids = [r["id"] for r, _ in hits]
    assert 1 not in ids, "generic fragment 不要 must not match entity 丁克"


def test_entity_name_exact_still_matches():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO entities (id, name, fact, aliases) VALUES (1, '丁克', '不要孩子的生活方式', '[]')",
    )
    # "丁克" is a 2-char content word with no func chars — must still hit
    hits = _entity_strong_hits(conn, "丁克")
    ids = [r["id"] for r, _ in hits]
    assert 1 in ids, "exact name 丁克 must still match entity"


# ── (b) milestone: title 嫁给你 must not match query "给你" alone ─────────────

def test_milestone_title_generic_fragment_no_match():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO milestones (id, title, description) VALUES (1, '嫁给你', '想嫁给你')",
    )
    # "给你" → 给 is a func char → filtered out
    hits = _milestone_strong_hits(conn, "给你")
    ids = [r["id"] for r, _ in hits]
    assert 1 not in ids, "generic fragment 给你 must not match milestone 嫁给你"


def test_milestone_title_content_word_still_matches():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO milestones (id, title, description) VALUES (1, '正式在一起', '我们在一起了')",
    )
    # "在一起" — 在(func),一,起 → func_count=1 < 2, bigrams 在一,一起 not in stops → kept
    # query containing "在一起" must still hit the name tier
    hits = _milestone_strong_hits(conn, "我们在一起了")
    ids = [r["id"] for r, _ in hits]
    assert 1 in ids, "content trigram 在一起 must still match milestone name tier"


def test_milestone_title_嫁给你_all_needles_filtered():
    # Verify the filter behavior: 嫁给你 → 给(func)+你(func) = func_count 2 → all needles dropped
    # Title 嫁给你 gets zero name-tier needles → no strong name hit, only FTS/vec can surface it
    conn = _make_conn()
    conn.execute(
        "INSERT INTO milestones (id, title, description) VALUES (1, '嫁给你', '想嫁给你')",
    )
    hits = _milestone_strong_hits(conn, "嫁给你")
    name_hits = [r for r, tier in hits if tier == "name"]
    assert not name_hits, "嫁给你 title yields no name-tier needles after filtering"


# ── (c) meme: [Image #1] query must not match after harness strip ────────────

def test_meme_image_ref_no_match_after_strip():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO memes (id, key, value, status) VALUES (1, 'GPT image gen', 'AI图片生成', 'active')",
    )
    # query is only a harness image ref; after strip_harness_markers it becomes ""
    raw_query = "[Image #1]"
    stripped = strip_harness_markers(raw_query)
    assert stripped == ""
    # with empty query, no hits possible
    hits = _memes_strong_hits(conn, stripped)
    assert hits == [], "empty query after stripping must yield no meme hits"


def test_meme_real_text_still_matches():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO memes (id, key, value, status) VALUES (1, 'GPT image gen', 'AI图片生成', 'active')",
    )
    # real query containing "image" or "gpt" must still hit
    hits_image = _memes_strong_hits(conn, "how to use image generation")
    hits_gpt = _memes_strong_hits(conn, "gpt做图")
    assert any(r["id"] == 1 for r, _ in hits_image), "query with 'image' must match meme"
    assert any(r["id"] == 1 for r, _ in hits_gpt), "query with 'gpt' must match meme"


# ── (d) _body_needles: 3-char windows ─────────────────────────────────────────

def test_body_needles_drops_func_heavy_3char_window():
    # 你说我: 你(func) 说 我(func) → func_count=2 → dropped
    result = _body_needles(["你说我"])
    assert "你说我" not in result[0], "你说我 must be filtered (2 func chars)"


def test_body_needles_drops_stop_bigram_containing_3char_window():
    # 我现在: contains bigram 现在 which is in _CJK_STOP_BIGRAMS → dropped
    result = _body_needles(["我现在"])
    assert "我现在" not in result[0], "我现在 must be filtered (contains stop bigram 现在)"


def test_body_needles_keeps_content_3char_window():
    # 在一起: 在(func) 一 起 → func_count=1 < 2, bigrams: 在一, 一起 — check stop list
    # 在一 not in stop bigrams, 一起 not in stop bigrams → kept
    result = _body_needles(["在一起"])
    assert "在一起" in result[0], "在一起 must be kept (only 1 func char, no stop bigrams)"


# ── (e) _filter_generic_cjk helper ───────────────────────────────────────────

def test_filter_keeps_ascii():
    assert "gpt" in _filter_generic_cjk({"gpt", "image"})
    assert "image" in _filter_generic_cjk({"gpt", "image"})


def test_filter_drops_func_bigram():
    # 给你: 给 is func char → dropped
    assert "给你" not in _filter_generic_cjk({"给你"})


def test_filter_drops_stop_bigram():
    assert "一个" not in _filter_generic_cjk({"一个"})
    assert "现在" not in _filter_generic_cjk({"现在"})


def test_filter_keeps_content_bigram():
    # 丁克: neither char is func, not in stop bigrams → kept
    assert "丁克" in _filter_generic_cjk({"丁克"})


def test_filter_drops_3char_with_2_func():
    # 你说我: 你(func) 说 我(func) → 2 func chars → dropped
    assert "你说我" not in _filter_generic_cjk({"你说我"})


def test_filter_drops_3char_with_stop_bigram():
    # 我现在: contains 现在 (stop bigram) → dropped
    assert "我现在" not in _filter_generic_cjk({"我现在"})


def test_filter_keeps_3char_content_word():
    # 在一起: 在(func), 一, 起 → func_count=1 < 2; bigrams 在一,一起 not in stops → kept
    assert "在一起" in _filter_generic_cjk({"在一起"})


def test_filter_keeps_len1():
    assert "的" in _filter_generic_cjk({"的"})


def test_filter_drops_4char_with_2_func():
    # 在一起了: 在(func), 一, 起, 了(func) → func_count=2 → dropped
    assert "在一起了" not in _filter_generic_cjk({"在一起了"})


def test_filter_keeps_long_content_word():
    # 马自达suv is ASCII+CJK mixed — passes through as ASCII token
    # For pure CJK >4: 快乐每一天 len=5 → kept (len>4 rule)
    assert "快乐每一天" in _filter_generic_cjk({"快乐每一天"})
