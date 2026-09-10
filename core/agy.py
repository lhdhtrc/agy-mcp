"""Antigravity CLI 的定位与调用：本模块只负责"怎么把 agy 跑起来"。

这里不含任何业务状态——会话、额度、作业都在别的模块里。这样分层之后，
想换命令前缀（包装脚本、容器、测试替身）只需要看这一个文件。
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
from typing import Any, Dict, List, Optional, Tuple

# Windows 下不弹控制台窗口；其他平台为 0
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def resolve_agy() -> str:
    """定位 agy 可执行文件：环境变量优先，其次 PATH，最后常见安装目录。"""
    override = os.environ.get("AGY_BIN")
    if override and os.path.exists(override):
        return override

    found = shutil.which("agy") or shutil.which("agy.exe")
    if found:
        return found

    exe = "agy.exe" if os.name == "nt" else "agy"
    candidates = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "agy", "bin", exe))
    candidates.append(os.path.join(os.path.expanduser("~"), ".local", "bin", exe))
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    raise FileNotFoundError(
        "没找到 agy 可执行文件；请安装 Antigravity CLI，或设置 AGY_BIN 指向它"
    )


def agy_command_prefix() -> List[str]:
    """调用 CLI 的命令前缀。

    `AGY_MCP_AGY_CMD` 会整体替换它（按空格切分、不经 shell），
    适合包装脚本、容器与测试替身，例如 `AGY_MCP_AGY_CMD="wsl agy"`。
    """
    override = os.environ.get("AGY_MCP_AGY_CMD")
    if override and override.strip():
        return shlex.split(override)
    return [resolve_agy()]


def kill_process_tree(proc: subprocess.Popen) -> None:
    """agy 会拉起一批辅助进程；停止时要保证一个都不留。"""
    try:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def run_agy(
    argv: List[str],
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Tuple[int, str, str]:
    """跑一次 agy，超时时连同它的整个进程组一起清理。"""
    command = agy_command_prefix()
    popen_kwargs: Dict[str, Any] = {}
    if os.name != "nt":
        # 建独立进程组，超时被杀时能连带清理它拉起的子进程。
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        command + argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )
    # 把这个进程挂到当前调用上，客户端取消时才能杀掉它。
    # current_task 属于协议层，这里延迟导入以避免循环依赖（等协议层拆分后收敛）。
    from core.impl import current_task

    task = current_task()
    if task is not None:
        task.process = proc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        raise
    finally:
        if task is not None:
            task.process = None
    return proc.returncode, out or "", err or ""


def _git(args: List[str], cwd: str, timeout: float = 30) -> Tuple[int, str]:
    """在指定目录跑一条 git 命令，返回 (退出码, 标准输出+标准错误)。"""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def pid_alive(pid: int) -> bool:
    """判断某个 pid 是否还活着（用于脱离进程的长作业状态判断）。"""
    if not pid:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
