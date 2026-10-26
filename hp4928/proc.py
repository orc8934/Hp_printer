"""子进程调用的统一封装：固定 locale、超时、可读的错误。"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

# 固定 LC_ALL=C 才能稳定解析 lp/lpstat 的英文输出
_BASE_ENV = {
    "LC_ALL": "C",
    "LANG": "C",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


@dataclass
class RunResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def output(self) -> str:
        return (self.stdout or "") + (self.stderr or "")

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def summary(self, limit: int = 400) -> str:
        text = " ".join((self.output or "").split())
        return text[:limit] + ("…" if len(text) > limit else "")


def which(program: str) -> str | None:
    """查找可执行文件（含 /usr/sbin 这类 CUPS 常用目录）。"""
    if not program:
        return None
    if os.path.isabs(program):
        return program if os.path.isfile(program) and os.access(program, os.X_OK) else None
    found = shutil.which(program)
    if found:
        return found
    for directory in ("/usr/sbin", "/usr/bin", "/sbin", "/bin", "/usr/local/sbin"):
        candidate = os.path.join(directory, program)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def run(args: list[str], timeout: float = 30, extra_env: dict[str, str] | None = None) -> RunResult:
    """执行命令并捕获输出；不抛异常（除参数错误）。"""
    env = dict(os.environ)
    env.update(_BASE_ENV)
    if extra_env:
        env.update(extra_env)
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=max(1.0, float(timeout)),
            env=env,
        )
    except FileNotFoundError:
        return RunResult(" ".join(args), 127, "", f"找不到命令：{args[0]}")
    except PermissionError:
        return RunResult(" ".join(args), 126, "", f"没有执行权限：{args[0]}")
    except subprocess.TimeoutExpired:
        return RunResult(" ".join(args), 124, "", f"命令超时（{timeout:g} 秒）：{' '.join(args)}", True)
    except OSError as exc:
        return RunResult(" ".join(args), 125, "", f"命令执行失败：{exc}")
    return RunResult(
        " ".join(args),
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
    )
