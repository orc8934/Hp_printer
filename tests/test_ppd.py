"""PPD / lpoptions 解析与 CUPS 参数映射。"""

from __future__ import annotations

from hp4928 import ppd, settings as settings_mod

# 接近 HPLIP 实际输出的 HP DeskJet 4920 系列 PPD 片段（含常见选项，无 Duplex/Borderless）
PPD_FIXTURE = r'''
*PPD-Adobe: "4.3"
*FormatVersion: "4.3"
*FileVersion: "3.21.12"
*Manufacturer: "HP"
*ModelName: "HP DeskJet 4920 Series"
*NickName: "HP DeskJet 4920 Series, hpcups 3.21.12"
*ShortNickName: "HP DeskJet 4920 Series"

*OpenUI *PageSize/Media Size: PickOne
*OrderDependency: 30 AnySetup *PageSize
*DefaultPageSize: A4
*PageSize A4/A4: "<</PageSize[595.276 841.89]/ImagingBBox null>>setpagedevice"
*PageSize A5/A5: "<</PageSize[419.528 595.276]>>setpagedevice"
*PageSize A6/A6: "<</PageSize[297.638 419.528]>>setpagedevice"
*PageSize B5/B5: "<</PageSize[498.898 708.661]>>setpagedevice"
*PageSize 4x6in/4x6 in: "<</PageSize[283.465 425.197]>>setpagedevice"
*PageSize DL/DL Envelope: "<</PageSize[311.811 623.622]>>setpagedevice"
*CloseUI: *PageSize

*OpenUI *PageRegion/Page Region: PickOne
*DefaultPageRegion: A4
*PageRegion A4/A4: "<</PageSize[595.276 841.89]>>setpagedevice"
*CloseUI: *PageRegion

*OpenUI *MediaType/Media Type: PickOne
*OrderDependency: 30 AnySetup *MediaType
*DefaultMediaType: Plain
*MediaType Plain/Plain Paper: "<< /MediaType (Plain) >> setpagedevice"
*MediaType Photo/Photo Paper: "<< /MediaType (Photo) >> setpagedevice"
*MediaType Brochure/Brochure: "<< /MediaType (Brochure) >> setpagedevice"
*CloseUI: *MediaType

*OpenUI *InputSlot/Media Source: PickOne
*DefaultInputSlot: Auto
*InputSlot Auto/Auto Select: ""
*InputSlot Main/Main Tray: ""
*CloseUI: *InputSlot

*OpenUI *Resolution/Print Quality: PickOne
*OrderDependency: 60 AnySetup *Resolution
*DefaultResolution: 600dpi
*Resolution 300dpi/300 x 300 DPI: "<</HWResolution[300 300]>>setpagedevice"
*Resolution 600dpi/600 x 600 DPI: "<</HWResolution[600 600]>>setpagedevice"
*Resolution 1200dpi/1200 x 1200 DPI: "<</HWResolution[1200 1200]>>setpagedevice"
*CloseUI: *Resolution

*OpenUI *ColorMode/Color: PickOne
*DefaultColorMode: Color
*ColorMode Color/Color: ""
*ColorMode Gray/Grayscale: ""
*CloseUI: *ColorMode

*DefaultDuplex: None
*OpenUI *JobPatchFile/Job Patch File: PickOne
*JobPatchFile 1/1: ""
*CloseUI: *JobPatchFile
*DefaultcupsFilter: "application/vnd.cups-raster 0 hpcups"
'''


def test_parse_ppd_extracts_options():
    catalog = ppd.parse_ppd(PPD_FIXTURE)
    assert catalog.source == "ppd"
    assert "PageSize" in catalog.options
    assert "MediaType" in catalog.options
    assert "Resolution" in catalog.options
    assert "ColorMode" in catalog.options
    # 内部选项不应出现在界面
    assert "JobPatchFile" not in catalog.options
    assert "PageRegion" not in catalog.options
    assert catalog.description.startswith("HP DeskJet 4920 Series")
    assert catalog.get("PageSize").default == "A4"
    assert [c.name for c in catalog.get("Resolution").choices] == ["300dpi", "600dpi", "1200dpi"]
    assert catalog.get("PageSize").find("A4").label == "A4"


def test_parse_lpoptions():
    text = (
        "PageSize/Media Size: 4x6in A5 *A4 A6 B5 DL\n"
        "MediaType/Media Type: *Plain Photo Brochure\n"
        "Resolution/Print Quality: 300dpi *600dpi 1200dpi\n"
        "ColorMode/Color: Color *Gray\n"
    )
    catalog = ppd.parse_lpoptions(text)
    assert catalog.source == "lpoptions"
    assert catalog.get("PageSize").default == "A4"
    assert catalog.get("ColorMode").default == "Gray"
    assert set(catalog.get("MediaType").choice_names()) == {"Plain", "Photo", "Brochure"}


