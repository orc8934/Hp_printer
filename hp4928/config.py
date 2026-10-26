"""配置加载：默认值 → /etc 配置 → 项目配置 → 本地配置 → 环境变量。"""

from __future__ import annotations

import copy
import ipaddress
import os
import secrets
import socket
import stat
from pathlib import Path
from typing import Any, Iterable

import yaml

from .default_config import defaults, deep_merge

ENV_CONFIG = "HP4928_CONFIG"
ENV_DATA_DIR = "HP4928_DATA_DIR"
ENV_SECRET = "HP4928_SECRET_KEY"

# 环境变量 → 配置项
_ENV_MAP: dict[str, str] = {
    "HP4928_HOST": "server.host",
    "HP4928_PORT": "server.port",
    "HP4928_THREADS": "server.threads",
    "HP4928_BACKEND": "printer.backend",
    "HP4928_PRINTER": "printer.name",
    "HP4928_LOG_LEVEL": "logging.level",
    "HP4928_SESSION_HOURS": "server.session_hours",
    "HP4928_TRUST_PROXY": "server.trust_proxy",
    "HP4928_BEHIND_HTTPS": "server.behind_https",
    "HP4928_ALLOWED_CIDRS": "server.allowed_cidrs",
    "HP4928_MAX_UPLOAD_MB": "server.max_upload_mb",
    "HP4928_FORCE_PASSWORD_CHANGE": "security.force_password_change",
}


def _coerce(key: str, value: str) -> Any:
    """把环境变量字符串转换成分层配置里对应的类型。"""
    if key == "server.allowed_cidrs":
        return [item.strip() for item in value.split(",") if item.strip()]
    if key in {"server.port", "server.threads", "server.session_hours", "server.max_upload_mb"}:
        return int(value)
    if key in {"server.trust_proxy", "server.behind_https", "security.force_password_change"}:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value


def _parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _parse_proc_ipv6(text: str) -> list[str]:
    """解析 /proc/net/if_inet6，只保留全局地址（scope=00，排除 link-local/回环）。"""
    addresses: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        raw, scope = parts[0], parts[3]
        if scope != "00":
            continue
        try:
            addr = ipaddress.IPv6Address(bytes.fromhex(raw))
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or addr.ipv4_mapped:
            continue
        text_addr = str(addr)
        if text_addr not in addresses:
            addresses.append(text_addr)
    return addresses


def ipv6_addresses() -> list[str]:
    """本机全局 IPv6 地址（Linux 读 /proc，其它平台退回 getaddrinfo）。

    DDNS 的 AAAA 记录必须指向这里的地址之一（通常是服务器自己的全局地址，
    不是路由器 WAN 口的地址）。
    """
    proc = Path("/proc/net/if_inet6")
    if proc.is_file():
        try:
            parsed = _parse_proc_ipv6(proc.read_text(encoding="utf-8", errors="replace"))
            if parsed:
                return parsed
        except OSError:
            pass
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            addr = str(info[4][0]).split("%", 1)[0]
            try:
                obj = ipaddress.IPv6Address(addr)
            except ValueError:
                continue
            if obj.is_loopback or obj.is_link_local:
                continue
            if addr not in found:
                found.append(addr)
    except (OSError, ValueError):
        pass
    return found


