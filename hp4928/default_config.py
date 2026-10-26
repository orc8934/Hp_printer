"""默认配置（唯一事实来源）。

`config.example.yaml` 由 `python3 -m hp4928.cli config-dump` 生成，避免文档与代码漂移。
"""

from __future__ import annotations

import copy
from typing import Any

DEFAULTS: dict[str, Any] = {
    "app": {
        "name": "HP 4928 打印服务",
        # 页面标题 / 局域网设备列表里显示的名字
        "site_name": "打印服务",
    },
    "paths": {
        # 数据目录（上传、转换结果、虚拟打印输出、SQLite）。相对路径以项目根为基准。
        "data_dir": "data",
    },
    "server": {
        # 0.0.0.0 才能被局域网访问；只给本机用就改成 127.0.0.1
        "host": "0.0.0.0",
        "port": 10086,
        # waitress 工作线程数（单进程多线程，任务状态用 SQLite 串行化）
        "threads": 8,
        # 登录会话有效期（小时）
        "session_hours": 12,
        # 只有放在 Nginx 等反向代理后面时才打开
        "trust_proxy": False,
        "behind_https": False,
        # 留空=不限制；例：["192.168.1.0/24", "10.0.0.0/8"]
        "allowed_cidrs": [],
        "max_upload_mb": 64,
    },
    "printer": {
        # CUPS 队列名（lpadmin -p 后面那个名字）
        "name": "HP4928",
        # auto: 有 CUPS 队列就走 CUPS，否则降级为虚拟打印机
        # cups: 强制 CUPS（失败即报错，不降级）
        # virtual: 强制虚拟打印机（输出 PDF，适合调试/无打印机）
        "backend": "auto",
        "cups": {
            "lp": "lp",
            "lpstat": "lpstat",
            "lpoptions": "lpoptions",
            "lpinfo": "lpinfo",
            "lpadmin": "lpadmin",
            # 单条 CUPS 命令超时（秒）
            "timeout": 60,
            # 允许管理队列的系统组
            "admin_group": "lpadmin",
        },
        "usb": {
            # true: 检测不到 HP USB 设备时自动降级到虚拟打印机
            # false: 不检查 USB，直接把任务交给 CUPS
            "require_device": True,
            # HP 的 USB 厂商 ID 是 03f0
            "hp_vendor_ids": ["03f0"],
            # USB 不可用时，允许尝试同型号的 Wi-Fi/IPP 队列
            "allow_network_fallback": True,
        },
        "virtual": {
            # 虚拟打印机输出目录，留空 = <data>/spool
            "dir": "",
        },
        # 队列不存在时是否尝试 lpadmin 自动创建（需要当前用户属于 lpadmin 组）
        "auto_create_queue": False,
    },
    "print": {
        # ===== 默认值完全按需求：黑白 / A4 / 单面 =====
        "media": "A4",
        "media_type": "plain",
        "color": "mono",          # mono | color | gray
        "duplex": "off",          # off | long-edge | short-edge（后两者为手动双面）
        "quality": "normal",      # draft | normal | best | photo
        "copies": 1,
        "pages_per_sheet": 1,
        "scale": "fit",           # fit | fill | none
        "orientation": "auto",    # auto | portrait | landscape
        "page_ranges": "",
        # 手动双面第二遍：逆序送纸，翻面后页码才对得上
        "reverse_second_pass": True,
        # 手动双面第一遍打完后，多少分钟内不点「继续打印」就自动取消（0=永不）
        "manual_duplex_timeout_minutes": 60,
        "photo": {
            # 6 寸相纸 = 4×6 英寸 = 4R = 101.6×152.4mm（在官方自定义范围内）
            "media": "4x6in",
            "dpi": 300,
            # 该机型官方标注不支持无边距，故默认保留安全边距
            "border_mm": 3.0,
            "fit": "fit",         # fit 完整显示 | fill 铺满裁剪 | none 原始尺寸
            "auto_rotate": True,
        },
    },
    "conversion": {
        # 留空 = 自动探测 soffice / libreoffice
        "soffice": "",
        "pdftoppm": "",
        # Word 转 PDF 超时（秒）
        "timeout": 180,
        "image_dpi": 300,
    },
    "security": {
        # 首次登录是否强制改密（默认关闭：先用默认账号跑起来，再自行修改）
        "force_password_change": False,
        "login_max_attempts": 5,
        "login_window_seconds": 300,
        "min_password_length": 8,
        "default_admin_user": "admin",
    },
    "retention": {
        # 打印任务文件保留天数（数据库记录保留更久）
        "days": 14,
        "keep_failed_days": 30,
    },
    "logging": {
        "level": "INFO",
        # 留空 = 输出到 stdout（systemd 用 journalctl 查看）
        "file": "",
    },
}


def defaults() -> dict[str, Any]:
    """返回默认配置的深拷贝。"""
    return copy.deepcopy(DEFAULTS)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并 override 到 base（不修改入参）。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out
