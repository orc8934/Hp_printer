"""打印机发现、后端选择与自检。

降级策略（对应需求「USB 行不通的话」）：
1. CUPS + USB 队列可用   → 走 CUPS（真实打印）
2. USB 掉线 / 队列不存在 → 若是 Wi-Fi 队列则走网络，否则降级虚拟打印机
3. 完全没有 CUPS        → 虚拟打印机（输出 PDF，界面明确标注）
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .cups import QueueState, VirtualPrinter, cups_available, make_printer, scheduler_running
from .proc import run, which

USB_URI_RE = re.compile(r"(usb://[^\s]+|hp:/usb/[^\s]+)", re.IGNORECASE)
PRINTER_HINTS = (
    "deskjet", "officejet", "envy", "photosmart", "laserjet", "smart tank", "smarttank",
    "4920", "4925", "4926", "4928", "4978", "printer", "打印机",
)

# 可以通过「网络」连到打印机的地址方案
NETWORK_SCHEMES = ("ipp://", "ipps://", "dnssd://", "socket://", "lpd://", "hp:/net/", "http://")
# 队列名：CUPS 允许的字符子集，避免命令行注入
QUEUE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")


class BackendError(RuntimeError):
    """后端不可用且不允许降级。"""


@dataclass
class UsbDevice:
    uri: str = ""
    vendor_id: str = ""
    product_id: str = ""
    manufacturer: str = ""
    product: str = ""
    serial: str = ""

    @property
    def looks_like_printer(self) -> bool:
        blob = f"{self.product} {self.manufacturer} {self.uri}".lower()
        return any(hint in blob for hint in PRINTER_HINTS)

    def to_public(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "manufacturer": self.manufacturer,
            "product": self.product,
            "serial": self.serial,
            "looks_like_printer": self.looks_like_printer,
        }


@dataclass
class Backend:
    kind: str                      # cups | virtual
    queue: str = ""
    reason: str = ""
    device_uri: str = ""
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def is_real(self) -> bool:
        return self.kind == "cups"

    def to_public(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "queue": self.queue,
            "reason": self.reason,
            "device_uri": self.device_uri,
            "degraded": self.degraded,
            "real_printer": self.is_real,
            "warnings": self.warnings,
        }


@dataclass
class Check:
    name: str
    status: str        # ok | warn | fail
    detail: str = ""
    hint: str = ""

    def to_public(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "hint": self.hint}


# --------------------------------------------------------------------- USB 探测
def _lpinfo_uris(cfg) -> list[str]:
    lpinfo = which(str(cfg.get("printer.cups.lpinfo", "lpinfo") or "lpinfo"))
    if not lpinfo:
        return []
    result = run([lpinfo, "-v"], timeout=20)
    if not result.ok:
        return []
    uris = []
    for line in result.output.splitlines():
        stripped = line.strip()
        if stripped.startswith("direct ") or stripped.startswith("network "):
            parts = stripped.split(None, 1)
            if len(parts) == 2:
                uris.append(parts[1].strip())
    return uris


def _sys_usb_devices(cfg) -> list[UsbDevice]:
    """直接读 /sys 判断 USB 上有没有 HP 设备（即使 CUPS 没队列）。"""
    base = Path("/sys/bus/usb/devices")
    if not base.is_dir():
        return []
    vendor_ids = {str(item).lower() for item in (cfg.get("printer.usb.hp_vendor_ids") or ["03f0"])}
    devices: list[UsbDevice] = []
    for entry in sorted(base.iterdir()):
        try:
            vendor = (entry / "idVendor").read_text(encoding="utf-8").strip().lower()
            product_id = (entry / "idProduct").read_text(encoding="utf-8").strip()
        except (OSError, NotADirectoryError):
            continue
        if vendor not in vendor_ids:
            continue

        def read(name: str) -> str:
            try:
                return (entry / name).read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return ""

        devices.append(
            UsbDevice(
                uri="",
                vendor_id=vendor,
                product_id=product_id,
                manufacturer=read("manufacturer"),
                product=read("product"),
                serial=read("serial"),
            )
        )
    return devices


def detect_usb(cfg) -> list[UsbDevice]:
    """汇总 CUPS 看到的 USB URI 与 /sys 里的 HP USB 设备。"""
    devices: list[UsbDevice] = []
    seen: set[str] = set()
    for uri in _lpinfo_uris(cfg):
        match = USB_URI_RE.search(uri)
        if not match:
            continue
        raw = match.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        readable = unquote(raw)
        head = readable.split("?", 1)[0]
        manufacturer = ""
        product = ""
        serial = ""
        if head.lower().startswith("usb://"):
            segments = head[len("usb://"):].split("/")
            if segments:
                manufacturer = segments[0].replace("+", " ")
            if len(segments) >= 2:
                product = segments[1].replace("+", " ")
        if "?" in readable:
            for pair in readable.split("?", 1)[1].split("&"):
                if pair.lower().startswith("serial="):
                    serial = pair.split("=", 1)[1]
        devices.append(
            UsbDevice(uri=raw, manufacturer=manufacturer, product=product, serial=serial)
        )
    for device in _sys_usb_devices(cfg):
        if device.vendor_id and device.product_id:
            key = f"{device.vendor_id}:{device.product_id}:{device.serial}"
            if key in seen:
                continue
            seen.add(key)
            devices.append(device)
    return devices


def usb_printer_present(cfg) -> tuple[bool, str]:
    devices = detect_usb(cfg)
    print_like = [item for item in devices if item.looks_like_printer]
    if print_like:
        first = print_like[0]
        label = first.product or first.uri or f"{first.vendor_id}:{first.product_id}"
        return True, label
    if devices:
        return False, f"检测到 {len(devices)} 个 HP USB 设备，但没有一个是打印机"
    return False, "没有检测到 HP USB 设备"


# --------------------------------------------------------------------- CUPS 队列
def list_queues(cfg) -> list[QueueState]:
    if not cups_available(cfg):
        return []
    lpstat = which(str(cfg.get("printer.cups.lpstat", "lpstat") or "lpstat")) or "lpstat"
    printers = run([lpstat, "-p"], timeout=20)
    devices = run([lpstat, "-v"], timeout=20)
    accepting = run([lpstat, "-a"], timeout=20)
    default = run([lpstat, "-d"], timeout=20)

    uris: dict[str, str] = {}
    for line in devices.output.splitlines():
        if line.lower().startswith("device for "):
            body = line[len("device for "):]
            if ":" in body:
                name, uri = body.split(":", 1)
                uris[name.strip()] = uri.strip()

    accept_state: dict[str, bool] = {}
    for line in accepting.output.splitlines():
        if line.lower().startswith("system default") or " not accepting" in line.lower():
            if " not accepting" in line.lower():
                name = line.split(None, 1)[0]
                accept_state[name] = False
        elif line and "accepting requests" in line.lower():
            accept_state[line.split(None, 1)[0]] = True

    queues: list[QueueState] = []
    seen: set[str] = set()
    for line in printers.output.splitlines():
        if not line.lower().startswith("printer "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        if name in seen:
            continue
        seen.add(name)
        lowered = line.lower()
        queues.append(
            QueueState(
                name=name,
                device_uri=uris.get(name, ""),
                accepting=accept_state.get(name, True),
                enabled="disabled" not in lowered,
                state_text=" ".join(line.split())[:200],
            )
        )
    for name, uri in uris.items():
        if name not in seen:
            seen.add(name)
            queues.append(QueueState(name=name, device_uri=uri, state_text="（未在 lpstat -p 中列出）"))
    _ = default
    return queues


def pick_queue(cfg, queues: list[QueueState], preferred: str = "") -> QueueState | None:
    """按「界面选中的队列 → 配置名 → USB 队列 → 型号关键字 → 网络队列」优先挑一个。"""
    if not queues:
        return None
    for candidate in (preferred, str(cfg.get("printer.name", "") or "")):
        if not candidate:
            continue
        for queue in queues:
            if queue.name == candidate:
                return queue
    for queue in queues:
        if queue.device_uri.lower().startswith("usb://") and any(
            hint in f"{queue.name} {queue.device_uri}".lower() for hint in ("hp", "deskjet", "4920")
        ):
            return queue
    if cfg.get("printer.usb.allow_network_fallback", True):
        for queue in queues:
            if queue.device_uri.lower().startswith(("ipp://", "ipps://", "socket://", "dnssd://")):
                return queue
    return queues[0]


def select_backend(cfg, force: str | None = None, preferred_queue: str = "") -> Backend:
    """决定这次任务交给谁。preferred_queue 是界面上选中的队列（优先于配置）。"""
    requested = (force or str(cfg.get("printer.backend", "auto") or "auto")).lower()
    if requested == "virtual":
        return Backend("virtual", reason="已按配置强制使用虚拟打印机", degraded=True)

    if not cups_available(cfg):
        message = "未安装 CUPS 客户端（lp / lpstat），无法真机打印"
        if requested == "cups":
            raise BackendError(message + "；请先 apt install cups cups-client")
        return Backend("virtual", reason=message + "，已降级为虚拟打印机（输出 PDF）", degraded=True)

    if not scheduler_running(cfg):
        message = "CUPS 服务未运行"
        if requested == "cups":
            raise BackendError(message + "；请执行 systemctl enable --now cups")
        return Backend("virtual", reason=message + "，已降级为虚拟打印机（输出 PDF）", degraded=True)

    queues = list_queues(cfg)
    queue = pick_queue(cfg, queues, preferred_queue)
    if queue is None:
        message = f"CUPS 里没有任何打印机队列（配置的队列名：{cfg.get('printer.name')}）"
        if requested == "cups":
            raise BackendError(message + "；请先运行 scripts/setup-usb-printer.sh，或在设置页添加网络打印机")
        return Backend("virtual", reason=message + "，已降级为虚拟打印机（输出 PDF）", degraded=True)

    warnings: list[str] = []
    if not queue.enabled:
        warnings.append("队列处于停用状态（可能是缺纸、卡纸、盖板打开或驱动报错），任务会排队等待。")
    if not queue.accepting:
        warnings.append("队列当前不接受新任务，任务会排队等待。")

    # USB 掉线检测：只有直接连 USB 的队列才检查
    if cfg.get("printer.usb.require_device", True) and queue.device_uri.lower().startswith("usb://"):
        present, detail = usb_printer_present(cfg)
        if not present:
            message = f"{detail}，但队列 {queue.name} 绑定的 USB 设备不在线"
            if requested == "cups":
                raise BackendError(message + "；请检查 USB 线/电源，或改配置 printer.usb.require_device: false")
            return Backend(
                "virtual",
                queue=queue.name,
                device_uri=queue.device_uri,
                reason=message + "，已降级为虚拟打印机（输出 PDF）",
                degraded=True,
                warnings=warnings,
            )

    return Backend(
        "cups",
        queue=queue.name,
        device_uri=queue.device_uri,
        reason=f"CUPS 队列 {queue.name}（{queue.device_uri or '未知设备'}）",
        warnings=warnings,
    )


def printer_for(cfg, backend: Backend):
    return make_printer(cfg, backend.kind, backend.queue)


# --------------------------------------------------------------------- 网络打印机
@dataclass
class DiscoveredPrinter:
    uri: str
    name: str = ""
    scheme: str = ""
    source: str = "lpinfo"

    @property
    def is_network(self) -> bool:
        return self.uri.lower().startswith(NETWORK_SCHEMES)

    def to_public(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name or self.uri,
            "scheme": self.scheme,
            "source": self.source,
            "is_network": self.is_network,
        }


def _scheme_of(uri: str) -> str:
    return uri.split("://", 1)[0].lower() + "://" if "://" in uri else uri.split(":", 1)[0]


def _friendly_name(uri: str) -> str:
    try:
        parts = urlsplit(uri)
        host = parts.hostname or ""
        if host:
            return unquote(host)
    except ValueError:
        pass
    return uri[:60]


def _run(command: list[str], timeout: float = 30) -> Any:
    """薄封装，便于测试替换。"""
    return run(command, timeout=timeout)


def discover_network(cfg, scan_seconds: int = 4) -> list[DiscoveredPrinter]:
    """找出局域网里可用的打印机地址。

    两个来源：
    1. `lpinfo -v` 里 CUPS 已知的 network 设备（dnssd/ipp/ipps/socket）
    2. `ippfind` 主动 mDNS 探测（需要 cups-ipp-utils，可选）
    """
    found: dict[str, DiscoveredPrinter] = {}

    lpinfo = which(str(cfg.get("printer.cups.lpinfo", "lpinfo") or "lpinfo"))
    if lpinfo:
        result = _run([lpinfo, "-v"], 20)
        for line in result.output.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or parts[0] != "network":
                continue
            uri = parts[1].strip()
            if not uri.lower().startswith(NETWORK_SCHEMES):
                continue
            found.setdefault(
                uri,
                DiscoveredPrinter(uri=uri, name=_friendly_name(uri),
                                  scheme=_scheme_of(uri), source="lpinfo"),
            )

    ippfind = which("ippfind")
    if ippfind:
        result = _run([ippfind, "-T", str(max(1, int(scan_seconds)))], scan_seconds + 8)
        for line in result.output.splitlines():
            uri = line.strip()
            if not uri.lower().startswith(("ipp://", "ipps://")):
                continue
            if uri in found:
                continue
            found[uri] = DiscoveredPrinter(
                uri=uri, name=_friendly_name(uri), scheme=_scheme_of(uri), source="ippfind"
            )
    return sorted(found.values(), key=lambda item: item.uri)


def can_manage_queues(cfg) -> bool:
    """当前进程能不能执行 lpadmin（root 或 lpadmin 组成员）。"""
    if os.name != "posix":
        return False
    try:
        if os.geteuid() == 0:
            return True
    except AttributeError:
        return False
    try:
        import grp

        wanted = str(cfg.get("printer.cups.admin_group", "lpadmin") or "lpadmin")
        gid = grp.getgrnam(wanted).gr_gid
        groups = set(os.getgroups())
        groups.add(os.getgid())
        return gid in groups
    except (KeyError, OSError):
        return False


def manage_hint(cfg) -> str:
    if can_manage_queues(cfg):
        return ""
    return (
        "当前服务账号不在 lpadmin 组，无法创建/修改队列。请执行："
        "sudo usermod -aG lpadmin hp4928 && sudo systemctl restart hp4928-print"
    )


def add_queue(cfg, name: str, uri: str, model: str = "") -> dict[str, Any]:
    """把一个网络打印机地址加成 CUPS 队列（默认用 everywhere 驱动）。"""
    name = (name or "").strip()
    uri = (uri or "").strip()
    if not QUEUE_NAME_RE.match(name):
        raise BackendError("队列名只能用字母、数字、下划线、点、短横线，1-60 位，例如 HP4928-NET")
    if not uri.lower().startswith(NETWORK_SCHEMES):
        raise BackendError("只接受 ipp:// ipps:// dnssd:// socket:// lpd:// hp:/net/ 这类网络地址")
    if not can_manage_queues(cfg):
        raise BackendError(manage_hint(cfg))

    lpadmin = which(str(cfg.get("printer.cups.lpadmin", "lpadmin") or "lpadmin"))
    if not lpadmin:
        raise BackendError("找不到 lpadmin 命令，请先 apt install cups cups-client")

    timeout = float(cfg.get("printer.cups.timeout", 60) or 60)
    candidates = [model.strip()] if model.strip() else ["everywhere"]
    last = None
    for candidate in candidates:
        command = [
            lpadmin, "-p", name, "-E", "-v", uri, "-m", candidate,
            "-D", "由打印服务添加", "-L", "network",
        ]
        result = _run(command, timeout)
        last = result
        if not result.ok:
            continue
        for tool in ("cupsenable", "cupsaccept"):
            binary = which(tool)
            if binary:
                _run([binary, name], 20)
        return {"name": name, "uri": uri, "model": candidate, "message": f"已添加队列 {name}"}

    detail = last.summary() if last else "未知错误"
    raise BackendError(
        f"添加队列失败：{detail}\n"
        "常见原因：目标不是 IPP Everywhere/AirPrint 打印机（socket:// 老机型要指定 PPD）、"
        "地址不可达、或 CUPS 版本太老不支持 everywhere。"
        "可先跑 sudo hp-setup -i，或添加时手动填驱动（--model / 界面里的“驱动”栏）。"
    )


def delete_queue(cfg, name: str) -> dict[str, Any]:
    name = (name or "").strip()
    if not QUEUE_NAME_RE.match(name):
        raise BackendError("队列名不合法")
    if not can_manage_queues(cfg):
        raise BackendError(manage_hint(cfg))
    lpadmin = which(str(cfg.get("printer.cups.lpadmin", "lpadmin") or "lpadmin"))
    if not lpadmin:
        raise BackendError("找不到 lpadmin 命令")
    result = _run([lpadmin, "-x", name], 30)
    if not result.ok:
        raise BackendError(f"删除队列失败：{result.summary()}")
    return {"name": name, "message": f"已删除队列 {name}"}


# --------------------------------------------------------------------- 状态 / 自检
def status(cfg, preferred_queue: str = "") -> dict[str, Any]:
    """给界面状态条和设置页用。"""
    try:
        backend = select_backend(cfg, preferred_queue=preferred_queue)
    except BackendError as exc:
        return {
            "ok": False,
            "backend": {"kind": "cups", "queue": cfg.get("printer.name"), "degraded": False,
                        "real_printer": False, "reason": str(exc), "warnings": []},
            "usb": {"present": False, "detail": str(exc)},
            "queue": {},
            "selected": preferred_queue or str(cfg.get("printer.name", "")),
            "can_manage": can_manage_queues(cfg),
            "manage_hint": manage_hint(cfg),
            "queues": [],
        }
    present, detail = usb_printer_present(cfg)
    queue_state: dict[str, Any] = {}
    if backend.kind == "cups" and backend.queue:
        try:
            queue_state = printer_for(cfg, backend).state().to_public()
        except Exception as exc:  # noqa: BLE001 - 状态接口不能因为后端异常而 500
            queue_state = {"error": str(exc)}
    return {
        "ok": backend.kind == "cups" and not backend.warnings,
        "backend": backend.to_public(),
        "usb": {"present": present, "detail": detail},
        "queue": queue_state,
        "selected": preferred_queue or str(cfg.get("printer.name", "")),
        "can_manage": can_manage_queues(cfg),
        "manage_hint": manage_hint(cfg),
        "queues": [item.to_public() for item in list_queues(cfg)],
    }


def _check(name: str, status_value: str, detail: str, hint: str = "") -> Check:
    return Check(name=name, status=status_value, detail=detail, hint=hint)


def doctor(cfg) -> list[Check]:
    """环境体检，全部返回可操作的提示。"""
    checks: list[Check] = []

    checks.append(
        _check("操作系统", "ok" if sys.platform.startswith("linux") else "warn",
               f"{sys.platform} / Python {sys.version.split()[0]}",
               "" if sys.platform.startswith("linux") else "本服务面向 Debian 系 Linux，当前系统仅能虚拟打印")
    )

    # CUPS
    if not cups_available(cfg):
        checks.append(_check("CUPS 客户端", "fail", "找不到 lp / lpstat",
                             "sudo apt install -y cups cups-client"))
    else:
        checks.append(_check("CUPS 客户端", "ok", "lp / lpstat 可用"))
        running = scheduler_running(cfg)
        checks.append(
            _check("CUPS 服务", "ok" if running else "fail",
                   "scheduler is running" if running else "CUPS 未运行",
                   "" if running else "sudo systemctl enable --now cups")
        )

    # 队列
    queues = list_queues(cfg)
    if queues:
        names = ", ".join(item.name for item in queues)
        checks.append(_check("打印队列", "ok", f"发现 {len(queues)} 个队列：{names}"))
        queue = pick_queue(cfg, queues)
        if queue is not None:
            uri = queue.device_uri or "未知"
            checks.append(
                _check("目标队列", "ok" if queue.enabled and queue.accepting else "warn",
                       f"{queue.name} → {uri}", "" if queue.enabled else "cupsenable " + queue.name)
            )
            if uri.lower().startswith("usb://"):
                checks.append(_check("连接方式", "ok", "USB 直连"))
            elif uri.lower().startswith(("ipp://", "ipps://", "dnssd://", "socket://")):
                checks.append(_check("连接方式", "warn", f"网络队列（{uri}）",
                                     "USB 不可达时会走这个队列"))
    else:
        checks.append(_check("打印队列", "fail", "没有找到任何 CUPS 队列",
                             "运行 scripts/setup-usb-printer.sh 创建 HP 4928 队列"))

    # USB
    present, detail = usb_printer_present(cfg)
    checks.append(
        _check("USB 打印机", "ok" if present else "warn", detail,
               "" if present else "检查 USB 线与打印机电源；驱动安装见 README「USB 配置」")
    )

    # 驱动选项
    try:
        from .ppd import load_catalog, resolve_roles

        catalog = load_catalog(cfg)
        roles = resolve_roles(catalog)
        if catalog.source == "builtin":
            checks.append(_check("打印驱动", "warn", "未读到 HP 驱动选项，使用内置能力画像",
                                 "安装 hplip 并创建队列后会自动读取真实选项"))
        else:
            checks.append(
                _check("打印驱动", "ok",
                       f"{catalog.source}：{catalog.description or catalog.path}（{len(catalog.options)} 个选项）")
            )
        checks.append(
            _check("关键选项", "ok" if roles.get("media") and roles.get("color") else "warn",
                   "、".join(f"{key}={value}" for key, value in sorted(roles.items())) or "未识别",
                   "" if roles.get("media") else "驱动可能不完整")
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("打印驱动", "warn", f"读取失败：{exc}"))

    # Word 转换
    soffice = which("soffice") or which("libreoffice")
    checks.append(
        _check("LibreOffice（Word 支持）", "ok" if soffice else "fail",
               soffice or "未安装",
               "" if soffice else "sudo apt install -y libreoffice-writer")
    )

    # PDF 工具
    pdfinfo = which("pdfinfo")
    pdftoppm = which("pdftoppm")
    checks.append(
        _check("poppler-utils（PDF 信息）", "ok" if pdfinfo else "warn",
               pdfinfo or "未安装（会用 pypdf 兜底）",
               "" if pdfinfo else "sudo apt install -y poppler-utils")
    )
    checks.append(
        _check("pdftoppm（PDF 转照片）", "ok" if pdftoppm else "warn",
               pdftoppm or "未安装（PDF 转图片打印会用不了）",
               "" if pdftoppm else "sudo apt install -y poppler-utils")
    )

    # 网络打印机发现能力
    ippfind = which("ippfind")
    checks.append(
        _check("局域网打印机发现", "ok" if ippfind else "warn",
               ippfind or "未安装 cups-ipp-utils（只能用 lpinfo 看已知设备）",
               "" if ippfind else "sudo apt install -y cups-ipp-utils")
    )
    checks.append(
        _check("队列管理权限", "ok" if can_manage_queues(cfg) else "warn",
               "可以 lpadmin 增删队列" if can_manage_queues(cfg) else "当前账号不能改队列",
               manage_hint(cfg))
    )

    # 中文字体
    fc_list = which("fc-list")
    if fc_list:
        result = run([fc_list, ":lang=zh", "family"], timeout=20)
        if result.ok and result.output.strip():
            families = sorted({line.split(",")[0].strip() for line in result.output.splitlines() if line.strip()})
            checks.append(_check("中文字体", "ok", f"{len(families)} 个：{', '.join(families[:4])}"))
        else:
            checks.append(_check("中文字体", "warn", "没有中文字体，Word 转 PDF 会乱码/方框",
                                 "sudo apt install -y fonts-noto-cjk"))
    else:
        checks.append(_check("中文字体", "warn", "未安装 fontconfig",
                             "sudo apt install -y fontconfig fonts-noto-cjk"))

    # Python 依赖
    missing = []
    for module in ("flask", "waitress", "pypdf", "PIL", "yaml"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    checks.append(
        _check("Python 依赖", "ok" if not missing else "fail",
               "齐全" if not missing else "缺少：" + ", ".join(missing),
               "" if not missing else "source .venv/bin/activate && pip install -r requirements.txt")
    )

    # 数据目录
    try:
        cfg.ensure_layout()
        probe = cfg.data_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        usage = shutil.disk_usage(cfg.data_dir)
        free_gb = usage.free / (1024 ** 3)
        checks.append(
            _check("数据目录", "ok" if free_gb > 1 else "warn",
                   f"{cfg.data_dir} 可写，剩余 {free_gb:.1f} GB",
                   "" if free_gb > 1 else "磁盘空间不足，清理 data/ 下旧任务")
        )
    except OSError as exc:
        checks.append(_check("数据目录", "fail", f"不可写：{exc}", "检查目录权限"))

    # 数据库
    try:
        from . import db as db_mod

        if cfg.db_path.exists():
            conn = sqlite3.connect(str(cfg.db_path))
            try:
                conn.execute("SELECT 1 FROM jobs LIMIT 1")
                checks.append(_check("数据库", "ok", str(cfg.db_path)))
            finally:
                conn.close()
        else:
            checks.append(_check("数据库", "warn", "尚未初始化", "python3 -m hp4928.cli init"))
    except sqlite3.Error as exc:
        checks.append(_check("数据库", "fail", f"{exc}", "备份后删除 data/hp4928.sqlite3 重新初始化"))

    # 端口
    host, port = str(cfg.get("server.host", "0.0.0.0")), int(cfg.get("server.port", 10086))
    state, detail = port_status(host, port)
    checks.append(
        _check("服务端口", "ok" if state in {"listening", "free"} else "fail",
               f"{host}:{port} {detail}",
               "" if state in {"listening", "free"} else f"换个端口：HP4928_PORT=10087")
    )

    return checks


def port_status(host: str, port: int) -> tuple[str, str]:
    """listening（本服务已在跑）/ free / busy。"""
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    try:
        with socket.create_connection((probe_host, port), timeout=1.0):
            return "listening", "已有服务在监听（可能正是本服务）"
    except OSError:
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((probe_host, port))
        return "free", "可用"
    except OSError as exc:
        return "busy", f"被占用（{exc.strerror or exc}）"
    finally:
        sock.close()


def cleanup_old_files(cfg, conn) -> int:
    """清理过期任务文件（数据库记录保留）。"""
    days = max(1, int(cfg.get("retention.days", 14) or 14))
    failed_days = max(days, int(cfg.get("retention.keep_failed_days", 30) or 30))
    removed = 0
    oldest = _cutoff_iso(failed_days)
    rows = conn.execute(
        "SELECT id, state, created_at, source_path, pass1_path, pass2_path FROM jobs "
        "WHERE created_at < ?",
        (oldest,),
    ).fetchall()
    for row in rows:
        created = str(row["created_at"] or "")
        limit = _cutoff_iso(failed_days if row["state"] == "failed" else days)
        if created and created >= limit:
            continue
        for key in ("source_path", "pass1_path", "pass2_path"):
            path = Path(str(row[key] or ""))
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def _cutoff_iso(days: int) -> str:
    """与 db.utcnow() 同格式的截止时间字符串，可直接做字典序比较。"""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def spool_files(cfg) -> list[Path]:
    printer = VirtualPrinter(cfg, "virtual")
    try:
        return sorted(
            printer.output_dir.glob("*.pdf"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
