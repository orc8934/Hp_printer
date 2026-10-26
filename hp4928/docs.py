"""文档流水线：识别 → 转 PDF → 选页 → 拆分（手动双面）/ 照片排版。

支持：
- PDF（原样，按页抽取）
- Word .docx / .doc（LibreOffice headless 转 PDF）
- 图片 JPEG/PNG/BMP/TIFF/GIF/WEBP（按相纸或文档纸排版）
"""

from __future__ import annotations

import io
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import media as media_mod
from .proc import run, which

WORD_EXTENSIONS = {".doc", ".docx", ".odt", ".rtf", ".wps"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".gif", ".webp"}
PDF_EXTENSIONS = {".pdf"}

KIND_PDF = "pdf"
KIND_WORD = "word"
KIND_IMAGE = "image"


class DocumentError(RuntimeError):
    """文档处理失败，消息直接给用户看。"""


@dataclass
class DocumentInfo:
    kind: str
    path: Path
    pages: int = 0
    bytes: int = 0
    title: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.path.name,
            "pages": self.pages,
            "bytes": self.bytes,
            "title": self.title,
        }


# --------------------------------------------------------------------- 类型识别
def sniff_kind(path: Path, filename: str = "") -> str:
    """用文件头判断类型，扩展名只作参考（防止改扩展名骗过校验）。"""
    name = (filename or path.name).lower()
    suffix = Path(name).suffix
    head = b""
    try:
        with open(path, "rb") as handle:
            head = handle.read(1024)
    except OSError as exc:
        raise DocumentError(f"读取文件失败：{exc}") from exc

    if head.startswith(b"%PDF"):
        return KIND_PDF
    if head.startswith(b"PK\x03\x04"):
        if suffix in WORD_EXTENSIONS or b"word/" in head:
            return KIND_WORD
        if suffix in {".odt", ".docx"}:
            return KIND_WORD
        raise DocumentError("这个压缩包不是支持的文档格式（只支持 .docx/.odt）")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE 复合文档：.doc / .wps / .rtf(少见)
        if suffix in WORD_EXTENSIONS or suffix == "":
            return KIND_WORD
        raise DocumentError("这是旧版 Office 复合文档，但扩展名不认识，请用 .doc 或 .docx")
    if head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG") or head.startswith(
        (b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*")
    ):
        return KIND_IMAGE
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return KIND_IMAGE
    if suffix in PDF_EXTENSIONS:
        raise DocumentError("扩展名是 PDF 但文件头不对，文件可能已损坏")
    if suffix in WORD_EXTENSIONS:
        raise DocumentError("无法识别 Word 文件（文件可能损坏）")
    if suffix in IMAGE_EXTENSIONS:
        raise DocumentError("无法识别的图片格式")
    raise DocumentError(f"不支持的文件类型：{suffix or '未知'}（支持 PDF / DOC / DOCX / 常见图片）")


# --------------------------------------------------------------------- PDF 基础
def _pypdf_reader(path: Path):
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 - pypdf 抛的异常类型很杂
        raise DocumentError(f"PDF 打不开：{exc}") from exc
    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise DocumentError("PDF 有密码保护，请先解密后再上传")
        except DocumentError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DocumentError(f"PDF 解密失败：{exc}") from exc
    return reader


def page_count(pdf_path: Path, cfg=None) -> int:
    """页数：pypdf 优先，pdfinfo 兜底。"""
    try:
        return len(_pypdf_reader(pdf_path).pages)
    except DocumentError:
        raise
    except Exception:  # noqa: BLE001
        pass
    pdfinfo = which(str((cfg.get("conversion.pdfinfo") if cfg else "") or "pdfinfo"))
    if pdfinfo:
        result = run([pdfinfo, str(pdf_path)], timeout=30)
        match = re.search(r"^Pages:\s+(\d+)", result.output, re.MULTILINE)
        if match:
            return int(match.group(1))
    raise DocumentError("无法读取 PDF 页数，文件可能已损坏")


def pdf_page_size(pdf_path: Path) -> tuple[float, float]:
    """第一页尺寸（点）。"""
    reader = _pypdf_reader(pdf_path)
    if not reader.pages:
        return 0.0, 0.0
    box = reader.pages[0].mediabox
    return float(box.width), float(box.height)


