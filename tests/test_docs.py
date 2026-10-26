"""PDF 页操作、页码范围、照片排版、文件类型识别。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from hp4928 import docs, media


def make_pdf(widths=(600, 600, 600)) -> bytes:
    writer = PdfWriter()
    for width in widths:
        writer.add_blank_page(width=float(width), height=842.0)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def page_widths(pdf_bytes: bytes) -> list[float]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [round(float(page.mediabox.width), 1) for page in reader.pages]


def test_sniff_kind(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(make_pdf())
    assert docs.sniff_kind(pdf) == docs.KIND_PDF

    png = tmp_path / "a.png"
    Image.new("RGB", (10, 10), "white").save(png)
    assert docs.sniff_kind(png) == docs.KIND_IMAGE

    jpeg = tmp_path / "a.jpg"
    Image.new("RGB", (10, 10), "white").save(jpeg)
    assert docs.sniff_kind(jpeg) == docs.KIND_IMAGE

    docx = tmp_path / "a.docx"
    docx.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    assert docs.sniff_kind(docx) == docs.KIND_WORD

    bad = tmp_path / "a.txt"
    bad.write_bytes(b"hello world" * 20)
    with pytest.raises(docs.DocumentError):
        docs.sniff_kind(bad)


def test_page_count(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(make_pdf((600, 600, 600, 600)))
    assert docs.page_count(pdf) == 4


def test_parse_page_ranges():
    assert docs.parse_page_ranges("", 5) == [1, 2, 3, 4, 5]
    assert docs.parse_page_ranges("1-3,5", 5) == [1, 2, 3, 5]
    assert docs.parse_page_ranges("3-", 5) == [3, 4, 5]
    assert docs.parse_page_ranges("-2", 5) == [1, 2]
    assert docs.parse_page_ranges("2,2,2", 5) == [2]
    with pytest.raises(docs.DocumentError):
        docs.parse_page_ranges("0", 5)
    with pytest.raises(docs.DocumentError):
        docs.parse_page_ranges("6", 5)
    with pytest.raises(docs.DocumentError):
        docs.parse_page_ranges("abc", 5)


def test_split_pages_for_manual_duplex():
    assert docs.split_pages("", 5) == ([1, 3, 5], [2, 4])
    assert docs.split_pages("", 4) == ([1, 3], [2, 4])
    # 页码范围按「所选范围」重新编号
    assert docs.split_pages("2-5", 9) == ([2, 4], [3, 5])
    assert docs.split_pages("3", 9) == ([3], [])


def test_extract_pages_and_reverse(tmp_path):
    source = tmp_path / "src.pdf"
    source.write_bytes(make_pdf((600, 601, 602, 603, 604)))

    forward = tmp_path / "forward.pdf"
    docs.extract_pages(source, [0, 2, 4], forward)
    assert docs.page_count(forward) == 3
    assert page_widths(forward.read_bytes()) == [600.0, 602.0, 604.0]

    backward = tmp_path / "backward.pdf"
    docs.extract_pages(source, [1, 3], backward, reverse=True)
    assert page_widths(backward.read_bytes()) == [603.0, 601.0]


def test_compose_photo_page_geometry(tmp_path):
    image_path = tmp_path / "photo.jpg"
    Image.new("RGB", (1200, 800), (200, 30, 30)).save(image_path, quality=90)
    target = tmp_path / "out.pdf"
    photo_media = media.find("4x6in")
    docs.compose_photo(image_path, target, photo_media, dpi=300, fit="fit", border_mm=3)

    reader = PdfReader(str(target))
    assert len(reader.pages) == 1
    box = reader.pages[0].mediabox
    assert round(float(box.width), 1) == 288.0
    assert round(float(box.height), 1) == 432.0
    assert reader.pages[0].images is not None


def test_compose_photo_fill_crops(tmp_path):
    image_path = tmp_path / "wide.png"
    Image.new("RGB", (2000, 500), (10, 120, 200)).save(image_path)
    target = tmp_path / "fill.pdf"
    docs.compose_photo(image_path, target, media.find("4x6in"), 150, "fill", 0)
    reader = PdfReader(str(target))
    box = reader.pages[0].mediabox
    assert round(float(box.width), 1) == 288.0


def test_photo_pdf_from_images_multi_page(tmp_path):
    paths = []
    for index in range(3):
        path = tmp_path / f"p{index}.png"
        Image.new("RGB", (900 + index * 10, 600), (index * 40, 80, 160)).save(path)
        paths.append(path)
    target = tmp_path / "multi.pdf"
    docs.photo_pdf_from_images(paths, target, media.find("10x15cm"), 300, "fit", 3, True)
    assert docs.page_count(target) == 3


def test_convert_word_without_libreoffice_is_actionable(tmp_path, app):
    source = tmp_path / "x.docx"
    source.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    cfg = app.extensions["hp4928"].cfg
    from hp4928 import docs as docs_mod

    if docs_mod.find_soffice(cfg):
        pytest.skip("本机安装了 LibreOffice，跳过缺依赖用例")
    with pytest.raises(docs.DocumentError) as excinfo:
        docs.convert_word(source, tmp_path, cfg)
    assert "LibreOffice" in str(excinfo.value)


def test_convert_word_builds_headless_command(tmp_path, monkeypatch):
    """用桩替代 soffice：验证命令拼装、输出探测与失败处理。"""
    from hp4928 import docs as docs_mod
    from hp4928 import proc

    calls = []

    def fake_run(command, timeout=30, extra_env=None):
        calls.append((command, timeout))
        outdir = Path(command[command.index("--outdir") + 1])
        source = Path(command[-1])
        target = outdir / f"{source.stem}.pdf"
        target.write_bytes(make_pdf())
        return proc.RunResult(" ".join(command), 0, "", "")

    monkeypatch.setattr(docs_mod, "run", fake_run)
    monkeypatch.setattr(docs_mod, "find_soffice", lambda cfg: "/usr/bin/soffice")

    source = tmp_path / "报告.docx"
    source.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    cfg = _cfg_stub()
    result = docs_mod.convert_word(source, tmp_path / "out", cfg, profile_dir=tmp_path / "prof")
    assert result.is_file()
    assert calls, "应当调用 soffice"

    command, timeout = calls[0]
    assert command[0] == "/usr/bin/soffice"
    assert "--headless" in command
    assert "-env:UserInstallation=file:///" in " ".join(command)
    assert "pdf:writer_pdf_Export" in command
    assert timeout >= 60


def test_convert_word_failure_raises(tmp_path, monkeypatch):
    from hp4928 import docs as docs_mod
    from hp4928 import proc

    monkeypatch.setattr(
        docs_mod, "run", lambda command, timeout=30, extra_env=None:
        proc.RunResult(" ".join(command), 1, "", "Error: source file could not be loaded")
    )
    monkeypatch.setattr(docs_mod, "find_soffice", lambda cfg: "/usr/bin/soffice")
    source = tmp_path / "bad.docx"
    source.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    with pytest.raises(docs.DocumentError) as excinfo:
        docs_mod.convert_word(source, tmp_path / "out", _cfg_stub())
    assert "Word 转 PDF 失败" in str(excinfo.value)


def test_soffice_profile_isolated_per_job(tmp_path, monkeypatch):
    from hp4928 import docs as docs_mod
    from hp4928 import proc

    seen = []

    def fake_run(command, timeout=30, extra_env=None):
        seen.append([part for part in command if part.startswith("-env:UserInstallation")][0])
        outdir = Path(command[command.index("--outdir") + 1])
        (outdir / "a.pdf").write_bytes(make_pdf())
        return proc.RunResult("ok", 0, "", "")

    monkeypatch.setattr(docs_mod, "run", fake_run)
    monkeypatch.setattr(docs_mod, "find_soffice", lambda cfg: "/usr/bin/soffice")
    source = tmp_path / "a.docx"
    source.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    cfg = _cfg_stub()
    docs_mod.convert_word(source, tmp_path / "o1", cfg, profile_dir=cfg.soffice_profile_dir / "J1")
    docs_mod.convert_word(source, tmp_path / "o2", cfg, profile_dir=cfg.soffice_profile_dir / "J2")
    assert seen[0] != seen[1], "每个任务必须用独立 profile，否则并发转换会互锁"


def _cfg_stub():
    from hp4928.config import load_config

    return load_config(extra={"paths": {"data_dir": ".tmp/docs-test"},
                              "conversion": {"soffice": ""}})
