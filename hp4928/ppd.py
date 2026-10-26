"""打印机能力目录：解析 HP 驱动（PPD / lpoptions），并把界面选项映射成 CUPS 参数。

设计要点
--------
1. 真实机器上，选项**全部来自已安装的 HP 驱动**（`lpoptions -l` 或 /etc/cups/ppd/*.ppd），
   所以「高级设置」与官方驱动逐项一致，不做任何臆造。
2. 没有驱动时（虚拟打印机 / 尚未安装 CUPS），使用内置的 HP 4928 能力画像，
   依据 HP 官方规格：A4/B5/A6/DL、自定义 76×127–215×355mm、手动双面、不支持无边距。
3. 界面分两层：语义化控件（纸张/质量/色彩/双面…）+ 原生驱动选项（原样透传）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import media as media_mod

# PWG / IPP 标准介质类型（PPD 没有 MediaType 时使用）
PWG_MEDIA_TYPE = {
    "plain": "stationery",
    "plain-letterhead": "stationery-letterhead",
    "photo": "photographic",
    "glossy": "photographic-glossy",
    "matte": "photographic-matte",
    "envelope": "envelope",
    "labels": "labels",
    "transparency": "transparency",
    "cardstock": "cardstock",
    "brochure": "stationery",
}

QUALITY_PRESETS = {
    # key: (中文名, 目标 dpi 列表, IPP print-quality 值)
    "draft": ("草稿（省墨）", (300, 150, 200), 3),
    "normal": ("标准", (600, 300, 1200), 4),
    "best": ("最佳 / 高质量", (1200, 600, 2400), 5),
    "photo": ("最佳照片", (1200, 2400, 600), 5),
}


# --------------------------------------------------------------------- 数据模型
@dataclass
class Choice:
    name: str
    label: str = ""
    is_default: bool = False

    def display(self) -> str:
        return self.label.strip() or self.name


@dataclass
class Option:
    name: str
    label: str = ""
    choices: list[Choice] = field(default_factory=list)
    default: str = ""

    def choice_names(self) -> list[str]:
        return [item.name for item in self.choices]

    def find(self, pattern: str) -> Choice | None:
        """按名字或标签模糊匹配取值。"""
        needle = pattern.strip().lower()
        for choice in self.choices:
            if choice.name.lower() == needle:
                return choice
        for choice in self.choices:
            text = f"{choice.name} {choice.label}".lower()
            if needle in text:
                return choice
        return None

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label or self.name,
            "default": self.default,
            "choices": [
                {"name": item.name, "label": item.display(), "default": item.is_default}
                for item in self.choices
            ],
        }


@dataclass
class Catalog:
    options: dict[str, Option] = field(default_factory=dict)
    source: str = "builtin"          # lpoptions | ppd | builtin
    description: str = ""
    path: str = ""
    warnings: list[str] = field(default_factory=list)

    # 大小写不敏感的取值
    def get(self, *names: str) -> Option | None:
        for name in names:
            if not name:
                continue
            if name in self.options:
                return self.options[name]
            for key, option in self.options.items():
                if key.lower() == name.lower():
                    return option
        return None

    def has(self, *names: str) -> bool:
        return self.get(*names) is not None

    def public(self, bound: dict[str, str] | None = None) -> list[dict[str, Any]]:
        bound = {v: k for k, v in (bound or {}).items() if v}
        out = []
        for option in self.options.values():
            data = option.public()
            option_value = option.name if option.name in bound else ""
            data["bound_to"] = bound.get(option.name, "")
            out.append(data)
        return out


# --------------------------------------------------------------------- PPD 解析
_OPENUI_RE = re.compile(r"^\*OpenUI\s+\*([^/:\s]+)\s*(?:/([^:]*))?:\s*(\w+)", re.IGNORECASE)
_CLOSEUI_RE = re.compile(r"^\*CloseUI:\s*\*?([^:\s]+)", re.IGNORECASE)
_CHOICE_RE = re.compile(r"^\*([^/:\s]+)\s+([^/:\s]+)\s*(?:/([^:]*))?:\s*(.*)$")
_DEFAULT_RE = re.compile(r"^\*Default([^:]+):\s*(\S+)", re.IGNORECASE)
_NICKNAME_RE = re.compile(r'^\*NickName:\s*"?(.*?)"?\s*$', re.IGNORECASE)
_MODELNAME_RE = re.compile(r'^\*ModelName:\s*"?(.*?)"?\s*$', re.IGNORECASE)

# 这些不是真实打印选项，解析时过滤
_SKIP_OPTIONS = {
    "cupsfilter", "cupsfilter2", "cupsmanualcopies", "cupsmodelnumber", "cupsjobsheets",
    "cupsprintertype", "cupsprotocol", "cupsversion", "cupsencryption", "cupsimperativejobs",
    "cupsipftoolarge", "cupslanmanager", "cupsportmonitor", "cupsoperationssupported",
    "pageregion", "pagerregion", "page region", "hpsetup", "hpoption", "jobpatchfile",
    "jobpatchfile2", "outputbin", "cupsmediasize",
}


def parse_ppd(text: str) -> Catalog:
    """解析 PPD 文本，提取 OpenUI 块内的选项。"""
    catalog = Catalog(source="ppd")
    current: Option | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("*%") or line.startswith("%"):
            continue

        nickname = _NICKNAME_RE.match(line) or _MODELNAME_RE.match(line)
        if nickname and not catalog.description:
            catalog.description = nickname.group(1).strip()
            continue

        open_match = _OPENUI_RE.match(line)
        if open_match:
            name = open_match.group(1)
            label = (open_match.group(2) or "").strip()
            if name.lower() in _SKIP_OPTIONS:
                current = None
                continue
            current = Option(name=name, label=label)
            catalog.options[name] = current
            continue

        if _CLOSEUI_RE.match(line):
            current = None
            continue

        default_match = _DEFAULT_RE.match(line)
        if default_match and current is not None:
            name, value = default_match.group(1).strip(), default_match.group(2).strip()
            if name.lower() == current.name.lower():
                current.default = value
                for choice in current.choices:
                    if choice.name == value:
                        choice.is_default = True
            continue

        if current is None:
            continue

        choice_match = _CHOICE_RE.match(line)
        if choice_match:
            name, value, label = choice_match.group(1), choice_match.group(2), choice_match.group(3)
            if name.lower() != current.name.lower():
                continue
            if value.strip() == "":
                continue
            current.choices.append(Choice(name=value, label=(label or "").strip()))
            continue

    # 清理：没有取值、或只有 1 个取值且是 "None" 的选项没意义
    for name in list(catalog.options):
        option = catalog.options[name]
        if not option.choices:
            del catalog.options[name]
            continue
        if not option.default:
            defaults = [item.name for item in option.choices if item.is_default]
            option.default = defaults[0] if defaults else option.choices[0].name
        if not any(item.is_default for item in option.choices):
            for item in option.choices:
                item.is_default = item.name == option.default
    return catalog


def parse_lpoptions(text: str) -> Catalog:
    """解析 `lpoptions -l` 输出：

    PageSize/Media Size: 4x6 *A4 A5
    """
    catalog = Catalog(source="lpoptions")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        left, right = line.split(":", 1)
        left = left.strip()
        if not left:
            continue
        if "/" in left:
            name, label = left.split("/", 1)
        else:
            name, label = left, ""
        name = name.strip().lstrip("*")
        if not name or " " in name:
            continue
        option = Option(name=name, label=label.strip())
        for token in right.split():
            is_default = token.startswith("*")
            value = token.lstrip("*")
            if not value:
                continue
            option.choices.append(Choice(name=value, label="", is_default=is_default))
            if is_default:
                option.default = value
        if not option.choices:
            continue
        if not option.default:
            option.default = option.choices[0].name
        catalog.options[name] = option
    return catalog


# --------------------------------------------------------------------- 内置画像
def _opt(name: str, label: str, choices: Iterable[tuple[str, str]], default: str) -> Option:
    option = Option(name=name, label=label, default=default)
    for value, choice_label in choices:
        option.choices.append(Choice(name=value, label=choice_label, is_default=(value == default)))
    return option


def builtin_catalog(cfg=None) -> Catalog:
    """HP DeskJet Ink Advantage 4928 官方能力画像（无驱动时使用）。"""
    media_choices = [
        (item.id, item.label) for item in media_mod.list_media(include_unsupported=True)
    ]
    catalog = Catalog(
        source="builtin",
        description="HP DeskJet Ink Advantage 4928（内置能力画像，HP 官方规格）",
        path="",
    )
    catalog.warnings.append(
        "当前未读取到已安装的 HP 驱动，选项来自内置官方能力画像；"
        "接上打印机并配置 CUPS 队列后会自动切换为驱动真实选项。"
    )
    for option in (
        _opt("PageSize", "Media Size / 纸张尺寸", media_choices, "A4"),
        _opt(
            "MediaType",
            "Media Type / 纸张类型",
            [
                ("Plain", "普通纸 Plain"),
                ("Photo", "照片纸 Photo"),
                ("Brochure", "宣传册纸 Brochure"),
                ("Cardstock", "卡片纸 Cardstock"),
                ("Labels", "标签 Labels"),
                ("Transparency", "投影胶片 Transparency"),
                ("Envelope", "信封 Envelope"),
            ],
            "Plain",
        ),
        _opt("InputSlot", "Media Source / 纸张来源", [("Auto", "自动选择"), ("Main", "主纸盒")], "Auto"),
        _opt(
            "Resolution",
            "Print Quality / 打印质量",
            [
                ("300dpi", "草稿 300 dpi"),
                ("600dpi", "标准 600 dpi"),
                ("1200dpi", "最佳 1200 dpi"),
            ],
            "600dpi",
        ),
        _opt(
            "ColorMode",
            "Color / 色彩",
            [("Color", "彩色"), ("Gray", "灰度（黑白）")],
            "Gray",
        ),
    ):
        catalog.options[option.name] = option
    return catalog


# --------------------------------------------------------------------- 加载
def _run(command: list[str], timeout: int = 20) -> str:
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"LC_ALL": "C", "LANG": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    return (proc.stdout or "") + (proc.stderr or "")


def ppd_path_for(cfg, queue: str) -> Path | None:
    explicit = str(cfg.get("printer.cups.ppd_path") or "").strip()
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if queue:
        candidates.append(Path("/etc/cups/ppd") / f"{queue}.ppd")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_catalog(cfg, queue: str = "") -> Catalog:
    """优先 lpoptions（驱动运行态），其次 PPD 文件，最后内置画像。"""
    queue = queue or str(cfg.get("printer.name", "") or "")
    lpoptions = str(cfg.get("printer.cups.lpoptions", "lpoptions") or "lpoptions")
    timeout = int(cfg.get("printer.cups.timeout", 60) or 60)

    if shutil.which(lpoptions) and queue:
        try:
            text = _run([lpoptions, "-l", "-p", queue], timeout=timeout)
            if text.strip():
                catalog = parse_lpoptions(text)
                if catalog.options:
                    catalog.path = str(ppd_path_for(cfg, queue) or "")
                    catalog.description = _driver_description(cfg, queue) or queue
                    return catalog
        except (OSError, subprocess.SubprocessError):
            pass

    ppd = ppd_path_for(cfg, queue)
    if ppd is not None:
        try:
            catalog = parse_ppd(ppd.read_text(encoding="utf-8", errors="replace"))
            if catalog.options:
                catalog.path = str(ppd)
                catalog.description = catalog.description or f"PPD: {ppd.name}"
                return catalog
        except OSError:
            pass

    builtin = builtin_catalog(cfg)
    if queue:
        builtin.warnings.insert(
            0, f"未能读取队列 {queue} 的驱动选项（CUPS 未安装或队列不存在），已回退到内置能力画像。"
        )
    return builtin


def _driver_description(cfg, queue: str) -> str:
    """从 lpstat 里拿驱动描述（PPD NickName）。"""
    lpstat = str(cfg.get("printer.cups.lpstat", "lpstat") or "lpstat")
    if not shutil.which(lpstat) or not queue:
        return ""
    try:
        text = _run([lpstat, "-l", "-p", queue], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("description:") or "interface:" in stripped.lower():
            continue
        if stripped and not stripped.lower().startswith("printer "):
            return stripped[:120]
    return ""


# --------------------------------------------------------------------- 角色识别
ROLE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "media": ("PageSize", "media", "PaperSize", "HPPaperSize"),
    "media_type": ("MediaType", "media-type", "HPPaperType", "PaperType"),
    "input_slot": ("InputSlot", "InputSource", "MediaSource", "HPSource"),
    "quality": ("Resolution", "print-quality", "HPPrintQuality", "Quality"),
    "color": ("ColorMode", "print-color-mode", "output-mode", "HPColorMode", "Color"),
    "duplex": ("Duplex", "sides", "HP Duplex"),
    "borderless": ("Borderless", "HPBorderless", "BorderlessPrinting"),
}

_COLOR_PATTERNS = {
    "mono": ("gray", "grey", "mono", "black", "grayscale", "grayscale"),
    "color": ("color", "colour", "rgb", "cmyk"),
    "gray": ("gray", "grey", "grayscale"),
}


def resolve_roles(catalog: Catalog) -> dict[str, str]:
    """角色 → 真实的驱动选项名。"""
    roles: dict[str, str] = {}
    for role, candidates in ROLE_CANDIDATES.items():
        option = catalog.get(*candidates)
        if option is not None:
            roles[role] = option.name
    return roles


def _dpi_of(choice: Choice) -> int:
    match = re.search(r"(\d{3,4})\s*(?:x\s*\d{3,4})?\s*dpi", f"{choice.name} {choice.label}", re.I)
    if match:
        return int(match.group(1))
    return 0


def quality_value(catalog: Catalog, roles: dict[str, str], quality: str) -> tuple[str, str] | None:
    """返回 (选项名, 取值)。按预设里的 dpi 优先级依次尝试精确匹配。"""
    quality = quality if quality in QUALITY_PRESETS else "normal"
    option = catalog.get(roles.get("quality", "")) if roles.get("quality") else None
    if option is None:
        return None

    if option.name.lower() in {"print-quality", "hpoption_print_quality"}:
        target = QUALITY_PRESETS[quality][2]
        choice = option.find(str(target)) or option.find(
            {3: "draft", 4: "normal", 5: "high"}.get(target, "normal")
        )
        return (option.name, choice.name) if choice else None

    # Resolution 类：先按优先级找完全匹配，再退化为最接近首选值
    targets = QUALITY_PRESETS[quality][1]
    by_dpi: dict[int, Choice] = {}
    for choice in option.choices:
        dpi = _dpi_of(choice)
        if dpi and dpi not in by_dpi:
            by_dpi[dpi] = choice
    for target in targets:
        if target in by_dpi:
            return option.name, by_dpi[target].name
    if by_dpi:
        closest = min(by_dpi, key=lambda dpi: abs(dpi - targets[0]))
        return option.name, by_dpi[closest].name

    # 没有 dpi 就按文字猜
    keyword = {"draft": "draft", "normal": "normal", "best": "best", "photo": "photo"}[quality]
    choice = option.find(keyword)
    return (option.name, choice.name) if choice else None


def color_value(catalog: Catalog, roles: dict[str, str], color: str) -> tuple[str, str] | None:
    option = catalog.get(roles.get("color", "")) if roles.get("color") else None
    if option is None:
        return None
    wants = color if color in {"mono", "color", "gray"} else "mono"
    if option.name.lower() in {"print-color-mode", "output-mode"}:
        mapping = {"mono": "monochrome", "color": "color", "gray": "monochrome"}
        choice = option.find(mapping[wants]) or option.find("monochrome")
        return (option.name, choice.name) if choice else None
    for pattern in _COLOR_PATTERNS[wants]:
        choice = option.find(pattern)
        if choice:
            return option.name, choice.name
    # 黑白机器：有 Color 就选 Color，没有就保持默认
    choice = option.choices[0] if option.choices else None
    return (option.name, choice.name) if choice else None


def media_value(catalog: Catalog, roles: dict[str, str], media_id: str) -> tuple[str, str] | None:
    option = catalog.get(roles.get("media", "")) if roles.get("media") else None
    target = media_mod.normalize_media_id(media_id)
    if option is None:
        return None
    choice = option.find(target)
    if choice:
        return option.name, choice.name
    # 别名兜底：4x6in 在部分驱动里叫 4x6、10x15cm 或 na_index-4x6
    media_obj = media_mod.find(target)
    if media_obj is not None:
        for alias in (media_obj.id, *media_obj.aliases):
            choice = option.find(alias)
            if choice:
                return option.name, choice.name
    # 自定义尺寸：PPD 里通常只有 Custom.WxHmm 模板
    if target.startswith("Custom."):
        for item in option.choices:
            if item.name.lower().startswith("custom"):
                return option.name, item.name
    # 驱动里没有这个尺寸：不臆造，交给上层用 IPP 标准 media 属性兜底
    return None


def media_type_value(catalog: Catalog, roles: dict[str, str], media_type: str) -> tuple[str, str] | None:
    option = catalog.get(roles.get("media_type", "")) if roles.get("media_type") else None
    if option is None:
        return None
    wanted = (media_type or "plain").lower()
    aliases = {
        "plain": ("plain", "普通", "stationery"),
        "photo": ("photo", "照片", "photographic"),
        "glossy": ("glossy", "photo", "光泽", "photographic"),
        "matte": ("matte", "photo", "亚光", "哑光"),
        "brochure": ("brochure", "宣传"),
        "envelope": ("envelope", "信封"),
        "labels": ("label", "标签"),
        "transparency": ("transparency", "胶片"),
        "cardstock": ("cardstock", "card", "卡片"),
    }
    for pattern in aliases.get(wanted, (wanted,)):
        choice = option.find(pattern)
        if choice:
            return option.name, choice.name
    return None


def input_slot_value(catalog: Catalog, roles: dict[str, str], slot: str) -> tuple[str, str] | None:
    option = catalog.get(roles.get("input_slot", "")) if roles.get("input_slot") else None
    if option is None:
        return None
    wanted = (slot or "auto").lower()
    patterns = {
        "auto": ("auto", "自动"),
        "main": ("main", "tray1", "tray-1", "主"),
        "photo": ("photo", "照片"),
        "manual": ("manual", "手动"),
    }.get(wanted, (wanted,))
    for pattern in patterns:
        choice = option.find(pattern)
        if choice:
            return option.name, choice.name
    return None


# --------------------------------------------------------------------- 参数生成
def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是"}


@dataclass
class ResolvedOptions:
    args: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mapped: dict[str, str] = field(default_factory=dict)   # 语义键 → "Name=Value"

    def as_dict(self) -> dict[str, Any]:
        return {"args": self.args, "warnings": self.warnings, "mapped": self.mapped}


def build_lp_options(
    settings: dict[str, Any],
    catalog: Catalog,
    roles: dict[str, str] | None = None,
) -> ResolvedOptions:
    """把界面设置翻译成 `lp -o` 参数。

    重要：手动双面在应用层实现（先打奇数页，翻面后点按钮再打偶数页），
    绝不向驱动发送 two-sided —— 该机型没有自动双面器。
    """
    roles = roles if roles is not None else resolve_roles(catalog)
    out = ResolvedOptions()
    args: list[str] = []

    def emit(name: str, value: str, role_key: str = "") -> None:
        token = f"{name}={value}"
        if token not in args:
            args.append(token)
        if role_key:
            out.mapped[role_key] = token

    # 1) 纸张尺寸
    media_id = media_mod.normalize_media_id(settings.get("media"), "A4")
    media_obj = media_mod.find(media_id)
    paired = media_value(catalog, roles, media_id)
    if paired:
        emit(*paired, "media")
        if paired[1] != media_id and media_obj is not None and media_obj.id != paired[1]:
            out.warnings.append(f"驱动里没有 {media_id}，已按驱动中的 {paired[1]} 提交。")
    else:
        # 驱动没有该尺寸（或没有 PageSize 选项）：用 IPP 标准属性兜底
        emit("media", media_id, "media")
        if catalog.has(*ROLE_CANDIDATES["media"]):
            out.warnings.append(
                f"驱动未声明纸张尺寸 {media_id}，已通过 IPP media 属性提交，"
                "实际用纸可能被打印机替换为默认尺寸。"
            )

    if media_obj is not None and media_mod.support_level(media_obj) == "unsupported":
        out.warnings.append(
            f"{media_obj.label} 超出该机型 215 × 355 mm 的上限，可能无法进纸或报错。"
        )

    # 2) 纸张类型
    media_type = str(settings.get("media_type") or "plain").lower()
    paired = media_type_value(catalog, roles, media_type)
    if paired:
        emit(*paired, "media_type")
    else:
        pwg = PWG_MEDIA_TYPE.get(media_type)
        if pwg:
            emit("media-type", pwg, "media_type")

    # 3) 纸张来源
    paired = input_slot_value(catalog, roles, str(settings.get("input_slot") or "auto"))
    if paired:
        emit(*paired, "input_slot")

    # 4) 打印质量
    quality = str(settings.get("quality") or "normal").lower()
    paired = quality_value(catalog, roles, quality)
    if paired:
        emit(*paired, "quality")
    else:
        # 没有 Resolution 选项时用 IPP 标准属性
        emit("print-quality", str(QUALITY_PRESETS.get(quality, QUALITY_PRESETS["normal"])[2]),
             "quality")

    # 5) 色彩
    color = str(settings.get("color") or "mono").lower()
    paired = color_value(catalog, roles, color)
    if paired:
        emit(*paired, "color")
    else:
        emit("print-color-mode", "monochrome" if color in {"mono", "gray"} else "color", "color")

    # 6) 双面：手动双面由应用分两遍完成，这里只显式声明单面
    duplex = str(settings.get("duplex") or "off").lower()
    emit("sides", "one-sided", "sides")
    if duplex in {"long-edge", "short-edge"}:
        out.warnings.append(
            "已启用手动双面：第一遍打印全部奇数页，打印机停稳后按提示翻面，"
            "点击「继续打印」才会输出第二遍（偶数页）。"
        )

    # 7) 版式
    pages_per_sheet = int(settings.get("pages_per_sheet") or 1)
    if pages_per_sheet > 1:
        emit("number-up", str(pages_per_sheet), "number_up")
    scale = str(settings.get("scale") or "fit").lower()
    if scale in {"fit", "fill", "none"}:
        emit("print-scaling", scale, "scaling")

    orientation = str(settings.get("orientation") or "auto").lower()
    if orientation == "landscape":
        emit("orientation-requested", "4", "orientation")
    elif orientation == "portrait":
        emit("orientation-requested", "3", "orientation")

    order = str(settings.get("output_order") or "normal").lower()
    if order in {"normal", "reverse"}:
        emit("outputorder", order, "output_order")

    ranges = str(settings.get("page_ranges") or "").strip()
    if ranges:
        emit("page-ranges", ranges, "page_ranges")

    # 8) 无边距（该机型官方不支持，只有驱动真的提供时才发）
    borderless_option = catalog.get(roles.get("borderless", "")) if roles.get("borderless") else None
    if _bool(settings.get("borderless", False)):
        if borderless_option is not None:
            choice = borderless_option.find("true") or borderless_option.find("on")
            if choice:
                emit(borderless_option.name, choice.name, "borderless")
        else:
            out.warnings.append(
                "该机型官方规格为「不支持无边距打印」，已忽略无边距请求，照片会保留安全边距。"
            )

    # 9) 原生选项透传（用户直接改驱动选项时优先）
    native = settings.get("native") or {}
    if isinstance(native, dict):
        for name, value in native.items():
            name = str(name).strip()
            value = str(value).strip()
            if not name or not value:
                continue
            option = catalog.get(name)
            if option is None:
                out.warnings.append(f"驱动没有选项 {name}，已忽略。")
                continue
            if value not in option.choice_names():
                out.warnings.append(f"{name} 不支持取值 {value}，已忽略。")
                continue
            prefix = f"{name}="
            args = [item for item in args if not item.startswith(prefix)]
            emit(name, value)

    out.args = args
    return out


# --------------------------------------------------------------------- 界面 Schema
def _choices_for(catalog: Catalog, roles: dict[str, str], role: str,
                 labels: dict[str, str] | None = None) -> list[dict]:
    option = catalog.get(roles.get(role, "")) if roles.get(role) else None
    if option is None:
        return []
    items = []
    for choice in option.choices:
        label = (labels or {}).get(choice.name, choice.display())
        items.append({"value": choice.name, "label": label, "default": choice.is_default})
    return items


def build_schema(cfg, catalog: Catalog, roles: dict[str, str] | None = None) -> dict[str, Any]:
    """生成前端「高级设置」结构：语义控件 + 原生驱动选项。"""
    roles = roles if roles is not None else resolve_roles(catalog)
    defaults = {
        "media": cfg.get("print.media", "A4"),
        "media_type": cfg.get("print.media_type", "plain"),
        "input_slot": "auto",
        "quality": cfg.get("print.quality", "normal"),
        "color": cfg.get("print.color", "mono"),
        "duplex": cfg.get("print.duplex", "off"),
        "copies": int(cfg.get("print.copies", 1) or 1),
        "pages_per_sheet": int(cfg.get("print.pages_per_sheet", 1) or 1),
        "scale": cfg.get("print.scale", "fit"),
        "orientation": cfg.get("print.orientation", "auto"),
        "page_ranges": cfg.get("print.page_ranges", ""),
        "reverse_second_pass": bool(cfg.get("print.reverse_second_pass", True)),
        "borderless": False,
        "output_order": "normal",
        "photo": {
            "media": cfg.get("print.photo.media", "4x6in"),
            "dpi": int(cfg.get("print.photo.dpi", 300) or 300),
            "border_mm": float(cfg.get("print.photo.border_mm", 3.0) or 0),
            "fit": cfg.get("print.photo.fit", "fit"),
            "auto_rotate": bool(cfg.get("print.photo.auto_rotate", True)),
        },
    }

    media_options = [
        {
            "value": item.id,
            "label": item.label,
            "support": media_mod.support_level(item),
            "category": item.category,
            "note": item.note,
        }
        for item in media_mod.MEDIA
    ]

    paper_fields = [
        {
            "key": "media",
            "type": "select",
            "label": "纸张尺寸",
            "hint": "HP 4928 官方支持 A4/B5/A6/DL，自定义范围 76×127–215×355 mm",
            "options": media_options,
            "drive": _drive(roles, "media", catalog),
        },
        {
            "key": "media_type",
            "type": "select",
            "label": "纸张类型",
            "options": _choices_for(catalog, roles, "media_type")
            or [
                {"value": "plain", "label": "普通纸 Plain"},
                {"value": "photo", "label": "照片纸 Photo"},
                {"value": "brochure", "label": "宣传册纸 Brochure"},
                {"value": "cardstock", "label": "卡片纸 Cardstock"},
                {"value": "labels", "label": "标签 Labels"},
                {"value": "transparency", "label": "投影胶片 Transparency"},
                {"value": "envelope", "label": "信封 Envelope"},
            ],
            "drive": _drive(roles, "media_type", catalog),
        },
    ]
    if catalog.has(*ROLE_CANDIDATES["input_slot"]):
        paper_fields.append(
            {
                "key": "input_slot",
                "type": "select",
                "label": "纸张来源",
                "options": _choices_for(catalog, roles, "input_slot"),
                "drive": _drive(roles, "input_slot", catalog),
            }
        )

    quality_choices = [
        {"value": "draft", "label": "草稿（省墨 300 dpi）"},
        {"value": "normal", "label": "标准（600 dpi）"},
        {"value": "best", "label": "最佳（1200 dpi）"},
        {"value": "photo", "label": "最佳照片（照片纸 + 1200 dpi）"},
    ]
    color_choices = [
        {"value": "mono", "label": "黑白 / 灰度"},
        {"value": "color", "label": "彩色"},
        {"value": "gray", "label": "灰度（照片）"},
    ]

    groups = [
        {
            "id": "paper",
            "label": "纸张 / 质量",
            "fields": paper_fields
            + [
                {
                    "key": "quality",
                    "type": "select",
                    "label": "打印质量",
                    "options": quality_choices,
                    "drive": _drive(roles, "quality", catalog),
                },
                {
                    "key": "color",
                    "type": "select",
                    "label": "色彩",
                    "options": color_choices,
                    "drive": _drive(roles, "color", catalog),
                },
            ],
        },
        {
            "id": "layout",
            "label": "版式 / 双面",
            "fields": [
                {
                    "key": "duplex",
                    "type": "select",
                    "label": "双面打印",
                    "hint": "本机型无自动双面器，属手动双面：先打奇数页，翻面后点按钮再打偶数页",
                    "options": [
                        {"value": "off", "label": "单面（默认）"},
                        {"value": "long-edge", "label": "双面 · 长边翻转（像翻书）"},
                        {"value": "short-edge", "label": "双面 · 短边翻转（像翻日历）"},
                    ],
                    "drive": "manual",
                },
                {
                    "key": "reverse_second_pass",
                    "type": "bool",
                    "label": "第二遍逆序送纸",
                    "hint": "手动双面的标准做法：第二遍从最后一页倒着打，翻面后页码顺序才正确",
                    "visible_if": {"duplex": ["long-edge", "short-edge"]},
                },
                {
                    "key": "copies",
                    "type": "int",
                    "label": "份数",
                    "min": 1,
                    "max": 99,
                },
                {
                    "key": "pages_per_sheet",
                    "type": "select",
                    "label": "每张纸页数",
                    "options": [
                        {"value": "1", "label": "1 页（正常）"},
                        {"value": "2", "label": "2 页"},
                        {"value": "4", "label": "4 页"},
                        {"value": "6", "label": "6 页"},
                        {"value": "9", "label": "9 页"},
                        {"value": "16", "label": "16 页"},
                    ],
                },
                {
                    "key": "scale",
                    "type": "select",
                    "label": "缩放方式",
                    "options": [
                        {"value": "fit", "label": "适应纸张（保留完整内容）"},
                        {"value": "fill", "label": "铺满（可能裁切边缘）"},
                        {"value": "none", "label": "原始大小（居中）"},
                    ],
                },
                {
                    "key": "orientation",
                    "type": "select",
                    "label": "方向",
                    "options": [
                        {"value": "auto", "label": "自动（跟文档）"},
                        {"value": "portrait", "label": "纵向"},
                        {"value": "landscape", "label": "横向"},
                    ],
                },
                {
                    "key": "output_order",
                    "type": "select",
                    "label": "出纸顺序",
                    "options": [
                        {"value": "normal", "label": "正序（1,2,3…）"},
                        {"value": "reverse", "label": "逆序（末页先出）"},
                    ],
                },
            ],
        },
        {
            "id": "photo",
            "label": "照片 / 6 寸相纸",
            "mode": "photo",
            "fields": [
                {
                    "key": "photo.media",
                    "type": "select",
                    "label": "相纸尺寸",
                    "options": [
                        {"value": item.id, "label": item.label, "note": item.note}
                        for item in media_mod.list_media(category="photo")
                    ],
                },
                {
                    "key": "photo.fit",
                    "type": "select",
                    "label": "照片怎么放",
                    "options": [
                        {"value": "fit", "label": "完整显示（留白边）"},
                        {"value": "fill", "label": "铺满裁切（不变形）"},
                        {"value": "none", "label": "原始尺寸（1:1）"},
                    ],
                },
                {
                    "key": "photo.dpi",
                    "type": "select",
                    "label": "输出分辨率",
                    "options": [
                        {"value": "300", "label": "300 dpi（标准）"},
                        {"value": "600", "label": "600 dpi（更细腻，更慢）"},
                        {"value": "1200", "label": "1200 dpi（最佳，最慢）"},
                    ],
                },
                {
                    "key": "photo.border_mm",
                    "type": "number",
                    "label": "安全边距 (mm)",
                    "min": 0,
                    "max": 20,
                    "step": 0.5,
                    "hint": "该机型不支持无边距，建议 3 mm",
                },
                {
                    "key": "photo.auto_rotate",
                    "type": "bool",
                    "label": "自动旋转匹配纸向",
                },
            ],
        },
        {
            "id": "advanced",
            "label": "高级",
            "fields": [
                {
                    "key": "page_ranges",
                    "type": "text",
                    "label": "页码范围",
                    "placeholder": "留空=全部；例：1-3,5,8-",
                    "hint": "支持 1-3、5、8- 等写法；手动双面时按所选范围重新分奇偶页",
                },
                {
                    "key": "borderless",
                    "type": "bool",
                    "label": "无边距打印",
                    "hint": "官方规格：该机型不支持无边距，勾选也不会生效"
                    if not catalog.has(*ROLE_CANDIDATES["borderless"])
                    else "由驱动 Borderless 选项控制",
                    "disabled": not catalog.has(*ROLE_CANDIDATES["borderless"]),
                },
            ],
        },
    ]

    notes = list(catalog.warnings)
    notes.append("手动双面：第一遍打印奇数页后服务会暂停，必须点击「继续打印」才会输出第二遍。")
    if not catalog.has(*ROLE_CANDIDATES["borderless"]):
        notes.append("无边距：HP 4928 官方不支持，6 寸相纸会保留约 3 mm 安全边距。")

    return {
        "driver": {
            "source": catalog.source,
            "description": catalog.description,
            "path": catalog.path,
            "roles": roles,
            "option_count": len(catalog.options),
        },
        "defaults": defaults,
        "groups": groups,
        "native_options": catalog.public(roles),
        "notes": notes,
        "capabilities": {
            "auto_duplex": False,
            "manual_duplex": True,
            "borderless": catalog.has(*ROLE_CANDIDATES["borderless"]),
            "scan": True,
            "media_custom_range_mm": [list(media_mod.CUSTOM_MIN), list(media_mod.CUSTOM_MAX)],
        },
    }


def _drive(roles: dict[str, str], role: str, catalog: Catalog) -> str:
    """给前端显示「该控件最终映射到哪个驱动选项」。"""
    name = roles.get(role)
    if not name:
        return ""
    return name