def extract_pages(source: Path, indices: Iterable[int], target: Path, reverse: bool = False) -> int:
    """按 0 基页码抽取页面生成新 PDF，返回页数。"""
    from pypdf import PdfWriter

    reader = _pypdf_reader(source)
    total = len(reader.pages)
    picked = [index for index in indices if 0 <= index < total]
    if reverse:
        picked = list(reversed(picked))
    if not picked:
        raise DocumentError("没有可打印的页面")
    writer = PdfWriter()
    for index in picked:
        writer.add_page(reader.pages[index])
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "wb") as handle:
            writer.write(handle)
    except OSError as exc:
        raise DocumentError(f"写入 PDF 失败：{exc}") from exc
    return len(picked)


def merge_pdfs(sources: list[Path], target: Path) -> int:
    from pypdf import PdfWriter

    writer = PdfWriter()
    pages = 0
    for source in sources:
        reader = _pypdf_reader(source)
        for page in reader.pages:
            writer.add_page(page)
            pages += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as handle:
        writer.write(handle)
    return pages


# --------------------------------------------------------------------- 页码范围
def parse_page_ranges(spec: str, total: int) -> list[int]:
    """把 "1-3,5,8-" 解析成 1 基页码列表（去重、保序）。空字符串=全部。"""
    if total <= 0:
        raise DocumentError("文档没有页面")
    spec = (spec or "").strip()
    if not spec:
        return list(range(1, total + 1))

    picked: list[int] = []
    seen: set[int] = set()
    for token in re.split(r"[,\s，]+", spec):
        if not token:
            continue
        match = re.fullmatch(r"(\d*)\s*-\s*(\d*)", token)
        if match:
            start_raw, end_raw = match.group(1), match.group(2)
            start = int(start_raw) if start_raw else 1
            end = int(end_raw) if end_raw else total
        elif token.isdigit():
            start = end = int(token)
        else:
            raise DocumentError(f"页码范围写法不对：{token}（示例：1-3,5,8-）")
        if start < 1 or end > total:
            raise DocumentError(f"页码 {token} 超出范围（共 {total} 页）")
        if start > end:
            start, end = end, start
        for page in range(start, end + 1):
            if page not in seen:
                seen.add(page)
                picked.append(page)
    if not picked:
        raise DocumentError("页码范围没有选中任何页")
    return picked


def split_pages(spec: str, total: int) -> tuple[list[int], list[int]]:
    """手动双面：返回 (第一遍, 第二遍) 的 1 基页码。

    以「所选范围」为新文档编号，第一位是奇数页 → 第一遍。
    """
    selected = parse_page_ranges(spec, total)
    first = [page for position, page in enumerate(selected, start=1) if position % 2 == 1]
    second = [page for position, page in enumerate(selected, start=1) if position % 2 == 0]
    return first, second


