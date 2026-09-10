"""异步任务、取消与进度：工具调用跑在循环之外，取消才能叫停它。

一次 `tools/call` 可能阻塞几分钟，所以主循环只负责读输入，工具调用放到别的线程执行；
`notifications/cancelled` 到达时能立刻把这一轮停掉。所有响应都经过 `send()` 写到
stdio，并用一把锁保证不会交错半行 JSON。
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

from core.agy import kill_process_tree
from core.config import PROGRESS_INTERVAL_MS, log


class ActiveTask:
    """一个跑在主循环之外的工具调用，好让取消能传到它身上。"""

    def __init__(self, request_id: Any, progress_token: Any = None) -> None:
        self.request_id = request_id
        self.progress_token = progress_token
        self.last_progress = 0.0
        self.cancelled = threading.Event()
        self.suppress_response = False
        self.worker: Optional[Any] = None
        self.process: Optional[Any] = None

    def bind(self, worker: Optional[Any]) -> None:
        self.worker = worker
        if worker is not None and self.cancelled.is_set():
            worker.stop()  # 这一轮启动途中就被取消了


ACTIVE_TASKS: Dict[Any, ActiveTask] = {}
TASKS_LOCK = threading.Lock()
SEND_LOCK = threading.Lock()
TOOL_QUEUE: "queue.Queue[Tuple[Dict[str, Any], ActiveTask]]" = queue.Queue()
_TASK_LOCAL = threading.local()


def current_task() -> Optional[ActiveTask]:
    """当前线程正在跑的任务（不在工具调用里时为 None）。"""
    return getattr(_TASK_LOCAL, "task", None)


def is_cancelled() -> bool:
    task = current_task()
    return task is not None and task.cancelled.is_set()


def register_task(task: ActiveTask) -> None:
    with TASKS_LOCK:
        ACTIVE_TASKS[task.request_id] = task


def finish_task(request_id: Any) -> None:
    with TASKS_LOCK:
        ACTIVE_TASKS.pop(request_id, None)


def cancel_task(request_id: Any) -> bool:
    """停掉 `request_id` 背后的那一轮：客户端不等了，我们也不该继续跑。"""
    with TASKS_LOCK:
        task = ACTIVE_TASKS.get(request_id)
    if task is None:
        return False
    task.suppress_response = True
    task.cancelled.set()
    log(f"cancel {request_id!r}: stopping the Antigravity turn")
    if task.worker is not None:
        task.worker.stop()
    if task.process is not None:
        kill_process_tree(task.process)
    return True


def send(payload: Dict[str, Any]) -> None:
    """把一条 JSON-RPC 消息写到 stdout（换行分隔的协议）。"""
    # 用 errors="replace"：子进程回传孤立代理字符（它用 surrogateescape 解出的非法字节）时，
    # 绝不能因为写响应就把整个服务器弄崩。
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace")
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def notify_progress(
    progress: float,
    message: str,
    task: Optional[ActiveTask] = None,
    force: bool = False,
) -> None:
    """汇报长轮次的进度（只在客户端要了 progressToken 时才发）。"""
    task = task or current_task()
    if task is None or task.progress_token is None:
        return
    now = time.monotonic()
    if (
        not force
        and PROGRESS_INTERVAL_MS > 0
        and (now - task.last_progress) * 1000 < PROGRESS_INTERVAL_MS
    ):
        return
    task.last_progress = now
    with SEND_LOCK:
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {
                    "progressToken": task.progress_token,
                    "progress": round(float(progress), 3),
                    "message": message,
                },
            }
        )
