#!/usr/bin/env python3
"""HP 4928 打印服务控制台 · NiceGUI 单文件设计原型

运行：
    python -m pip install nicegui
    python app.py                      # http://127.0.0.1:8080
    python app.py --port 9000 --host 0.0.0.0 --show

设计要点：
- 左侧固定侧边栏：Logo 占位区 + 按身份动态渲染的菜单 + 底部身份切换
- HP 配色：主蓝 #0096D6 / 深蓝 #002E6D；侧边栏白底 #FFFFFF、正文深灰 #333333
- 选中态：左侧 3px HP 主蓝竖条 + 浅蓝底 + 深蓝加粗
- 页面：工作台 / 个人中心 / 消息通知（所有人）；系统管理 / 用户权限（仅管理员）
- 系统管理含「打印机与连接」：USB/网络队列、切换、扫描局域网打印机、
  手动添加 ipp:// 队列、打测试页 —— 即之前那版服务的 Wi-Fi 功能

注意：这是纯前端原型，数据在内存里，不会真的调用 CUPS。接真实后端见文件末尾注释。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from nicegui import app, ui

# --------------------------------------------------------------------------- #
#  常量
# --------------------------------------------------------------------------- #
HP_BLUE = "#0096D6"
HP_BLUE_DARK = "#0083BD"
HP_NAVY = "#002E6D"
HP_INK = "#333333"
HP_MUTED = "#6B7A90"
HP_LINE = "#E6EBF2"
HP_BG = "#F5F7FA"

QUEUE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
NETWORK_PREFIXES = ("ipp://", "ipps://", "dnssd://", "socket://", "lpd://", "hp:/net/")


# --------------------------------------------------------------------------- #
#  内联 SVG 图标（不依赖图标字体，离线也能显示）
# --------------------------------------------------------------------------- #
_ICONS: dict[str, str] = {
    "dashboard": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" '
                 'height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>'
                 '<rect x="3" y="14" width="7" height="7" rx="1.5"/>',
    "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "bell": '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>'
            '<path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
    "settings": '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/>'
                '<line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/>'
                '<line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/>'
                '<line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/>'
                '<line x1="17" y1="16" x2="23" y2="16"/>',
    "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
             '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "printer": '<polyline points="6 9 6 2 18 2 18 9"/>'
               '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>'
               '<rect x="6" y="14" width="12" height="8"/>',
    "wifi": '<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/>'
            '<path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/>',
    "usb": '<circle cx="12" cy="19" r="2"/><path d="M12 17V5"/><path d="M9 8l3-3 3 3"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "refresh": '<polyline points="23 4 23 10 17 10"/>'
               '<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/>'
             '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
             '<path d="M10 11v6M14 11v6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "check": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>'
             '<polyline points="22 4 12 14.01 9 11.01"/>',
    "alert": '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>'
             '<line x1="12" y1="16" x2="12.01" y2="16"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/>'
            '<path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "play": '<polygon points="5 3 19 12 5 21 5 3"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
              '<polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    "server": '<rect x="2" y="3" width="20" height="7" rx="2"/>'
              '<rect x="2" y="14" width="20" height="7" rx="2"/>'
              '<line x1="6" y1="6.5" x2="6.01" y2="6.5"/><line x1="6" y1="17.5" x2="6.01" y2="17.5"/>',
}


def icon_svg(name: str, size: int = 18) -> str:
    body = _ICONS.get(name, _ICONS["alert"])
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="1.9" stroke-linecap="round" '
        f'stroke-linejoin="round">{body}</svg>'
    )


# --------------------------------------------------------------------------- #
#  内存数据
# --------------------------------------------------------------------------- #
@dataclass
class Queue:
    name: str
    uri: str
    enabled: bool = True
    accepting: bool = True

    @property
    def is_network(self) -> bool:
        return self.uri.lower().startswith(NETWORK_PREFIXES)

    @property
    def kind_label(self) -> str:
        return "网络" if self.is_network else "USB"

    @property
    def kind_icon(self) -> str:
        return "wifi" if self.is_network else "usb"


@dataclass
class PrintJob:
    job_id: str
    title: str
    state: str
    settings: str
    pages: int
    created: datetime


@dataclass
class Notification:
    title: str
    body: str
    kind: str
    created: datetime
    unread: bool = True


@dataclass
class User:
    username: str
    display: str
    role: str
    active: bool = True
    last_login: str = ""


@dataclass
class Discovered:
    uri: str
    name: str
    source: str


JOB_STATE = {
    "done": ("已完成", "ok"),
    "awaiting_flip": ("等待翻面", "warn"),
    "printing": ("打印中", "info"),
    "failed": ("失败", "danger"),
    "canceled": ("已取消", "neutral"),
    "queued": ("排队中", "neutral"),
}


def _ago(minutes: int) -> datetime:
    return datetime.now() - timedelta(minutes=minutes)


@dataclass
class Store:
    queues: list[Queue] = field(default_factory=lambda: [
        Queue("HP4928", "usb://HP/DeskJet%204900%20series?serial=CN594K6KG8"),
        Queue("HP4928-WIFI", "ipp://192.168.1.50/ipp/print"),
    ])
    selected: str = "HP4928"
    discovered: list[Discovered] = field(default_factory=list)
    jobs: list[PrintJob] = field(default_factory=lambda: [
        PrintJob("J20261026-A31F", "季度报表.pdf", "awaiting_flip",
                 "A4 · 黑白 · 手动双面长边 · 6 页", 6, _ago(3)),
        PrintJob("J20261026-9C02", "合同扫描件.pdf", "done",
                 "A4 · 黑白 · 单面 · 3 页", 3, _ago(26)),
        PrintJob("J20261026-77B1", "产品照片.jpg", "done",
                 "6 寸 4x6in · 彩色 · 最佳照片", 1, _ago(74)),
        PrintJob("J20261025-5E8A", "会议纪要.docx", "failed",
                 "A4 · 黑白 · 单面 · Word 转换超时", 0, _ago(300)),
        PrintJob("J20261025-1B44", "报销单.pdf", "done",
                 "A4 · 黑白 · 单面 · 2 页 × 2 份", 2, _ago(420)),
    ])
    notifications: list[Notification] = field(default_factory=lambda: [
        Notification("需要翻面确认",
                     "「季度报表.pdf」第一面（奇数页）已打印，请在出纸口翻面后点「继续打印」。",
                     "warn", _ago(3)),
        Notification("打印完成", "「合同扫描件.pdf」3 页已全部送出。", "ok", _ago(26)),
        Notification("打印机提醒", "USB 队列 HP4928 曾短暂离线 12 秒，已自动恢复。",
                     "info", _ago(95), False),
        Notification("任务失败", "「会议纪要.docx」转换超时（180 秒），请重试或拆分文档。",
                     "danger", _ago(300), False),
    ])
    users: list[User] = field(default_factory=lambda: [
        User("admin", "系统管理员", "admin", True, "今天 09:12"),
        User("zhangsan", "张三", "user", True, "今天 08:40"),
        User("lisi", "李四", "user", False, "3 天前"),
    ])

    def active_queue(self) -> Queue:
        for queue in self.queues:
            if queue.name == self.selected:
                return queue
        return self.queues[0]

    def set_active(self, name: str) -> None:
        if any(queue.name == name for queue in self.queues):
            self.selected = name

    def add_queue(self, name: str, uri: str, model: str = "") -> tuple[bool, str]:
        if not QUEUE_NAME_RE.match(name or ""):
            return False, "队列名只能用字母、数字、下划线、点、短横线（1-60 位）"
        if not (uri or "").lower().startswith(NETWORK_PREFIXES):
            return False, "地址要以 ipp:// ipps:// dnssd:// socket:// 开头"
        if any(queue.name == name for queue in self.queues):
            return False, f"队列 {name} 已存在"
        self.queues.append(Queue(name, uri))
        self.selected = name
        return True, f"已添加 {name}（driver: {model or 'everywhere'}）并设为默认"

    def delete_queue(self, name: str) -> tuple[bool, str]:
        if name == self.selected:
            return False, "这是当前使用的队列，请先切换到别的队列"
        self.queues = [queue for queue in self.queues if queue.name != name]
        return True, f"已删除队列 {name}"

    def unread_count(self) -> int:
        return sum(1 for item in self.notifications if item.unread)

    def mark_all_read(self) -> None:
        for item in self.notifications:
            item.unread = False


STORE = Store()


# --------------------------------------------------------------------------- #
#  样式
# --------------------------------------------------------------------------- #
CSS = f"""
:root {{
  --hp-blue: {HP_BLUE}; --hp-blue-dark: {HP_BLUE_DARK}; --hp-navy: {HP_NAVY};
  --hp-ink: {HP_INK}; --hp-muted: {HP_MUTED}; --hp-line: {HP_LINE};
  --hp-bg: {HP_BG}; --hp-soft: #F8F9FA;
}}
html, body {{
  background: var(--hp-bg) !important; color: var(--hp-ink) !important;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif !important;
  font-size: 14px; margin: 0;
}}
.q-page, .nicegui-content {{ padding: 0 !important; gap: 0 !important; max-width: none !important; }}
.q-page {{ min-height: 100vh; }}
/* NiceGUI 的 .nicegui-content 是 align-items:flex-start 的列容器，子元素不会自动撑满，这里显式给宽度 */
.hp-shell {{ min-height: 100vh; width: 100%; background: var(--hp-bg); }}

