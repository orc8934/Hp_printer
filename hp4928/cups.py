"""打印投递：CUPS（lp）或虚拟打印机（落盘 PDF）。"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import utcnow
from .proc import run, which

_REQUEST_ID_RE = re.compile(r"request id is (\S+)", re.IGNORECASE)
_JOB_ID_RE = re.compile(r"^(\S+)-(\d+)\s", re.MULTILINE)


class PrintError(RuntimeError):
    """投递失败，消息直接展示给用户。"""


@dataclass
class PrintResult:
    ok: bool
    job_id: str = ""
    message: str = ""
    command: str = ""
    backend: str = ""
    output_path: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "job_id": self.job_id,
            "message": self.message,
            "command": self.command,
            "backend": self.backend,
            "output_path": self.output_path,
        }


@dataclass
class QueueState:
    name: str = ""
    device_uri: str = ""
    accepting: bool = True
    enabled: bool = True
    state_text: str = ""
    jobs: int = 0
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "device_uri": self.device_uri,
            "accepting": self.accepting,
            "enabled": self.enabled,
            "state": self.state_text,
            "jobs": self.jobs,
            "description": self.description,
        }


class BasePrinter:
    kind = "base"

    def __init__(self, cfg, queue: str) -> None:
        self.cfg = cfg
        self.queue = queue or str(cfg.get("printer.name", "HP4928"))

    def submit(self, pdf_path: Path, options: list[str], title: str, copies: int = 1) -> PrintResult:
        raise NotImplementedError

    def cancel(self, job_id: str) -> bool:
        return False

    def state(self) -> QueueState:
        raise NotImplementedError


class CupsPrinter(BasePrinter):
    """通过 CUPS 的 lp 命令投递（USB 与局域网队列都走这里）。"""

    kind = "cups"

    @property
    def lp(self) -> str:
        return which(str(self.cfg.get("printer.cups.lp", "lp") or "lp")) or "lp"

    @property
    def lpstat(self) -> str:
        return which(str(self.cfg.get("printer.cups.lpstat", "lpstat") or "lpstat")) or "lpstat"

    @property
    def timeout(self) -> float:
        return float(self.cfg.get("printer.cups.timeout", 60) or 60)

    def build_command(self, pdf_path: Path, options: list[str], title: str, copies: int) -> list[str]:
        args = [self.lp, "-d", self.queue]
        if title:
            args += ["-t", title[:120]]
        if copies and copies > 1:
            args += ["-n", str(int(copies))]
        for option in options:
            args += ["-o", option]
        args += ["--", str(pdf_path)]
        return args

    def submit(self, pdf_path: Path, options: list[str], title: str, copies: int = 1) -> PrintResult:
        command = self.build_command(pdf_path, options, title, copies)
        printable = " ".join(shlex.quote(part) for part in command)
        result = run(command, timeout=self.timeout)
        if not result.ok:
            raise PrintError(
                f"CUPS 投递失败（rc={result.returncode}）：{result.summary() or '无输出'}"
            )
        match = _REQUEST_ID_RE.search(result.output)
        job_id = match.group(1) if match else ""
        return PrintResult(
            ok=True,
            job_id=job_id,
            message=result.summary(),
            command=printable,
            backend=self.kind,
        )

    def cancel(self, job_id: str) -> bool:
        if not job_id:
            return False
        cancel_bin = which("cancel")
        if not cancel_bin:
            return False
        result = run([cancel_bin, job_id], timeout=30)
        return result.ok

    def state(self) -> QueueState:
        state = QueueState(name=self.queue)
        if not self.queue:
            return state
        printers = run([self.lpstat, "-p", self.queue], timeout=20)
        accepting = run([self.lpstat, "-a", self.queue], timeout=20)
        devices = run([self.lpstat, "-v", self.queue], timeout=20)
        text = " ".join(printers.output.split())
        state.state_text = text
        state.enabled = "disabled" not in text.lower()
        accept_text = accepting.output.lower()
        state.accepting = "not accepting" not in accept_text
        for line in devices.output.splitlines():
            if line.lower().startswith("device for"):
                state.device_uri = line.split(":", 1)[1].strip() if ":" in line else ""
        extended = run([self.lpstat, "-l", "-p", self.queue], timeout=20)
        for line in extended.output.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("description:"):
                state.description = stripped.split(":", 1)[1].strip()
        return state


class VirtualPrinter(BasePrinter):
    """USB/CUPS 不可用时的降级目标：把最终 PDF 落到 spool 目录，可下载/自行打印。"""

    kind = "virtual"

    @property
    def output_dir(self) -> Path:
        directory = self.cfg.spool_dir
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def submit(self, pdf_path: Path, options: list[str], title: str, copies: int = 1) -> PrintResult:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_title = re.sub(r"[^\w\-.]+", "_", title)[:40] or "job"
        target = self.output_dir / f"{stamp}-{safe_title}-{secrets.token_hex(3)}.pdf"
        try:
            shutil.copy2(pdf_path, target)
        except OSError as exc:
            raise PrintError(f"写入虚拟打印目录失败：{exc}") from exc
        manifest = {
            "created_at": utcnow(),
            "title": title,
            "copies": copies,
            "options": options,
            "source": str(pdf_path),
            "output": str(target),
        }
        try:
            target.with_suffix(".json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
        return PrintResult(
            ok=True,
            job_id=f"VIRTUAL-{int(time.time())}",
            message=f"已生成 PDF：{target.name}（虚拟打印机，未连接实物打印机）",
            command=f"# 虚拟打印机 → {target}",
            backend=self.kind,
            output_path=str(target),
        )

    def state(self) -> QueueState:
        return QueueState(
            name="virtual",
            device_uri=str(self.output_dir),
            state_text="虚拟打印机：任务输出为 PDF 文件",
            description="虚拟打印机",
        )


def make_printer(cfg, backend_kind: str, queue: str) -> BasePrinter:
    if backend_kind == "virtual":
        return VirtualPrinter(cfg, queue)
    return CupsPrinter(cfg, queue)


def cups_available(cfg) -> bool:
    lp = which(str(cfg.get("printer.cups.lp", "lp") or "lp"))
    lpstat = which(str(cfg.get("printer.cups.lpstat", "lpstat") or "lpstat"))
    return bool(lp and lpstat)


def scheduler_running(cfg) -> bool:
    lpstat = which(str(cfg.get("printer.cups.lpstat", "lpstat") or "lpstat"))
    if not lpstat:
        return False
    result = run([lpstat, "-r"], timeout=15)
    return "scheduler is running" in result.output.lower()
