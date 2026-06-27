from marrow.timeline import _tl_anchor_sid


def test_tl_anchor_sid_seq_zero_legacy_marker():
    assert _tl_anchor_sid("sid") == "<!-- tl:sid -->"


def test_tl_anchor_sid_segment_marker():
    assert _tl_anchor_sid("sid", 1) == "<!-- tl:sid:1 -->"