/* ---------- 侧边栏 ---------- */
.hp-sidebar {{
  position: fixed; top: 0; left: 0; bottom: 0; width: 264px; z-index: 30;
  background: #FFFFFF; border-right: 1px solid var(--hp-line);
  display: flex; flex-direction: column; gap: 4px;
  padding: 20px 14px 16px; overflow-y: auto;
}}
.hp-logo {{ display: flex; align-items: center; gap: 10px; padding: 2px 6px 16px; }}
.hp-logo-mark {{
  width: 40px; height: 40px; border-radius: 12px; flex: none;
  background: linear-gradient(135deg, {HP_BLUE} 0%, {HP_NAVY} 100%);
  color: #fff !important; display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 15px; letter-spacing: -0.5px;
  box-shadow: 0 6px 14px rgba(0, 46, 109, 0.22);
}}
.hp-logo-text {{ display: flex; flex-direction: column; line-height: 1.25; }}
.hp-logo-title {{ font-weight: 700; font-size: 14.5px; color: var(--hp-navy) !important; }}
.hp-logo-sub {{ font-size: 11.5px; color: var(--hp-muted) !important; }}

.hp-nav {{ display: flex; flex-direction: column; gap: 3px; }}
.hp-nav-label {{
  font-size: 11px; letter-spacing: 0.08em; color: #94A3B8 !important;
  padding: 14px 12px 6px; text-transform: uppercase; font-weight: 600;
}}
.hp-nav-item {{
  display: flex; align-items: center; gap: 10px; padding: 10px 12px;
  border-radius: 10px; border-left: 3px solid transparent; color: var(--hp-ink);
  cursor: pointer; transition: background .15s, color .15s; font-size: 14px;
  user-select: none;
}}
.hp-nav-item:hover {{ background: var(--hp-soft); }}
.hp-nav-icon {{ display: flex; align-items: center; justify-content: center;
                width: 20px; height: 20px; color: #5A6B80; flex: none; }}
.hp-nav-item.is-active {{ background: #E6F6FD; border-left-color: var(--hp-blue);
                          color: var(--hp-navy); font-weight: 650; }}
.hp-nav-item.is-active .hp-nav-icon {{ color: var(--hp-blue); }}
.hp-nav-badge {{
  background: {HP_BLUE}; color: #fff !important; border-radius: 999px;
  font-size: 11px; font-weight: 700; padding: 0 7px; line-height: 18px;
  min-width: 18px; text-align: center;
}}

.hp-rolebox {{ margin-top: auto; padding-top: 14px; border-top: 1px solid var(--hp-line);
               display: flex; flex-direction: column; gap: 10px; }}
.hp-userchip {{ display: flex; align-items: center; gap: 10px; }}
.hp-avatar {{
  width: 34px; height: 34px; border-radius: 50%; flex: none;
  background: #E6F6FD; color: var(--hp-navy) !important; font-weight: 700; font-size: 13px;
  display: flex; align-items: center; justify-content: center;
}}
.hp-userchip-name {{ font-size: 13px; font-weight: 600; color: var(--hp-navy) !important; }}
.hp-userchip-role {{ font-size: 11.5px; color: var(--hp-muted) !important; }}

/* ---------- 主区域 ---------- */
.hp-main {{ margin-left: 264px; min-height: 100vh; display: flex; flex-direction: column; }}
.hp-topbar {{
  position: sticky; top: 0; z-index: 20; height: 68px; flex: none;
  background: rgba(255, 255, 255, 0.94); backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--hp-line); display: flex; align-items: center;
  justify-content: space-between; padding: 0 26px;
}}
.hp-topbar-title {{ display: flex; flex-direction: column; gap: 1px; }}
.hp-crumb {{ font-size: 11.5px; color: var(--hp-muted) !important; }}
.hp-title {{ font-size: 18px; font-weight: 700; color: var(--hp-navy) !important; line-height: 1.3; }}
.hp-content {{ padding: 22px 26px 44px; width: 100%; max-width: 1240px; }}

/* ---------- 卡片 ---------- */
.hp-card {{
  background: #FFFFFF; border: 1px solid var(--hp-line); border-radius: 14px;
  padding: 18px 20px; box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}}
.hp-card-head {{ display: flex; align-items: center; justify-content: space-between;
                 gap: 10px; margin-bottom: 12px; }}
.hp-card-title {{ font-size: 15px; font-weight: 650; color: #0F1B2D !important;
                  display: flex; align-items: center; gap: 8px; }}
.hp-icon {{ color: var(--hp-blue); display: flex; align-items: center; }}
.hp-muted {{ color: var(--hp-muted) !important; }}
.hp-grid {{ display: grid; gap: 16px; }}
.hp-grid-2 {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.hp-grid-3 {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
.hp-grid-4 {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
.hp-stack {{ display: flex; flex-direction: column; gap: 16px; }}

/* ---------- Hero ---------- */
.hp-hero {{
  background: linear-gradient(120deg, #EAF8FE 0%, #F7FBFE 55%, #FFFFFF 100%);
  border: 1px solid #CDEBF8; border-left: 4px solid var(--hp-blue);
  border-radius: 14px; padding: 18px 20px;
}}
.hp-hero-title {{ font-size: 16.5px; font-weight: 700; color: var(--hp-navy) !important; }}
.hp-hero-sub {{ font-size: 12.5px; color: #4B6B85 !important; }}

/* ---------- 统计 ---------- */
.hp-stat {{ display: flex; flex-direction: column; gap: 2px; }}
.hp-stat-value {{ font-size: 26px; font-weight: 700; color: var(--hp-navy) !important;
                  line-height: 1.15; }}
.hp-stat-label {{ font-size: 12.5px; color: var(--hp-muted) !important; }}
.hp-stat-hint {{ font-size: 11.5px; color: #94A3B8 !important; }}

/* ---------- 徽标 ---------- */
.hp-badge {{
  display: inline-flex !important; align-items: center; gap: 5px; padding: 0 9px;
  border-radius: 999px; font-size: 12px; font-weight: 600; line-height: 20px;
  white-space: nowrap;
}}
.hp-badge.ok {{ background: #E7F8F0; color: #0B7A56 !important; }}
.hp-badge.info {{ background: #E6F6FD; color: #005F86 !important; }}
.hp-badge.warn {{ background: #FFF4E5; color: #A65B00 !important; }}
.hp-badge.danger {{ background: #FDECEC; color: #B42318 !important; }}
.hp-badge.neutral {{ background: #EEF2F6; color: #475467 !important; }}

/* ---------- 按钮 ---------- */
.hp-btn {{
  border-radius: 10px !important; font-weight: 600 !important; font-size: 13px !important;
  text-transform: none !important; letter-spacing: 0.2px; min-height: 36px !important;
  padding: 0 14px !important; box-shadow: none !important;
  flex: none !important; white-space: nowrap !important;
}}
.hp-btn-sm {{ min-height: 30px !important; font-size: 12.5px !important;
              padding: 0 10px !important; }}
.hp-btn-primary {{ background: var(--hp-blue) !important; color: #fff !important; }}
.hp-btn-primary:hover {{ background: var(--hp-blue-dark) !important; }}
.hp-btn-ghost {{ background: #fff !important; color: var(--hp-ink) !important;
                 border: 1px solid var(--hp-line) !important; }}
.hp-btn-ghost:hover {{ background: var(--hp-soft) !important; }}
.hp-btn-navy {{ background: var(--hp-navy) !important; color: #fff !important; }}
.hp-btn-danger {{ background: #fff !important; color: #B42318 !important;
                  border: 1px solid #F3C6C2 !important; }}
.hp-btn-danger:hover {{ background: #FDECEC !important; }}

/* ---------- 表格 ---------- */
.hp-table {{ display: flex; flex-direction: column; width: 100%; }}
.hp-tr {{ display: grid; gap: 10px; align-items: center; padding: 10px 2px;
          border-bottom: 1px solid var(--hp-line); font-size: 13.5px; }}
.hp-tr:last-child {{ border-bottom: none; }}
.hp-tr:hover {{ background: #FBFDFF; }}
.hp-th {{ font-size: 12px; font-weight: 600; color: var(--hp-muted) !important; padding: 6px 2px; }}
.hp-cell-strong {{ font-weight: 600; color: #16233A !important; }}
.hp-mono {{ font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
            font-size: 11.5px; color: var(--hp-muted) !important; word-break: break-all; }}

/* ---------- 表单 ---------- */
.hp-form {{ display: grid; gap: 12px; }}
.hp-form-2 {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.hp-form-field {{ display: flex; flex-direction: column; gap: 5px; }}
.q-field--outlined .q-field__control {{ border-radius: 10px !important; }}
.q-field__native, .q-field__input {{ font-size: 13.5px !important; }}
.q-field--outlined .q-field__control:before {{ border-color: var(--hp-line) !important; }}
.q-field--outlined.q-field--focused .q-field__control:after {{
  border-color: var(--hp-blue) !important; border-width: 1px !important;
}}

.hp-hint {{ background: #F6FAFD; border: 1px solid #DCEBF5; border-radius: 10px;
            padding: 10px 12px; font-size: 12.5px; color: #3F5A70 !important; line-height: 1.65; }}

/* ---------- 发现结果 ---------- */
.hp-found {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
             padding: 10px 12px; border: 1px solid var(--hp-line);
             border-radius: 10px; background: var(--hp-soft); }}

/* ---------- 消息 ---------- */
.hp-msg {{ display: flex; gap: 12px; padding: 12px 4px; align-items: flex-start;
           border-bottom: 1px solid var(--hp-line); cursor: pointer; }}
.hp-msg:last-child {{ border-bottom: none; }}
.hp-msg:hover {{ background: #FBFDFF; }}
.hp-msg-dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--hp-blue);
               margin-top: 7px; flex: none; }}
.hp-msg-dot.read {{ background: transparent; }}
.hp-msg-title {{ font-weight: 600; font-size: 13.5px; color: #16233A !important; }}
.hp-msg-body {{ font-size: 12.5px; color: var(--hp-muted) !important; }}
.hp-msg-time {{ font-size: 11.5px; color: #94A3B8 !important; white-space: nowrap; }}
.hp-pill {{ padding: 4px 12px; border-radius: 999px; font-size: 12.5px; cursor: pointer;
            border: 1px solid var(--hp-line); color: var(--hp-muted) !important;
            background: #fff; }}
.hp-pill.is-active {{ background: #E6F6FD; border-color: #BFE7F7;
                      color: #005F86 !important; font-weight: 600; }}

/* ---------- 设置项 ---------- */
.hp-kv {{ display: grid; grid-template-columns: 110px 1fr; gap: 8px 12px; font-size: 13px; }}
.hp-kv .k {{ color: var(--hp-muted) !important; }}
.hp-kv .v {{ color: #16233A !important; }}
.hp-chips {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.hp-chip {{ background: #F4F8FB; border: 1px solid #DCEBF5; border-radius: 999px;
            padding: 3px 11px; font-size: 12px; color: #34566E !important; }}

/* ---------- 弹窗 ---------- */
.hp-dialog {{ border-radius: 16px !important; width: 520px; max-width: 92vw;
              padding: 22px !important; }}
.hp-dialog-title {{ font-size: 16px; font-weight: 700; color: var(--hp-navy) !important; }}

/* 提示条下移，别盖住顶栏 */
.q-notifications__list--top {{ top: 76px !important; }}

/* ---------- 响应式 ---------- */
@media (max-width: 1180px) {{ .hp-grid-4 {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
@media (max-width: 1024px) {{
  .hp-sidebar {{ width: 216px; }}
  .hp-main {{ margin-left: 216px; }}
  .hp-grid-3 {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
}}
@media (max-width: 820px) {{
  .hp-sidebar {{ position: static; width: 100%; height: auto; flex-direction: row;
                 align-items: center; gap: 4px; overflow-x: auto; border-right: none;
                 border-bottom: 1px solid var(--hp-line); padding: 8px 10px; }}
  .hp-main {{ margin-left: 0; }}
  .hp-logo {{ padding: 0 4px 0 0; }}
  .hp-logo-text, .hp-nav-text, .hp-userchip, .hp-nav-label {{ display: none !important; }}
  .hp-nav {{ flex-direction: row; gap: 3px; }}
  .hp-nav-item {{ padding: 8px 7px; }}
  .hp-logo-mark {{ width: 30px; height: 30px; font-size: 12px; border-radius: 9px; }}
  .hp-rolebox {{ margin: 0 0 0 auto; border: none; padding: 0; flex-direction: row;
                 align-items: center; gap: 4px; flex: none; }}
  .hp-rolebox .q-field {{ width: 92px !important; min-height: 34px; }}
  .hp-rolebox .q-field__label {{ display: none !important; }}
  .hp-rolebox .q-field__control {{ min-height: 34px !important; padding: 0 4px !important; }}
  .hp-rolebox .q-field__native {{ min-height: 34px; padding: 0; font-size: 12.5px; }}
  .hp-rolebox > .hp-muted {{ display: none !important; }}
  .hp-role-badge {{ display: none !important; }}
  .hp-grid-2, .hp-grid-3, .hp-grid-4 {{ grid-template-columns: minmax(0, 1fr); }}
  .hp-topbar {{ padding: 0 12px; height: 60px; gap: 8px; overflow: hidden; }}
  .hp-topbar-title {{ min-width: 0; }}
  .hp-crumb {{ display: none !important; }}
  .hp-title {{ font-size: 16px; white-space: nowrap; overflow: hidden;
               text-overflow: ellipsis; }}
  .hp-content {{ padding: 16px 14px 32px; overflow-x: hidden; }}
  .hp-form-2 {{ grid-template-columns: minmax(0, 1fr); }}
  .hp-table {{ overflow-x: auto; }}
}}
"""


# --------------------------------------------------------------------------- #
#  导航元数据
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    icon: str
    admin_only: bool = False


NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem("dashboard", "工作台", "dashboard"),
    NavItem("profile", "个人中心", "user"),
    NavItem("messages", "消息通知", "bell"),
    NavItem("system", "系统管理", "settings", admin_only=True),
    NavItem("users", "用户权限", "users", admin_only=True),
)

PAGE_TITLES = {item.key: item.label for item in NAV_ITEMS}


def role_label(role: str) -> str:
    return "管理员" if role == "admin" else "普通用户"


def can_see(item: NavItem, role: str) -> bool:
    return (not item.admin_only) or role == "admin"


# --------------------------------------------------------------------------- #
#  小组件
# --------------------------------------------------------------------------- #
def badge(text: str, kind: str = "neutral") -> ui.label:
    return ui.label(text).classes(f"hp-badge {kind}")


def icon(name: str, size: int = 18) -> ui.html:
    return ui.html(icon_svg(name, size), sanitize=False).classes("hp-icon")


def card(title: str, icon_name: str = "", subtitle: str = "") -> ui.element:
    box = ui.element("section").classes("hp-card")
    if title:
        with box:
            with ui.element("div").classes("hp-card-head"):
                with ui.element("div").classes("hp-card-title"):
                    if icon_name:
                        icon(icon_name, 19)
                    ui.label(title)
                if subtitle:
                    ui.label(subtitle).classes("hp-muted").style("font-size:12px")
    return box


def kv(key: str, value: str) -> None:
    ui.label(key).classes("k")
    ui.label(value).classes("v")


def pill(text: str, active: bool, on_click: Callable) -> ui.label:
    el = ui.label(text).classes("hp-pill" + (" is-active" if active else ""))
    el.on("click", on_click)
    return el


def say(message: str, type: str = "info", timeout: int = 4200, **kwargs) -> None:
    """统一提示：右上角弹出，避免挡住页面内容。"""
    ui.notify(message, type=type, position="top-right", timeout=timeout, **kwargs)


def action_button(text: str, handler: Callable, style: str = "ghost", small: bool = True) -> ui.button:
    # color=None：不继承 Quasar 的 bg-primary/text-white（它们带 !important，会盖掉我们的 HP 配色）
    btn = ui.button(text, on_click=handler, color=None)
    classes = "hp-btn hp-btn-sm" if small else "hp-btn"
    return btn.classes(f"{classes} hp-btn-{style}")


# --------------------------------------------------------------------------- #
#  工作台
# --------------------------------------------------------------------------- #
def page_dashboard(role: str, refresh: Callable) -> None:
    active = STORE.active_queue()
    awaiting = sum(1 for job in STORE.jobs if job.state == "awaiting_flip")
    failed = sum(1 for job in STORE.jobs if job.state == "failed")
    done = sum(1 for job in STORE.jobs if job.state == "done")

    with ui.element("section").classes("hp-hero"):
        with ui.element("div").style("display:flex;justify-content:space-between;gap:16px;"
                                     "align-items:center;flex-wrap:wrap"):
            with ui.element("div"):
                ui.label("打印服务运行中").classes("hp-hero-title")
                ui.label(f"当前队列 {active.name} · {active.kind_label} 连接 · {active.uri}") \
                    .classes("hp-hero-sub")
            with ui.element("div").style("display:flex;gap:8px;align-items:center"):
                badge("在线", "ok")
                action_button("打测试页", lambda: test_print(active.name))

    with ui.element("div").classes("hp-grid hp-grid-4"):
        for value, label, hint in (
            (str(done), "今日完成", "最近 24 小时"),
            (str(awaiting), "等待翻面", "需点「继续打印」"),
            (str(failed), "失败任务", "可重试"),
            ("2", "在线连接", "USB + 网络"),
        ):
            with ui.element("div").classes("hp-card"):
                with ui.element("div").classes("hp-stat"):
                    ui.label(value).classes("hp-stat-value")
                    ui.label(label).classes("hp-stat-label")
                    ui.label(hint).classes("hp-stat-hint")

    with ui.element("div").classes("hp-grid hp-grid-2"):
        with card("最近任务", "printer", "演示数据"):
            with ui.element("div").classes("hp-table"):
                with ui.element("div").classes("hp-tr hp-th") \
                        .style("grid-template-columns:1.5fr .8fr 1.2fr .5fr"):
                    ui.label("任务")
                    ui.label("状态")
                    ui.label("设置")
                    ui.label("时间")
                for job in STORE.jobs[:4]:
                    label, kind = JOB_STATE.get(job.state, (job.state, "neutral"))
                    with ui.element("div").classes("hp-tr") \
                            .style("grid-template-columns:1.5fr .8fr 1.2fr .5fr"):
                        with ui.element("div"):
                            ui.label(job.title).classes("hp-cell-strong")
                            ui.label(job.job_id).classes("hp-mono")
                        with ui.element("div"):
                            badge(label, kind)
                        ui.label(job.settings).classes("hp-muted").style("font-size:12px")
                        ui.label(job.created.strftime("%H:%M")).classes("hp-muted") \
                            .style("font-size:12px")

        with ui.element("div").classes("hp-stack"):
            with card("默认打印设置", "settings", "与驱动能力一致"):
                with ui.element("div").classes("hp-chips"):
                    for text in ("A4 纸张", "黑白 / 灰度", "单面（默认）", "600 dpi",
                                 "手动双面可选", "6 寸相纸 4x6in"):
                        ui.label(text).classes("hp-chip")
                ui.label("手动双面会先打奇数页并暂停，翻面后点「继续打印」才输出第二面。") \
                    .classes("hp-hint").style("margin-top:12px")

            with card("快捷操作", "play"):
                with ui.element("div").style("display:flex;gap:8px;flex-wrap:wrap"):
                    action_button("上传文件打印",
                                  lambda: say("原型演示：真实服务里这里是上传 PDF / Word / 图片",
                                                    type="info"), "primary")
                    action_button("查看全部任务",
                                  lambda: go_hint("任务历史"))
                    action_button("6 寸照片",
                                  lambda: say("原型演示：4x6in 相纸 + 3mm 安全边距",
                                                    type="info"))


def go_hint(where: str) -> None:
    say(f"原型演示：这里会跳到「{where}」", type="info")


# --------------------------------------------------------------------------- #
#  个人中心
# --------------------------------------------------------------------------- #
def page_profile(role: str, refresh: Callable) -> None:
    me = "admin" if role == "admin" else "zhangsan"
    display = "系统管理员" if role == "admin" else "张三"

    with ui.element("div").classes("hp-grid hp-grid-2"):
        with card("账号信息", "user"):
            with ui.element("div").style("display:flex;gap:14px;align-items:center;"
                                         "margin-bottom:14px"):
                with ui.element("div").classes("hp-avatar") \
                        .style("width:52px;height:52px;font-size:18px"):
                    ui.label(display[:1])
                with ui.element("div"):
                    ui.label(display).style("font-size:16px;font-weight:700;color:#16233A")
                    ui.label(f"@{me} · {role_label(role)}").classes("hp-muted") \
                        .style("font-size:12.5px")
            with ui.element("div").classes("hp-kv"):
                kv("用户名", me)
                kv("角色", role_label(role))
                kv("最近登录", "今天 09:12 · 192.168.5.31")
                kv("会话有效期", "12 小时（可配置）")
            ui.label("账号由管理员创建；忘记密码可让管理员重置。") \
                .classes("hp-hint").style("margin-top:14px")

        with card("修改密码", "lock"):
            with ui.element("div").classes("hp-form"):
                current = ui.input("当前密码", password=True, password_toggle_button=True) \
                    .props("outlined dense").classes("w-full")
                new = ui.input("新密码（至少 8 位）", password=True,
                               password_toggle_button=True) \
                    .props("outlined dense").classes("w-full")
                confirm = ui.input("再输一次", password=True, password_toggle_button=True) \
                    .props("outlined dense").classes("w-full")

                def submit() -> None:
                    if not current.value:
                        say("请先输入当前密码", type="warning")
                    elif len(new.value or "") < 8:
                        say("新密码至少 8 位", type="warning")
                    elif new.value != confirm.value:
                        say("两次输入的新密码不一致", type="negative")
                    else:
                        say("原型演示：真实服务会校验旧密码并更新 scrypt 哈希",
                                  type="positive")
                        current.value = new.value = confirm.value = ""

                action_button("保存新密码", submit, "primary", small=False)

    with card("登录记录", "clock", "最近 4 条"):
        with ui.element("div").classes("hp-table"):
            with ui.element("div").classes("hp-tr hp-th") \
                    .style("grid-template-columns:1.2fr 1fr 1fr"):
                ui.label("时间")
                ui.label("来源 IP")
                ui.label("结果")
            for when, ip, result, kind in (
                ("今天 09:12", "192.168.5.31", "成功", "ok"),
                ("昨天 18:03", "192.168.5.31", "成功", "ok"),
                ("昨天 09:41", "10.8.0.12", "密码错误", "danger"),
                ("10-24 16:20", "192.168.5.88", "成功", "ok"),
            ):
                with ui.element("div").classes("hp-tr") \
                        .style("grid-template-columns:1.2fr 1fr 1fr"):
                    ui.label(when)
                    ui.label(ip).classes("hp-mono")
                    with ui.element("div"):
                        badge(result, kind)


# --------------------------------------------------------------------------- #
#  消息通知
# --------------------------------------------------------------------------- #
def page_messages(role: str, refresh: Callable, filter_state: dict) -> None:
    with card("消息通知", "bell", f"{STORE.unread_count()} 条未读"):
        with ui.element("div").style("display:flex;gap:8px;align-items:center;"
                                     "margin-bottom:6px;flex-wrap:wrap"):
            def set_filter(value: str) -> None:
                filter_state["filter"] = value
                refresh()

            pill("全部", filter_state["filter"] == "all", lambda: set_filter("all"))
            pill("未读", filter_state["filter"] == "unread", lambda: set_filter("unread"))
            ui.element("div").style("flex:1")

            def read_all() -> None:
                STORE.mark_all_read()
                say("已全部标记为已读", type="positive")
                refresh()

            action_button("全部标记已读", read_all)

        items = STORE.notifications
        if filter_state["filter"] == "unread":
            items = [item for item in items if item.unread]

        if not items:
            ui.label("没有未读消息").classes("hp-muted").style("padding:18px 4px")

        for item in items:
            msg = ui.element("div").classes("hp-msg")
            with msg:
                ui.element("div").classes("hp-msg-dot" + ("" if item.unread else " read"))
                with ui.element("div").style("flex:1"):
                    with ui.element("div").style("display:flex;gap:8px;align-items:center"):
                        ui.label(item.title).classes("hp-msg-title")
                        badge({"info": "系统", "ok": "完成", "warn": "提醒",
                               "danger": "异常"}.get(item.kind, "提示"), item.kind)
                    ui.label(item.body).classes("hp-msg-body")
                ui.label(item.created.strftime("%m-%d %H:%M")).classes("hp-msg-time")
            msg.on("click", lambda _, it=item: open_message(it, refresh))


def open_message(item: Notification, refresh: Callable) -> None:
    item.unread = False
    with ui.dialog() as dialog, ui.card().classes("hp-dialog"):
        ui.label(item.title).classes("hp-dialog-title")
        ui.label(item.body).style("font-size:13.5px;color:#3F5A70;line-height:1.7;"
                                 "margin-top:6px")
        with ui.element("div").style("display:flex;justify-content:flex-end;margin-top:14px"):
            action_button("知道了", lambda: (dialog.close(), refresh()), "primary")

    dialog.open()


# --------------------------------------------------------------------------- #
#  系统管理（含局域网打印机 / Wi-Fi）
# --------------------------------------------------------------------------- #
def page_system(role: str, refresh: Callable) -> None:
    active = STORE.active_queue()

    with card("连接状态", "printer"):
        with ui.element("div").style("display:flex;justify-content:space-between;"
                                     "align-items:center;gap:16px;flex-wrap:wrap"):
            with ui.element("div"):
                with ui.element("div").style("display:flex;gap:8px;align-items:center;"
                                             "margin-bottom:4px;flex-wrap:wrap"):
                    ui.label(active.name).style("font-size:16px;font-weight:700;color:#16233A")
                    badge(active.kind_label, "ok" if active.is_network else "info")
                    if active.enabled and active.accepting:
                        badge("接受任务", "neutral")
                    else:
                        badge("暂停", "warn")
                ui.label(active.uri).classes("hp-mono")
            with ui.element("div").style("display:flex;gap:8px;flex-wrap:wrap"):
                def refresh_status() -> None:
                    say("已刷新队列状态（原型：数据来自内存）", type="positive")
                    refresh()

                action_button("刷新状态", refresh_status)
                action_button("打测试页", lambda: test_print(active.name), "primary")

    with card("打印机队列", "server", f"共 {len(STORE.queues)} 个"):
        with ui.element("div").classes("hp-table"):
            with ui.element("div").classes("hp-tr hp-th") \
                    .style("grid-template-columns:1.1fr 2.2fr .9fr 1.6fr"):
                ui.label("队列")
                ui.label("连接地址")
                ui.label("类型")
                ui.label("操作")
            for queue in STORE.queues:
                is_active = queue.name == STORE.selected
                with ui.element("div").classes("hp-tr") \
                        .style("grid-template-columns:1.1fr 2.2fr .9fr 1.6fr"):
                    with ui.element("div").style("display:flex;gap:6px;align-items:center;"
                                                 "flex-wrap:wrap"):
                        ui.label(queue.name).classes("hp-cell-strong")
                        if is_active:
                            badge("使用中", "info")
                    ui.label(queue.uri).classes("hp-mono")
                    with ui.element("div").style("display:flex;gap:6px;align-items:center"):
                        icon(queue.kind_icon, 15)
                        ui.label(queue.kind_label).style("font-size:12.5px")
                    with ui.element("div").style("display:flex;gap:6px;flex-wrap:wrap"):
                        if not is_active:
                            action_button("使用",
                                          lambda _, name=queue.name: use_queue(name, refresh))
                        action_button("测试",
                                      lambda _, name=queue.name: test_print(name))
                        if not is_active:
                            action_button("删除",
                                          lambda _, name=queue.name: confirm_delete(name, refresh),
                                          "danger")

    with card("添加局域网打印机", "wifi", "把局域网里的打印机接进来"):
        with ui.element("div").style("display:flex;gap:8px;align-items:center;flex-wrap:wrap"):
            async def scan() -> None:
                say("正在扫描局域网（mDNS + lpinfo，约 1 秒）…", type="info", timeout=1200)
                await asyncio.sleep(1.0)
                STORE.discovered = [
                    Discovered("ipp://192.168.1.50/ipp/print",
                               "HP DeskJet 4900 series", "ippfind"),
                    Discovered("dnssd://HP%20DeskJet%204900._ipp._tcp.local/",
                               "HP DeskJet 4900（Bonjour）", "lpinfo"),
                    Discovered("ipp://192.168.1.77:631/ipp/print",
                               "HP LaserJet M404dn", "ippfind"),
                ]
                say(f"发现 {len(STORE.discovered)} 台打印机", type="positive")
                refresh()

            action_button("扫描局域网打印机", scan, "primary")
            ui.label("用 mDNS / lpinfo 找同一网段的 AirPrint / IPP 打印机") \
                .classes("hp-muted").style("font-size:12.5px")

        if STORE.discovered:
            ui.element("div").style("height:12px")
            for item in STORE.discovered:
                with ui.element("div").classes("hp-found"):
                    with ui.element("div").style("flex:1;min-width:220px"):
                        ui.label(item.name).style("font-size:13px;font-weight:600;color:#16233A")
                        ui.label(item.uri).classes("hp-mono")
                        ui.label(f"来源：{item.source}").classes("hp-muted") \
                            .style("font-size:11.5px")
                    name_input = ui.input("队列名", value=suggest_name(item.uri)) \
                        .props("outlined dense").style("width:200px")
                    action_button("添加",
                                  lambda _, it=item, inp=name_input: add_discovered(it, inp, refresh),
                                  "navy")

        ui.element("div").style("height:16px")
        ui.label("或手动添加").classes("hp-muted").style("font-size:12.5px;font-weight:600")

        with ui.element("div").classes("hp-form hp-form-2").style("margin-top:8px"):
            uri_input = ui.input("打印机地址", placeholder="ipp://192.168.1.50/ipp/print") \
                .props("outlined dense").classes("w-full")
            name_input = ui.input("队列名", placeholder="HP4928-WIFI") \
                .props("outlined dense").classes("w-full")
            model_input = ui.input("驱动（可留空，默认 IPP Everywhere）",
                                   placeholder="drv:///hpcups.drv/hp-deskjet_4900_series.ppd") \
                .props("outlined dense").classes("w-full").style("grid-column:1 / -1")

        def submit_manual() -> None:
            ok, message = STORE.add_queue(name_input.value or "", uri_input.value or "",
                                          model_input.value or "")
            say(message, type="positive" if ok else "negative")
            if ok:
                refresh()

        with ui.element("div").style("display:flex;gap:8px;margin-top:10px"):
            action_button("添加并设为默认", submit_manual, "primary")

        ui.label(
            "前提：打印机要先连上 Wi-Fi（4928 只支持 2.4G）并有 IP；地址一般是 "
            "ipp://打印机IP/ipp/print。老机型不支持 IPP Everywhere 时，请在「驱动」填 PPD，"
            "例如 drv:///hpcups.drv/hp-deskjet_4900_series.ppd。增删队列需要服务账号属于 lpadmin 组。"
        ).classes("hp-hint").style("margin-top:14px")

    with ui.element("div").classes("hp-grid hp-grid-2"):
        with card("服务信息", "server"):
            with ui.element("div").classes("hp-kv"):
                kv("监听地址", "0.0.0.0:10086")
                kv("数据目录", "/var/lib/hp4928-print")
                kv("降级策略", "USB 不在线 → 网络队列 → 虚拟打印机")
                kv("任务保留", "14 天（失败任务 30 天）")
                kv("版本", "v1.0.0 · NiceGUI 原型")
        with card("打印默认值", "printer"):
            with ui.element("div").classes("hp-chips"):
                for text in ("A4", "黑白", "单面", "600 dpi", "手动双面", "6 寸 4x6in"):
                    ui.label(text).classes("hp-chip")
            ui.label("默认值来自服务配置，改配置需重启服务；本页仅展示。") \
                .classes("hp-hint").style("margin-top:12px")


def suggest_name(uri: str) -> str:
    host = ""
    if "://" in uri:
        host = uri.split("://", 1)[1].split("/", 1)[0].split(":")[0]
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", host).strip("-") or "PRINTER"
    return f"NET-{cleaned}"[:60]


def add_discovered(item: Discovered, name_input, refresh: Callable) -> None:
    ok, message = STORE.add_queue(name_input.value or "", item.uri)
    say(message, type="positive" if ok else "negative")
    if ok:
        STORE.discovered = [d for d in STORE.discovered if d.uri != item.uri]
        refresh()


def use_queue(name: str, refresh: Callable) -> None:
    STORE.set_active(name)
    queue = STORE.active_queue()
    say(f"已切换到 {queue.name}（{queue.kind_label}）", type="positive")
    refresh()


def test_print(name: str) -> None:
    say(f"测试页已送 {name}：1 页 · A4 · 黑白", type="positive")


def confirm_delete(name: str, refresh: Callable) -> None:
    with ui.dialog() as dialog, ui.card().classes("hp-dialog"):
        ui.label(f"删除队列 {name}？").classes("hp-dialog-title")
        ui.label("只删除 CUPS 队列，不影响打印机本身。当前使用中的队列不能删除。") \
            .style("font-size:13px;color:#3F5A70;margin-top:6px")

        def do_delete() -> None:
            ok, message = STORE.delete_queue(name)
            dialog.close()
            say(message, type="positive" if ok else "negative")
            if ok:
                refresh()

        with ui.element("div").style("display:flex;justify-content:flex-end;gap:8px;"
                                     "margin-top:16px"):
            action_button("取消", dialog.close)
            action_button("删除", do_delete, "danger")

    dialog.open()


# --------------------------------------------------------------------------- #
#  用户权限
# --------------------------------------------------------------------------- #
def page_users(role: str, refresh: Callable) -> None:
    with card("用户列表", "users", f"共 {len(STORE.users)} 个账号"):
        with ui.element("div").style("display:flex;justify-content:flex-end;margin-bottom:8px"):
            action_button("新增用户", lambda: open_new_user(refresh), "primary")

        with ui.element("div").classes("hp-table"):
            with ui.element("div").classes("hp-tr hp-th") \
                    .style("grid-template-columns:1.1fr 1fr .8fr 1fr 1.7fr"):
                ui.label("账号")
                ui.label("姓名")
                ui.label("角色")
                ui.label("最近登录")
                ui.label("操作")
            for user in STORE.users:
                with ui.element("div").classes("hp-tr") \
                        .style("grid-template-columns:1.1fr 1fr .8fr 1fr 1.7fr"):
                    with ui.element("div").style("display:flex;gap:6px;align-items:center;"
                                                 "flex-wrap:wrap"):
                        ui.label(user.username).classes("hp-cell-strong")
                        if not user.active:
                            badge("已停用", "neutral")
                    ui.label(user.display)
                    with ui.element("div"):
                        badge("管理员" if user.role == "admin" else "普通用户",
                              "info" if user.role == "admin" else "neutral")
                    ui.label(user.last_login or "—").classes("hp-muted").style("font-size:12px")
                    with ui.element("div").style("display:flex;gap:6px;flex-wrap:wrap"):
                        if user.username == "admin":
                            ui.label("内置管理员").classes("hp-muted").style("font-size:12px")
                        else:
                            action_button("重置密码",
                                          lambda _, name=user.username: reset_password(name))
                            action_button("停用" if user.active else "启用",
                                          lambda _, u=user: toggle_user(u, refresh))
                            action_button("设为管理员" if user.role == "user" else "设为普通用户",
                                          lambda _, u=user: toggle_role(u, refresh))

    with card("角色与权限说明", "lock"):
        with ui.element("div").classes("hp-kv"):
            kv("普通用户", "工作台、个人中心、消息通知")
            kv("管理员", "以上全部 + 系统管理、用户权限")
            kv("打印机管理", "仅管理员可增删/切换队列")
            kv("演示提示", "侧边栏底部可切换身份，菜单会立即刷新")


def open_new_user(refresh: Callable) -> None:
    with ui.dialog() as dialog, ui.card().classes("hp-dialog"):
        ui.label("新增用户").classes("hp-dialog-title")
        with ui.element("div").classes("hp-form").style("margin-top:12px"):
            username = ui.input("登录名", placeholder="字母数字，3-32 位") \
                .props("outlined dense").classes("w-full")
            display = ui.input("显示名", placeholder="张三") \
                .props("outlined dense").classes("w-full")
            role_select = ui.select({"user": "普通用户", "admin": "管理员"}, value="user",
                                    label="角色").props("outlined dense").classes("w-full")

        def create() -> None:
            name = (username.value or "").strip()
            if not USERNAME_RE.match(name):
                say("登录名只能是 3-32 位字母/数字/下划线/点/短横线", type="warning")
                return
            if any(user.username == name for user in STORE.users):
                say(f"用户 {name} 已存在", type="negative")
                return
            password = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ"
                                              "abcdefghijkmnopqrstuvwxyz23456789")
                               for _ in range(12))
            chosen = getattr(role_select.value, "value", role_select.value) or "user"
            STORE.users.append(User(name, (display.value or name).strip(), chosen,
                                    True, "从未登录"))
            dialog.close()
            say(f"已创建 {name}，初始密码：{password}（只显示一次）",
                      type="positive", timeout=9000)
            refresh()

        with ui.element("div").style("display:flex;justify-content:flex-end;gap:8px;"
                                     "margin-top:16px"):
            action_button("取消", dialog.close)
            action_button("创建", create, "primary")

    dialog.open()


def reset_password(name: str) -> None:
    password = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ"
                                      "abcdefghijkmnopqrstuvwxyz23456789")
                       for _ in range(12))
    say(f"{name} 的新密码：{password}（演示，仅提示一次）", type="positive", timeout=8000)


def toggle_user(user: User, refresh: Callable) -> None:
    user.active = not user.active
    say(f"{user.username} 已{'启用' if user.active else '停用'}",
              type="positive" if user.active else "warning")
    refresh()


def toggle_role(user: User, refresh: Callable) -> None:
    admins = [item for item in STORE.users if item.role == "admin" and item.active]
    if user.role == "admin" and len(admins) <= 1:
        say("至少要保留一个管理员", type="negative")
        return
    user.role = "user" if user.role == "admin" else "admin"
    say(f"{user.username} 现在是{role_label(user.role)}", type="positive")
    refresh()


# --------------------------------------------------------------------------- #
#  外壳 + 状态
# --------------------------------------------------------------------------- #
@ui.page("/")
def index() -> None:
    session: dict = {"role": "admin", "page": "dashboard", "filter": "all"}

    with ui.element("div").classes("hp-shell"):
        with ui.element("aside").classes("hp-sidebar"):
            with ui.element("div").classes("hp-logo"):
                with ui.element("div").classes("hp-logo-mark"):
                    ui.label("hp")
                with ui.element("div").classes("hp-logo-text"):
                    ui.label("打印服务控制台").classes("hp-logo-title")
                    ui.label("DeskJet 4928").classes("hp-logo-sub")
            nav_box = ui.element("div").classes("hp-nav")
            role_box = ui.element("div").classes("hp-rolebox")

        with ui.element("div").classes("hp-main"):
            topbar = ui.element("header").classes("hp-topbar")
            content = ui.element("div").classes("hp-content")

    def refresh() -> None:
        """延迟一帧重建，避免销毁正在处理事件的控件。"""
        ui.timer(0.02, render_all, once=True)

    def go(page: str) -> None:
        item = next((it for it in NAV_ITEMS if it.key == page), None)
        if item is None or not can_see(item, session["role"]):
            say("没有权限访问该页面", type="warning")
            return
        session["page"] = page
        refresh()

    def switch_role(role: str) -> None:
        session["role"] = role
        current = next((it for it in NAV_ITEMS if it.key == session["page"]), None)
        if current is not None and not can_see(current, role):
            session["page"] = "dashboard"
        say(f"已切换为{role_label(role)}，菜单已刷新", type="positive")
        refresh()

    def render_nav() -> None:
        nav_box.clear()
        with nav_box:
            for item in NAV_ITEMS:
                if item.admin_only and session["role"] != "admin":
                    continue
                if item.key == "system":
                    ui.label("管理").classes("hp-nav-label")
                active = session["page"] == item.key
                el = ui.element("div").classes("hp-nav-item" + (" is-active" if active else ""))
                with el:
                    ui.html(icon_svg(item.icon, 19), sanitize=False).classes("hp-nav-icon")
                    ui.label(item.label).classes("hp-nav-text")
                    if item.key == "messages" and STORE.unread_count():
                        ui.label(str(STORE.unread_count())).classes("hp-nav-badge")
                el.on("click", lambda _, key=item.key: go(key))

    def render_role_box() -> None:
        role_box.clear()
        with role_box:
            with ui.element("div").classes("hp-userchip"):
                with ui.element("div").classes("hp-avatar"):
                    ui.label("管" if session["role"] == "admin" else "张")
                with ui.element("div"):
                    ui.label("系统管理员" if session["role"] == "admin" else "张三") \
                        .classes("hp-userchip-name")
                    ui.label(role_label(session["role"])).classes("hp-userchip-role")

            def on_role_change(event) -> None:
                value = getattr(event, "value", event)
                switch_role(value if value in {"admin", "user"} else "user")

            ui.select({"user": "普通用户", "admin": "管理员"},
                      value=session["role"], label="身份切换",
                      on_change=on_role_change) \
                .props("outlined dense options-dense").classes("w-full")

            ui.label("切换身份后菜单立即刷新").classes("hp-muted") \
                .style("font-size:11px;text-align:center")

    def render_topbar() -> None:
        topbar.clear()
        page = session["page"]
        with topbar:
            with ui.element("div").classes("hp-topbar-title"):
                ui.label(f"当前在：{PAGE_TITLES.get(page, '工作台')}").classes("hp-title")
                ui.label("HP DeskJet 4928 打印服务 / " + PAGE_TITLES.get(page, "")) \
                    .classes("hp-crumb")
            with ui.element("div").style("display:flex;align-items:center;gap:10px"):
                badge("服务在线", "ok")

                bell = ui.element("div").style(
                    "position:relative;cursor:pointer;display:flex;align-items:center;"
                    "color:#5A6B80")
                with bell:
                    ui.html(icon_svg("bell", 19), sanitize=False)
                    if STORE.unread_count():
                        ui.label(str(STORE.unread_count())) \
                            .classes("hp-nav-badge").style(
                                "position:absolute;top:-6px;right:-7px;font-size:10px;"
                                "line-height:15px;padding:0 5px")
                bell.on("click", lambda: go("messages"))

                badge(role_label(session["role"]), "info").classes("hp-role-badge")
                with ui.element("div").classes("hp-avatar") \
                        .style("width:30px;height:30px;font-size:12px"):
                    ui.label("管" if session["role"] == "admin" else "张")

    def render_content() -> None:
        content.clear()
        page = session["page"]
        with content:
            if page == "dashboard":
                page_dashboard(session["role"], refresh)
            elif page == "profile":
                page_profile(session["role"], refresh)
            elif page == "messages":
                page_messages(session["role"], refresh, session)
            elif page == "system":
                page_system(session["role"], refresh)
            elif page == "users":
                page_users(session["role"], refresh)
            else:
                page_dashboard(session["role"], refresh)

    def render_all() -> None:
        render_nav()
        render_role_box()
        render_topbar()
        render_content()

    render_all()


# --------------------------------------------------------------------------- #
#  健康检查
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz():
    return {"ok": True, "app": "hp4928-ui-prototype"}


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="HP 4928 打印服务控制台（NiceGUI 原型）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址；局域网预览用 0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--show", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    ui.add_head_html(f"<style>{CSS}</style>", shared=True)
    ui.run(
        host=args.host,
        port=args.port,
        title="HP 4928 打印服务控制台",
        favicon="🖨️",
        dark=False,
        reload=False,
        show=args.show,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()

# --------------------------------------------------------------------------- #
#  接真实后端（下一步）
# --------------------------------------------------------------------------- #
#  A. 把 STORE 换成 HTTP 客户端，直接调用现有 Flask 服务：
#       POST /api/auth/login           登录，拿会话 Cookie + CSRF
#       GET  /api/printers             队列列表 / selected
#       POST /api/printers/select      切换队列
#       GET  /api/printers/discover    扫描局域网
#       POST /api/printers/add         添加 ipp:// 队列
#       POST /api/printers/test        打测试页
#       GET  /api/jobs                 任务列表
#       POST /api/jobs/<id>/continue   手动双面「继续打印」
#     建议用 httpx.AsyncClient 保存 cookie，登录后把 X-CSRF-Token 放进默认 headers。
#
#  B. 保留 NiceGUI 做前端，把 hp4928 的 jobs/printers/ppd/docs 当库直接调用，
#     逐步把页面迁过来，最后用 ui.run 取代 waitress；这样少了 HTTP 层，但要处理
#     多客户端下的任务刷新（可用 ui.timer 定时拉 /api/jobs 或直接读 DB）。
# --------------------------------------------------------------------------- #
