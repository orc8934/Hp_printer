"""纸张规格：CUPS 介质名、毫米/点换算、HP 4928 支持范围判定。

官方依据（HP DeskJet Ink Advantage Ultra 4928 规格）：
- 介质尺寸：A4 / B5 / A6 / DL 信封
- 自定义尺寸范围：76 × 127 mm ～ 215 × 355 mm
- 无边距打印：不支持（照片会保留安全边距）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0

# HP 4928 官方自定义介质范围（mm）
CUSTOM_MIN = (76.0, 127.0)
CUSTOM_MAX = (215.0, 355.0)


def mm_to_pt(value: float) -> float:
    return float(value) * PT_PER_INCH / MM_PER_INCH


def pt_to_mm(value: float) -> float:
    return float(value) * MM_PER_INCH / PT_PER_INCH


def mm_to_px(value_mm: float, dpi: int) -> int:
    return max(1, int(round(float(value_mm) / MM_PER_INCH * int(dpi))))


@dataclass(frozen=True)
class Media:
    id: str                 # CUPS / PPD 里的介质名
    label: str              # 界面上显示的名字
    width_mm: float
    height_mm: float
    category: str = "paper"  # paper | photo | envelope | card
    aliases: tuple[str, ...] = ()
    note: str = ""

    @property
    def is_landscape(self) -> bool:
        return self.width_mm > self.height_mm

    def size_pt(self) -> tuple[float, float]:
        return mm_to_pt(self.width_mm), mm_to_pt(self.height_mm)

    def size_mm(self) -> tuple[float, float]:
        return self.width_mm, self.height_mm

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "width_mm": round(self.width_mm, 1),
            "height_mm": round(self.height_mm, 1),
            "category": self.category,
            "note": self.note,
            "support": support_level(self),
        }


# 顺序即界面默认顺序：常用在前
MEDIA: tuple[Media, ...] = (
    Media("A4", "A4 (210 × 297 mm)", 210.0, 297.0, "paper"),
    Media("A5", "A5 (148 × 210 mm)", 148.0, 210.0, "paper"),
    Media("A6", "A6 (105 × 148 mm)", 105.0, 148.0, "paper"),
    Media("B5", "B5 (176 × 250 mm)", 176.0, 250.0, "paper"),
    Media(
        "4x6in",
        "6 寸相纸 / 4R (102 × 152 mm)",
        101.6,
        152.4,
        "photo",
        aliases=("4x6", "4x6in", "10x15", "4r"),
        note="6 寸 = 4×6 英寸 = 4R，按自定义介质走（在官方 76×127–215×355 mm 范围内）",
    ),
    Media(
        "10x15cm",
        "10 × 15 cm 相纸",
        100.0,
        150.0,
        "photo",
        aliases=("10x15cm",),
        note="比 4R 略小 2 mm，若边缘被裁切请改用 6 寸/4R",
    ),
    Media("13x18cm", "5 寸相纸 / 5×7 英寸 (127 × 178 mm)", 127.0, 177.8, "photo",
          aliases=("5x7in",)),
    Media("20x25cm", "8 × 10 英寸相纸 (203 × 254 mm)", 203.2, 254.0, "photo",
          aliases=("8x10in",)),
    Media("3x5in", "3 × 5 英寸卡片 (76 × 127 mm)", 76.2, 127.0, "card",
          aliases=("3x5",), note="正好是该机型自定义尺寸的下限"),
    Media("Executive", "Executive (184 × 267 mm)", 184.15, 266.7, "paper"),
    Media("DL", "DL 信封 (110 × 220 mm)", 110.0, 220.0, "envelope"),
    Media("C5", "C5 信封 (162 × 229 mm)", 162.0, 229.0, "envelope"),
    Media("C6", "C6 信封 (114 × 162 mm)", 114.0, 162.0, "envelope"),
    Media("COM10", "#10 信封 (105 × 241 mm)", 104.8, 241.3, "envelope",
          aliases=("#10", "No10", "10envelope")),
    # 以下超出 215 mm 宽度，HP 4928 不支持（保留是为了驱动能识别，界面默认隐藏）
    Media("Letter", "Letter (216 × 279 mm)", 215.9, 279.4, "paper"),
    Media("Legal", "Legal (216 × 356 mm)", 215.9, 355.6, "paper"),
)

_INDEX: dict[str, Media] = {}
for _item in MEDIA:
    _INDEX[_item.id.lower()] = _item
    for _alias in _item.aliases:
        _INDEX.setdefault(_alias.lower(), _item)


def find(name: str | None) -> Media | None:
    """按 CUPS 名或别名查找；支持 "Custom.101.6x152.4mm" 这类写法。"""
    if not name:
        return None
    key = str(name).strip().lower()
    if key in _INDEX:
        return _INDEX[key]
    custom = parse_custom(key)
    if custom:
        width, height = custom
        return Media(f"Custom.{width:g}x{height:g}mm", f"自定义 {width:g} × {height:g} mm",
                     width, height, "paper")
    return None


def parse_custom(key: str) -> tuple[float, float] | None:
    """解析 Custom.100x150mm / custom.101.6x152.4mm。"""
    if not key.startswith("custom."):
        return None
    body = key[len("custom."):]
    if body.endswith("mm"):
        body = body[:-2]
    parts = body.replace("X", "x").split("x")
    if len(parts) != 2:
        return None
    try:
        width = float(parts[0])
        height = float(parts[1])
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def within_custom_range(width_mm: float, height_mm: float) -> bool:
    width = min(width_mm, height_mm)
    height = max(width_mm, height_mm)
    return CUSTOM_MIN[0] <= width <= CUSTOM_MAX[0] and CUSTOM_MIN[1] <= height <= CUSTOM_MAX[1]


def support_level(media: Media) -> str:
    """HP 4928 视角的支持程度：official / custom / unsupported。"""
    if media.id in {"A4", "B5", "A6", "DL"}:
        return "official"
    if within_custom_range(media.width_mm, media.height_mm):
        return "custom"
    return "unsupported"


def list_media(category: str | None = None, include_unsupported: bool = False) -> list[Media]:
    items: Iterable[Media] = MEDIA
    if category:
        items = [item for item in items if item.category == category]
    if not include_unsupported:
        items = [item for item in items if support_level(item) != "unsupported"]
    return list(items)


def normalize_media_id(name: str | None, fallback: str = "A4") -> str:
    """把用户/驱动给的介质名规范成 CUPS 可用的名字。"""
    media = find(name or "")
    if media is None:
        media = find(fallback) or MEDIA[0]
    if media.id.startswith("Custom."):
        width, height = media.width_mm, media.height_mm
        return f"Custom.{width:g}x{height:g}mm"
    return media.id


def orientation_of(media: Media, requested: str = "auto") -> str:
    """返回 portrait / landscape。"""
    if requested in {"portrait", "landscape"}:
        return requested
    return "landscape" if media.is_landscape else "portrait"


def rotated(media: Media) -> Media:
    """宽高互换（用于横向排版）。"""
    return Media(media.id, media.label, media.height_mm, media.width_mm, media.category,
                 media.aliases, media.note)
