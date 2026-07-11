"""book_* MCP tools (Phase 3 D-2): qidu config resolution, endpoint
realignment, auth headers, book_page client-side slicing."""
from __future__ import annotations

import json

import pytest

from marrow import config, daemon


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def qidu_cfg(monkeypatch):
    monkeypatch.setattr(
        config, "load",
        lambda: {"qidu": {"api_base": "http://example.com/api", "token": "tok-123"}},
    )


@pytest.fixture()
def no_qidu_cfg(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {"qidu": {"api_base": "", "token": ""}})


def _capture_request(monkeypatch, body: bytes):
    captured = {}

    def fake_urlopen(req, timeout=5):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k: v for k, v in req.header_items()}
        captured["data"] = req.data
        return FakeResponse(body)

    monkeypatch.setattr(daemon.urllib.request, "urlopen", fake_urlopen)
    return captured


# ── missing config ──────────────────────────────────────────────────────────

def test_book_list_missing_config_returns_error(no_qidu_cfg):
    out = daemon.book_list()
    assert out == {"error": daemon._QIDU_ERROR}


def test_book_annotate_missing_config_returns_error(no_qidu_cfg):
    out = daemon.book_annotate("bk-1", 42, "text")
    assert "not configured" in out


# ── GET tools: URL + auth header ────────────────────────────────────────────

def test_book_list_url_and_auth(qidu_cfg, monkeypatch):
    captured = _capture_request(monkeypatch, json.dumps([{"id": "bk-1"}]).encode())
    out = daemon.book_list()
    assert captured["url"] == "http://example.com/api/books"
    assert captured["headers"]["Authorization"] == "Bearer tok-123"
    assert out == [{"id": "bk-1"}]


def test_book_chapters_url(qidu_cfg, monkeypatch):
    captured = _capture_request(monkeypatch, b"[]")
    daemon.book_chapters("bk-1")
    assert captured["url"] == "http://example.com/api/books/bk-1/chapters"


def test_book_progress_url(qidu_cfg, monkeypatch):
    captured = _capture_request(monkeypatch, b"{}")
    daemon.book_progress("bk-1")
    assert captured["url"] == "http://example.com/api/books/bk-1/progress"


def test_book_annotations_no_chapter_filter(qidu_cfg, monkeypatch):
    captured = _capture_request(monkeypatch, b"[]")
    daemon.book_annotations("bk-1")
    assert captured["url"] == "http://example.com/api/books/bk-1/annotations"


def test_book_annotations_with_chapter_filter(qidu_cfg, monkeypatch):
    captured = _capture_request(monkeypatch, b"[]")
    daemon.book_annotations("bk-1", chapter_id="ch-2")
    assert captured["url"] == "http://example.com/api/books/bk-1/annotations?chapter_id=ch-2"


# ── book_page slicing ────────────────────────────────────────────────────────

def test_book_page_slices_client_side(qidu_cfg, monkeypatch):
    paragraphs = [{"id": f"p-{i}"} for i in range(50)]
    captured = _capture_request(monkeypatch, json.dumps(paragraphs).encode())
    out = daemon.book_page("bk-1", "ch-1", offset=10, limit=5)
    assert captured["url"] == "http://example.com/api/books/bk-1/chapters/ch-1/paragraphs"
    assert out == {
        "total": 50,
        "offset": 10,
        "paragraphs": paragraphs[10:15],
    }


def test_book_page_default_offset_limit(qidu_cfg, monkeypatch):
    paragraphs = [{"id": f"p-{i}"} for i in range(3)]
    _capture_request(monkeypatch, json.dumps(paragraphs).encode())
    out = daemon.book_page("bk-1", "ch-1")
    assert out["offset"] == 0
    assert out["total"] == 3
    assert out["paragraphs"] == paragraphs


# ── book_annotate POST body ─────────────────────────────────────────────────

def test_book_annotate_posts_body_without_parent(qidu_cfg, monkeypatch):
    captured = _capture_request(monkeypatch, json.dumps({"annotation_id": 7}).encode())
    out = daemon.book_annotate("bk-1", highlight_id=42, text="批注内容")
    assert captured["method"] == "POST"
    assert captured["url"] == "http://example.com/api/books/bk-1/annotations/ai"
    assert captured["headers"]["Authorization"] == "Bearer tok-123"
    body = json.loads(captured["data"])
    assert body == {"highlight_id": 42, "text": "批注内容"}
    assert out == "Annotation written: 7"


def test_book_annotate_posts_body_with_parent(qidu_cfg, monkeypatch):
    captured = _capture_request(monkeypatch, json.dumps({"annotation_id": 9}).encode())
    daemon.book_annotate("bk-1", highlight_id=42, text="回复", parent_id=5)
    body = json.loads(captured["data"])
    assert body == {"highlight_id": 42, "text": "回复", "parent_id": 5}


def test_book_annotate_http_error_returns_message(qidu_cfg, monkeypatch):
    def fake_urlopen(req, timeout=5):
        raise OSError("connection refused")
    monkeypatch.setattr(daemon.urllib.request, "urlopen", fake_urlopen)
    out = daemon.book_annotate("bk-1", highlight_id=42, text="text")
    assert "Failed to write annotation" in out
    assert "connection refused" in out