class Config:
    """分层配置对象。"""

    def __init__(self, raw: dict[str, Any], root: Path, sources: Iterable[Path] = ()) -> None:
        self._raw = raw
        self.root = root
        self.sources = [Path(item) for item in sources]

    # ------------------------------------------------------------------ 读取
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._raw)

    # ------------------------------------------------------------------ 路径
    @property
    def _data_dir_raw(self) -> str:
        return str(self.get("paths.data_dir") or "data")

    @property
    def data_dir(self) -> Path:
        raw = os.environ.get(ENV_DATA_DIR) or self._data_dir_raw
        path = Path(raw)
        if not path.is_absolute():
            path = self.root / path
        return path

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def prepared_dir(self) -> Path:
        return self.data_dir / "prepared"

    @property
    def spool_dir(self) -> Path:
        explicit = str(self.get("printer.virtual.dir") or "").strip()
        if not explicit:
            return self.data_dir / "spool"
        path = Path(explicit)
        return path if path.is_absolute() else (self.root / path)

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hp4928.sqlite3"

    @property
    def secret_file(self) -> Path:
        return self.data_dir / "secret_key"

    @property
    def soffice_profile_dir(self) -> Path:
        """LibreOffice 专用 profile，避免多任务互相锁死。"""
        return self.data_dir / "soffice-profile"

    def ensure_layout(self) -> None:
        """创建运行所需目录，并尽量收紧权限。"""
        try:
            for directory in (
                self.data_dir,
                self.upload_dir,
                self.prepared_dir,
                self.spool_dir,
                self.log_dir,
                self.soffice_profile_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                f"数据目录不可写：{self.data_dir}（{exc.strerror or exc}）。\n"
                "常见原因：\n"
                "  1) 数据目录落在只读路径（例如 /opt），systemd 单元用了 ProtectSystem=strict；\n"
                "     解决：配置里写 paths.data_dir: /var/lib/hp4928-print，"
                "或给服务加环境变量 HP4928_DATA_DIR=/var/lib/hp4928-print；\n"
                "  2) 目录属主不是运行服务的用户；\n"
                "     解决：sudo chown -R hp4928:hp4928 /var/lib/hp4928-print"
            ) from exc
        if os.name == "posix":
            for directory in (self.data_dir, self.upload_dir, self.prepared_dir):
                try:
                    os.chmod(directory, 0o700)
                except OSError:
                    pass

    # ------------------------------------------------------------------ 密钥
    def secret_key(self) -> str:
        """会话签名密钥：env > 文件 > 首次生成。"""
        from_env = os.environ.get(ENV_SECRET)
        if from_env:
            return from_env
        configured = str(self.get("security.secret_key") or "")
        if configured:
            return configured
        self.ensure_layout()
        try:
            if self.secret_file.exists():
                value = self.secret_file.read_text(encoding="utf-8").strip()
                if value:
                    return value
            value = secrets.token_urlsafe(48)
            self.secret_file.write_text(value + "\n", encoding="utf-8")
            if os.name == "posix":
                os.chmod(self.secret_file, stat.S_IRUSR | stat.S_IWUSR)
            return value
        except OSError:
            # 只读环境下的兜底：进程内随机（重启后会话失效）
            return secrets.token_urlsafe(48)

    # ------------------------------------------------------------------ 其他
    @property
    def port(self) -> int:
        return int(self.get("server.port", 10086))

    @property
    def host(self) -> str:
        return str(self.get("server.host", "0.0.0.0"))

    @property
    def listen_ipv6(self) -> bool:
        """是否额外在 [::]:port 上监听（IPv4 与 IPv6 需要两个 listener）。"""
        value = self.get("server.listen_ipv6", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @property
    def ipv6_addresses(self) -> list[str]:
        return ipv6_addresses()

    def lan_urls(self) -> list[str]:
        """猜出可访问地址，方便把网址告诉手机/同事（IPv6 用方括号）。"""
        port = self.port
        urls: list[str] = []

        def add(raw: str) -> None:
            host = str(raw).strip().split("%", 1)[0]        # 去掉 fe80::1%eth0 的 zone
            if not host:
                return
            literal = f"[{host}]" if ":" in host else host
            url = f"http://{literal}:{port}/"
            if url not in urls:
                urls.append(url)

        wants_v6 = self.listen_ipv6 or ":" in self.host
        bypass = self.host in {"0.0.0.0", "::", "[::]", ""}

        if not bypass:
            add(self.host)
        else:
            # IPv4 局域网地址
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    sock.connect(("223.5.5.5", 80))
                    candidate = sock.getsockname()[0]
                    if not candidate.startswith("127."):
                        add(candidate)
                finally:
                    sock.close()
            except OSError:
                pass
            try:
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                    candidate = info[4][0]
                    if not candidate.startswith("127."):
                        add(candidate)
            except OSError:
                pass
            # 全局 IPv6（DDNS 要指的就是这些地址之一）
            if wants_v6:
                for candidate in self.ipv6_addresses[:3]:
                    add(candidate)

        add("127.0.0.1")
        if wants_v6:
            add("::1")
        return urls

    def describe_sources(self) -> str:
        if not self.sources:
            return "（仅内置默认值）"
        return ", ".join(str(item) for item in self.sources)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"配置文件无法读取：{path}（{exc}）") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"配置文件顶层必须是键值对：{path}")
    return loaded


class ConfigError(RuntimeError):
    """配置错误（直接展示给用户）。"""


def config_candidates(root: Path) -> list[Path]:
    """按优先级从低到高返回候选配置文件。"""
    return [
        Path("/etc/hp4928-print/config.yaml"),
        root / "config.yaml",
        root / "config.local.yaml",
    ]


def load_config(root: Path | None = None, extra: dict[str, Any] | None = None) -> Config:
    """加载配置。root 默认为项目根目录。"""
    project_root = Path(root or Path(__file__).resolve().parent.parent).resolve()
    merged = defaults()
    sources: list[Path] = []

    explicit = os.environ.get(ENV_CONFIG)
    candidates = [Path(explicit)] if explicit else config_candidates(project_root)
    for candidate in candidates:
        try:
            if candidate.is_file():
                merged = deep_merge(merged, _read_yaml(candidate))
                sources.append(candidate)
        except OSError:
            continue

    if extra:
        merged = deep_merge(merged, extra)

    # 环境变量覆盖（最高优先级）
    for env_name, key in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        try:
            value = _coerce(key, raw)
        except ValueError:
            continue
        node = merged
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    # 兜底类型修正
    merged.setdefault("paths", {})
    merged["server"]["port"] = int(merged["server"]["port"])
    merged["server"]["threads"] = max(1, int(merged["server"]["threads"]))
    merged["server"]["trust_proxy"] = _parse_bool(merged["server"]["trust_proxy"])
    merged["security"]["force_password_change"] = _parse_bool(
        merged["security"]["force_password_change"]
    )

    return Config(merged, project_root, sources)


def dump_example_yaml(cfg: Config) -> str:
    """导出带注释的示例配置。"""
    header = (
        "# HP 4928 打印服务配置示例\n"
        "# 复制为 config.yaml（或 /etc/hp4928-print/config.yaml）后按需修改。\n"
        "# 所有字段均有内置默认值，删掉也能启动。\n"
        "# 端口、账号密码等说明见 README.md。\n\n"
    )
    return header + yaml.safe_dump(
        cfg.as_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False
    )