# --------------------------------------------------------------------- Word 转换
def find_soffice(cfg) -> str | None:
    configured = str(cfg.get("conversion.soffice") or "").strip()
    if configured and Path(configured).exists():
        return configured
    for name in ("soffice", "libreoffice"):
        found = which(name)
        if found:
            return found
    # 常见安装位置（snap / flatpak 之外的发行版路径）
    for candidate in (
        "/usr/bin/soffice",
        "/usr/lib/libreoffice/program/soffice",
        "/opt/libreoffice/program/soffice",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def convert_word(source: Path, out_dir: Path, cfg, profile_dir: Path | None = None) -> Path:
    """用 LibreOffice 无界面模式把 Word 转成 PDF。"""
    soffice = find_soffice(cfg)
    if not soffice:
        raise DocumentError(
            "未安装 LibreOffice，无法转换 Word。请在服务器执行："
            "sudo apt install -y libreoffice-writer fonts-noto-cjk"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = profile_dir or (cfg.soffice_profile_dir / "default")
    profile.mkdir(parents=True, exist_ok=True)
    profile_uri = "file:///" + str(profile).replace("\\", "/").lstrip("/")
    timeout = float(cfg.get("conversion.timeout", 180) or 180)

    command = [
        soffice,
        "--headless",
        "--norestore",
        "--nologo",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
        f"-env:UserInstallation={profile_uri}",
        "--convert-to",
        "pdf:writer_pdf_Export",
        "--outdir",
        str(out_dir),
        str(source),
    ]
    result = run(command, timeout=timeout)
    target = out_dir / f"{source.stem}.pdf"
    if not target.is_file() or target.stat().st_size == 0:
        # 某些版本输出名会保留原扩展名
        for candidate in out_dir.glob("*.pdf"):
            if candidate.stat().st_size > 0:
                target = candidate
                break
    if not target.is_file() or target.stat().st_size == 0:
        message = result.summary() or "没有生成 PDF"
        if "already running" in message.lower() or "lock" in message.lower():
            message += "（LibreOffice 实例冲突，可重试一次）"
        raise DocumentError(f"Word 转 PDF 失败：{message}")
    return target


# --------------------------------------------------------------------- 照片排版
def _load_image(path: Path):
    from PIL import Image, ImageOps

    try:
        image = Image.open(path)
        image.load()
    except Exception as exc:  # noqa: BLE001 - Pillow 异常类型多
        raise DocumentError(f"图片打不开：{exc}") from exc
    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA", "P"}:
        background = Image.new("RGB", image.size, "white")
        converted = image.convert("RGBA")
        background.paste(converted, mask=converted.split()[-1])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    return image


def compose_photo(
    image_path: Path,
    target_pdf: Path,
    media: media_mod.Media,
    dpi: int = 300,
    fit: str = "fit",
    border_mm: float = 3.0,
    auto_rotate: bool = True,
) -> Path:
    """把一张图片排到指定尺寸的页面上（1 页 1 张）。"""
    from PIL import Image

    image = _load_image(image_path)
    page_w = media_mod.mm_to_px(media.width_mm, dpi)
    page_h = media_mod.mm_to_px(media.height_mm, dpi)
    if page_w < 8 or page_h < 8:
        raise DocumentError("纸张尺寸太小，无法排版")

    if auto_rotate and image.width > image.height and page_h > page_w:
        image = image.rotate(90, expand=True)
    elif auto_rotate and image.width < image.height and page_w > page_h:
        image = image.rotate(90, expand=True)

    canvas = Image.new("RGB", (page_w, page_h), "white")
    margin = max(0, media_mod.mm_to_px(border_mm, dpi))
    avail_w = max(1, page_w - 2 * margin)
    avail_h = max(1, page_h - 2 * margin)

    if fit == "fill":
        scale = max(avail_w / image.width, avail_h / image.height)
    elif fit == "none":
        scale = 1.0
    else:
        scale = min(avail_w / image.width, avail_h / image.height)

    new_w = max(1, int(round(image.width * scale)))
    new_h = max(1, int(round(image.height * scale)))
    if (new_w, new_h) != image.size:
        image = image.resize((new_w, new_h), Image.LANCZOS)

    offset_x = (page_w - image.width) // 2
    offset_y = (page_h - image.height) // 2
    if fit == "fill":
        # 铺满：先居中裁切到可用区域，避免变形
        left = max(0, (image.width - avail_w) // 2)
        top = max(0, (image.height - avail_h) // 2)
        image = image.crop((left, top, left + min(avail_w, image.width),
                            top + min(avail_h, image.height)))
        offset_x = (page_w - image.width) // 2
        offset_y = (page_h - image.height) // 2
    canvas.paste(image, (offset_x, offset_y))

    target_pdf.parent.mkdir(parents=True, exist_ok=True)
    try:
        canvas.save(str(target_pdf), "PDF", resolution=dpi)
    except OSError as exc:
        raise DocumentError(f"生成照片 PDF 失败：{exc}") from exc
    return target_pdf


def render_pdf_pages(pdf_path: Path, out_dir: Path, cfg, dpi: int = 300) -> list[Path]:
    """PDF 每页渲染成 PNG（把 PDF 当照片打印时用）。依赖 poppler 的 pdftoppm。"""
    pdftoppm = which(str(cfg.get("conversion.pdftoppm") or "") or "pdftoppm")
    if not pdftoppm:
        raise DocumentError(
            "PDF 转图片需要 poppler-utils，请执行：sudo apt install -y poppler-utils"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "page"
    result = run(
        [pdftoppm, "-png", "-r", str(int(dpi)), str(pdf_path), str(prefix)],
        timeout=float(cfg.get("conversion.timeout", 180) or 180),
    )
    if not result.ok:
        raise DocumentError(f"PDF 转图片失败：{result.summary()}")
    pages = sorted(out_dir.glob("page-*.png"))
    if not pages:
        raise DocumentError("PDF 转图片没有输出")
    return pages


def photo_pdf_from_images(
    images: list[Path],
    target_pdf: Path,
    media: media_mod.Media,
    dpi: int,
    fit: str,
    border_mm: float,
    auto_rotate: bool,
) -> Path:
    """多张图片合成一个 PDF（每页一张）。"""
    temp_files: list[Path] = []
    work = target_pdf.parent / "photo-parts"
    work.mkdir(parents=True, exist_ok=True)
    try:
        for index, image in enumerate(images, start=1):
            part = work / f"part-{index:03d}.pdf"
            compose_photo(image, part, media, dpi, fit, border_mm, auto_rotate)
            temp_files.append(part)
        if len(temp_files) == 1:
            shutil.copyfile(temp_files[0], target_pdf)
            return target_pdf
        merge_pdfs(temp_files, target_pdf)
        return target_pdf
    finally:
        for part in temp_files:
            try:
                part.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------- 预览图
def first_page_thumbnail(pdf_path: Path, target_png: Path, cfg, width: int = 720) -> Path | None:
    """生成首页缩略图（给界面预览用）；poppler 不可用则返回 None。"""
    pdftoppm = which(str(cfg.get("conversion.pdftoppm") or "") or "pdftoppm")
    if not pdftoppm:
        return None
    target_png.parent.mkdir(parents=True, exist_ok=True)
    prefix = target_png.with_suffix("")
    result = run(
        [pdftoppm, "-png", "-f", "1", "-l", "1", "-singlefile", "-scale-to", str(width),
         str(pdf_path), str(prefix)],
        timeout=60,
    )
    if not result.ok or not target_png.is_file():
        return None
    return target_png


def describe_pdf(pdf_path: Path, cfg=None) -> dict[str, Any]:
    pages = page_count(pdf_path, cfg)
    width_pt, height_pt = pdf_page_size(pdf_path)
    return {
        "pages": pages,
        "width_mm": round(media_mod.pt_to_mm(width_pt), 1),
        "height_mm": round(media_mod.pt_to_mm(height_pt), 1),
        "bytes": pdf_path.stat().st_size if pdf_path.is_file() else 0,
    }


def make_selftest_pdf(
    target: Path,
    media_obj: media_mod.Media | None = None,
    pages: int = 1,
    dpi: int = 300,
    note: str = "",
) -> Path:
    """生成测试页 PDF（CLI self-test 与「打印机连通性测试」共用）。"""
    from PIL import Image, ImageDraw

    page_media = media_obj or media_mod.find("A4") or media_mod.MEDIA[0]
    width = media_mod.mm_to_px(page_media.width_mm, dpi)
    height = media_mod.mm_to_px(page_media.height_mm, dpi)
    total = max(1, int(pages))
    target.parent.mkdir(parents=True, exist_ok=True)

    images: list[Path] = []
    for index in range(1, total + 1):
        img = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(img)
        margin = max(20, int(width * 0.02))
        draw.rectangle([margin, margin, width - margin, height - margin], outline="black", width=4)
        lines = [
            "HP 4928 print service · self test",
            f"page {index} / {total}",
            "if you can read this, the print path works.",
        ]
        if note:
            lines.append(note[:70])
        for offset, text in enumerate(lines):
            draw.text((margin * 2, height * 0.08 + offset * height * 0.035), text, fill="black")
        steps = 8
        step_width = int((width - margin * 4) / steps)
        for step in range(steps):
            shade = int(255 * step / (steps - 1))
            left = margin * 2 + step * step_width
            draw.rectangle(
                [left, int(height * 0.28), left + step_width - 6, int(height * 0.34)],
                fill=(shade, shade, shade),
            )
        page_path = target.parent / f"{target.stem}-{index}.png"
        img.save(page_path)
        images.append(page_path)

    photo_pdf_from_images(images, target, page_media, dpi, "fit", 0, False)
    for image in images:
        try:
            image.unlink()
        except OSError:
            pass
    return target


def ensure_pdf(source: Path, work_dir: Path, cfg, kind: str, job_id: str = "job") -> Path:
    """把 PDF / Word 统一成 PDF（图片由调用方排版）。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    if kind == KIND_PDF:
        return source
    if kind == KIND_WORD:
        return convert_word(source, work_dir, cfg, profile_dir=cfg.soffice_profile_dir / job_id)
    raise DocumentError("图片请走照片排版流程")


def write_bytes_preview(image_bytes: bytes) -> io.BytesIO:
    """小工具：把字节包装成可读流（测试/缩略图用）。"""
    return io.BytesIO(image_bytes)
