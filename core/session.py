"""会话相关的落盘状态：遗留进程 pid 记录。

服务器被强杀（SIGKILL / 任务管理器）时来不及清理，所以把会话进程的 pid 记在
`workers.json` 里，下次启动据此收拾残局——但动手前必须确认那个 pid 现在**仍然是 agy**，
避免 pid 复用误杀别的程序。
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import List, Optional

from core.agy import CREATE_NO_WINDOW
from core.config import STATE_DIR

WORKER_PID_FILE = os.path.join(STATE_DIR, "workers.json")


def _read_worker_pids() -> List[int]:
    """读出上次记录的会话进程 pid（文件缺失或损坏时返回空列表）。"""
    try:
        with open(WORKER_PID_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return [int(pid) for pid in data if isinstance(pid, int)]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _write_worker_pids(pids: List[int]) -> None:
    """把会话进程 pid 写回磁盘（去重排序；失败只忽略，不影响主流程）。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(WORKER_PID_FILE, "w", encoding="utf-8") as handle:
            json.dump(sorted(set(pids)), handle)
    except OSError:
        pass


def track_worker_pid(pid: Optional[int], add: bool) -> None:
    """记录/移除一个会话进程 pid，供"被强杀后下次启动清理"使用。"""
    if not pid:
        return
    pids = _read_worker_pids()
    if add:
        pids.append(pid)
    elif pid in pids:
        pids.remove(pid)
    _write_worker_pids(pids)


def _is_agy_process(pid: int) -> bool:
    """防止 pid 复用误杀：只有该 pid 现在看起来仍是 CLI 才动手。"""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True, text=True, timeout=15,
            ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "agy" in out.lower()
