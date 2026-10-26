"""纸张规格换算与 HP 4928 支持范围。"""

from __future__ import annotations

from hp4928 import media


def test_mm_pt_roundtrip():
    assert round(media.mm_to_pt(25.4), 3) == 72.0
    assert round(media.pt_to_mm(72.0), 3) == 25.4
    assert media.mm_to_px(25.4, 300) == 300


def test_find_aliases():
    assert media.find("A4").id == "A4"
    assert media.find("4r").id == "4x6in"
    assert media.find("10x15cm").width_mm == 100.0
    assert media.find("nonexistent") is None


def test_custom_media_parsing():
    custom = media.find("Custom.120x180mm")
    assert custom is not None
    assert custom.width_mm == 120.0
    assert media.parse_custom("custom.101.6x152.4mm") == (101.6, 152.4)
    assert media.normalize_media_id("Custom.120x180mm").startswith("Custom.")


def test_support_levels():
    assert media.support_level(media.find("A4")) == "official"
    assert media.support_level(media.find("4x6in")) == "custom"
    assert media.support_level(media.find("Legal")) == "unsupported"
    assert media.support_level(media.find("Letter")) == "unsupported"


def test_six_inch_dimensions():
    photo = media.find("4x6in")
    assert (photo.width_mm, photo.height_mm) == (101.6, 152.4)
    width_pt, height_pt = photo.size_pt()
    assert round(width_pt, 1) == 288.0
    assert round(height_pt, 1) == 432.0


def test_list_media_hides_unsupported_by_default():
    ids = [item.id for item in media.list_media()]
    assert "A4" in ids and "4x6in" in ids
    assert "Legal" not in ids
    assert "Legal" in [item.id for item in media.list_media(include_unsupported=True)]