def test_roles_resolution():
    catalog = ppd.parse_ppd(PPD_FIXTURE)
    roles = ppd.resolve_roles(catalog)
    assert roles["media"] == "PageSize"
    assert roles["media_type"] == "MediaType"
    assert roles["input_slot"] == "InputSlot"
    assert roles["quality"] == "Resolution"
    assert roles["color"] == "ColorMode"
    # 该机型没有自动双面器，也没有无边距
    assert "duplex" not in roles
    assert "borderless" not in roles


def test_build_lp_options_defaults():
    catalog = ppd.parse_ppd(PPD_FIXTURE)
    settings, _ = settings_mod.normalize_settings({}, _cfg_stub(), catalog, "document")
    resolved = ppd.build_lp_options(settings, catalog)
    args = resolved.args
    assert "PageSize=A4" in args
    assert "MediaType=Plain" in args
    assert "ColorMode=Gray" in args          # 默认黑白
    assert "Resolution=600dpi" in args
    assert "sides=one-sided" in args         # 默认单面
    assert not any(arg.startswith("sides=two-sided") for arg in args)


def test_manual_duplex_never_sends_two_sided():
    catalog = ppd.parse_ppd(PPD_FIXTURE)
    settings, warnings = settings_mod.normalize_settings(
        {"duplex": "long-edge"}, _cfg_stub(), catalog, "document"
    )
    resolved = ppd.build_lp_options(settings, catalog)
    assert "sides=one-sided" in resolved.args
    assert not any("two-sided" in arg for arg in resolved.args)
    assert any("手动双面" in item for item in warnings + resolved.warnings)


def test_quality_and_color_mapping():
    catalog = ppd.parse_ppd(PPD_FIXTURE)
    cfg = _cfg_stub()
    for quality, expected in (("draft", "300dpi"), ("normal", "600dpi"),
                              ("best", "1200dpi"), ("photo", "1200dpi")):
        settings, _ = settings_mod.normalize_settings({"quality": quality}, cfg, catalog)
        args = ppd.build_lp_options(settings, catalog).args
        assert f"Resolution={expected}" in args

    settings, _ = settings_mod.normalize_settings({"color": "color"}, cfg, catalog)
    assert "ColorMode=Color" in ppd.build_lp_options(settings, catalog).args


def test_photo_media_mapping():
    catalog = ppd.parse_ppd(PPD_FIXTURE)
    settings, _ = settings_mod.normalize_settings(
        {"mode": "photo"}, _cfg_stub(), catalog, "photo"
    )
    # 照片模式自动切到 4x6in + 照片纸 + 最佳质量
    assert settings["media"] == "4x6in"
    assert settings["media_type"] == "photo"
    args = ppd.build_lp_options(settings, catalog).args
    assert "PageSize=4x6in" in args
    assert "MediaType=Photo" in args


def test_unknown_media_falls_back_to_ipp_attribute():
    catalog = ppd.parse_ppd(PPD_FIXTURE)
    settings, _ = settings_mod.normalize_settings(
        {"media": "Custom.120x180mm"}, _cfg_stub(), catalog
    )
    resolved = ppd.build_lp_options(settings, catalog)
    assert any(arg.startswith("media=") for arg in resolved.args)
    assert any("未声明" in item for item in resolved.warnings)


def test_native_override_wins():
    catalog = ppd.parse_ppd(PPD_FIXTURE)
    settings, _ = settings_mod.normalize_settings(
        {"color": "mono", "native": {"ColorMode": "Color"}}, _cfg_stub(), catalog
    )
    args = ppd.build_lp_options(settings, catalog).args
    assert "ColorMode=Color" in args
    assert "ColorMode=Gray" not in args


def test_invalid_native_value_is_dropped():
    catalog = ppd.parse_ppd(PPD_FIXTURE)
    settings, warnings = settings_mod.normalize_settings(
        {"native": {"ColorMode": "Neon"}}, _cfg_stub(), catalog
    )
    assert settings["native"] == {}
    assert any("Neon" in item for item in warnings)


def test_build_schema_shape():
    catalog = ppd.parse_ppd(PPD_FIXTURE)
    schema = ppd.build_schema(_cfg_stub(), catalog)
    group_ids = [group["id"] for group in schema["groups"]]
    assert group_ids == ["paper", "layout", "photo", "advanced"]
    assert schema["capabilities"]["manual_duplex"] is True
    assert schema["capabilities"]["auto_duplex"] is False
    assert schema["capabilities"]["borderless"] is False
    assert schema["defaults"]["media"] == "A4"
    assert schema["defaults"]["color"] == "mono"
    assert schema["defaults"]["duplex"] == "off"
    duplex_field = next(
        field for group in schema["groups"] for field in group["fields"] if field["key"] == "duplex"
    )
    assert [item["value"] for item in duplex_field["options"]] == ["off", "long-edge", "short-edge"]
    native_names = [item["name"] for item in schema["native_options"]]
    assert "PageSize" in native_names


def test_builtin_catalog_is_marked():
    catalog = ppd.builtin_catalog()
    assert catalog.source == "builtin"
    assert catalog.get("PageSize") is not None
    assert catalog.get("Borderless") is None
    assert catalog.warnings


def _cfg_stub():
    from hp4928.config import load_config

    return load_config(extra={"paths": {"data_dir": ".tmp/ppd-test"}})
