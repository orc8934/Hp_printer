"""打印设置的规范化与校验（唯一入口，API / CLI / 测试都走这里）。"""

from __future__ import annotations

import re
from typing import Any

from . import media as media_mod
from .ppd import Catalog, QUALITY_PRESETS

DUPLEX_VALUES = {"off", "long-edge", "short-edge"}
COLOR_VALUES = {"mono", "color", "gray"}
SCALE_VALUES = {"fit", "fill", "none"}
ORIENTATION_VALUES = {"auto", "portrait", "landscape"}
ORDER_VALUES = {"normal", "reverse"}
PAGES_PER_SHEET = {1, 2, 4, 6, 9, 16}
PHOTO_DPI = {300, 600, 1200}
PHOTO_FIT = {"fit", "fill", "none"}
MEDIA_TYPES = {
    "plain", "photo", "glossy", "matte", "brochure", "envelope", "labels", "transparency",
    "cardstock",
}
_RANGE_RE = re.compile(r"^[0-9,\-\s]{0,200}$")


class SettingsError(ValueError):
    """设置不合法（消息直接给用户看）。"""


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是"}


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _pick(payload: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """支持 'photo.media' 这种嵌套写法，也支持直接给嵌套 dict。"""
    if not isinstance(payload, dict):
        return default
    if "." in dotted:
        head, tail = dotted.split(".", 1)
        nested = payload.get(head)
        if isinstance(nested, dict):
            return nested.get(tail, default)
        flat = payload.get(dotted)
        return flat if flat is not None else default
    return payload.get(dotted, default)


def normalize_settings(
    payload: dict[str, Any] | None,
    cfg,
    catalog: Catalog,
    mode: str = "document",
) -> tuple[dict[str, Any], list[str]]:
    """把前端/CLI 传来的设置整理成内部标准结构。"""
    payload = payload or {}
    warnings: list[str] = []

    media_raw = str(_pick(payload, "media", cfg.get("print.media", "A4")) or "").strip()
    media_obj = media_mod.find(media_raw)
    if media_obj is None:
        media_obj = media_mod.find(str(cfg.get("print.media", "A4"))) or media_mod.MEDIA[0]
        warnings.append(f"不认识的纸张尺寸 {media_raw!r}，已改用 {media_obj.label}。")
    media_id = media_mod.normalize_media_id(media_obj.id)
    if media_mod.support_level(media_obj) == "unsupported":
        warnings.append(f"{media_obj.label} 超出 HP 4928 的 215 × 355 mm 上限。")

    media_type = str(_pick(payload, "media_type", cfg.get("print.media_type", "plain")) or "")
    media_type = media_type.strip().lower()
    if media_type not in MEDIA_TYPES:
        media_type = "plain"

    input_slot = str(_pick(payload, "input_slot", "auto") or "auto").strip().lower() or "auto"

    quality = _choice(_pick(payload, "quality", cfg.get("print.quality", "normal")),
                      set(QUALITY_PRESETS), "normal")
    color = _choice(_pick(payload, "color", cfg.get("print.color", "mono")), COLOR_VALUES, "mono")
    duplex = _choice(_pick(payload, "duplex", cfg.get("print.duplex", "off")), DUPLEX_VALUES, "off")
    scale = _choice(_pick(payload, "scale", cfg.get("print.scale", "fit")), SCALE_VALUES, "fit")
    orientation = _choice(_pick(payload, "orientation", cfg.get("print.orientation", "auto")),
                          ORIENTATION_VALUES, "auto")
    output_order = _choice(_pick(payload, "output_order", "normal"), ORDER_VALUES, "normal")

    copies = _int(_pick(payload, "copies", cfg.get("print.copies", 1)), 1, 1, 99)
    pages_per_sheet = _int(_pick(payload, "pages_per_sheet", 1), 1, 1, 16)
    if pages_per_sheet not in PAGES_PER_SHEET:
        pages_per_sheet = 1

    page_ranges = str(_pick(payload, "page_ranges", "") or "").strip()
    if page_ranges and not _RANGE_RE.match(page_ranges):
        raise SettingsError("页码范围只能包含数字、逗号和连字符，例如 1-3,5,8-")
    page_ranges = re.sub(r"\s+", "", page_ranges)

    borderless = _bool(_pick(payload, "borderless", False))
    if borderless and not catalog.has("Borderless", "HPBorderless", "BorderlessPrinting"):
        borderless = False
        warnings.append("该机型官方不支持无边距打印，已按有边距输出。")

    photo = {
        "media": media_mod.normalize_media_id(
            str(_pick(payload, "photo.media", cfg.get("print.photo.media", "4x6in"))), "4x6in"
        ),
        "dpi": _int(_pick(payload, "photo.dpi", cfg.get("print.photo.dpi", 300)), 300, 150, 1200),
        "border_mm": _float(
            _pick(payload, "photo.border_mm", cfg.get("print.photo.border_mm", 3.0)), 3.0, 0.0, 20.0
        ),
        "fit": _choice(_pick(payload, "photo.fit", cfg.get("print.photo.fit", "fit")),
                       PHOTO_FIT, "fit"),
        "auto_rotate": _bool(
            _pick(payload, "photo.auto_rotate", cfg.get("print.photo.auto_rotate", True)), True
        ),
    }
    if photo["dpi"] not in PHOTO_DPI:
        photo["dpi"] = min(PHOTO_DPI, key=lambda value: abs(value - photo["dpi"]))

    native: dict[str, str] = {}
    raw_native = payload.get("native")
    if isinstance(raw_native, dict):
        for name, value in raw_native.items():
            name = str(name).strip()
            value = str(value).strip()
            if not name or not value:
                continue
            option = catalog.get(name)
            if option is None:
                warnings.append(f"驱动没有选项 {name}，已忽略。")
                continue
            if value not in option.choice_names():
                warnings.append(f"{name} 不支持取值 {value}，已忽略。")
                continue
            native[option.name] = value

    if duplex != "off" and pages_per_sheet > 1:
        raise SettingsError("手动双面不能和「每张多页」同时使用，请先把每张页数改为 1。")

    reverse_second_pass = _bool(_pick(payload, "reverse_second_pass",
                                      cfg.get("print.reverse_second_pass", True)), True)

    settings = {
        "mode": "photo" if mode == "photo" else "document",
        "media": media_id,
        "media_type": media_type,
        "input_slot": input_slot,
        "quality": quality,
        "color": color,
        "duplex": duplex,
        "copies": copies,
        "pages_per_sheet": pages_per_sheet,
        "scale": scale,
        "orientation": orientation,
        "output_order": output_order,
        "page_ranges": page_ranges,
        "borderless": borderless,
        "reverse_second_pass": reverse_second_pass,
        "native": native,
        "photo": photo,
    }
    if settings["mode"] == "photo":
        # 照片模式强制照片纸与最佳质量（与 HP 官方照片预设一致）
        settings["media"] = photo["media"]
        if settings["media_type"] == "plain":
            settings["media_type"] = "photo"
        if settings["quality"] in {"draft", "normal"}:
            settings["quality"] = "best"

    if settings["duplex"] != "off":
        warnings.append("手动双面已启用：第一遍只打印奇数页，必须点「继续打印」才会打印第二遍。")
    return settings, _dedupe(warnings)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def describe(settings: dict[str, Any]) -> str:
    """人类可读的一句话摘要，显示在任务卡片上。"""
    media_obj = media_mod.find(settings.get("media"))
    media_label = media_obj.label if media_obj else str(settings.get("media"))
    color = {"mono": "黑白", "gray": "灰度", "color": "彩色"}.get(settings.get("color", "mono"), "")
    duplex = {
        "off": "单面",
        "long-edge": "手动双面·长边翻转",
        "short-edge": "手动双面·短边翻转",
    }.get(settings.get("duplex", "off"), "单面")
    parts = [media_label, color, duplex]
    if int(settings.get("copies", 1)) > 1:
        parts.append(f"{settings['copies']} 份")
    if int(settings.get("pages_per_sheet", 1)) > 1:
        parts.append(f"每张 {settings['pages_per_sheet']} 页")
    quality = {"draft": "草稿", "normal": "标准", "best": "最佳", "photo": "最佳照片"}.get(
        str(settings.get("quality")), ""
    )
    if quality:
        parts.append(quality)
    return " · ".join(part for part in parts if part)
