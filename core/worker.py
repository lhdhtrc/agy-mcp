"""常驻会话进程、空闲回收、信号处理与孤儿清理。

一个会话对应一个常驻 agy 进程：冷启动约 7 秒，热轮约 1.5 秒。这里管它的生命周期，
以及服务器被强杀后的残局清理（只清理"现在仍然是 agy"的 pid，避免误杀）。
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core import session
from core.agy import CREATE_NO_WINDOW, agy_command_prefix, kill_process_tree, resolve_agy
from core.session import _write_worker_pids, track_worker_pid  # Worker 生命周期要用
from core.jobs import running_job_count  # 已抽出，正常导入
from core.config import (
    WORKER_IDLE_SEC,
    log,
)
from core.protocol import parse_stream_line, progress_from_event, summarize_delta  # noqa: F401

class Worker:
    """一个常驻的 `agy --input-format stream-json` 进程，绑定在一个会话上。"""

    def __init__(self, key: str, argv: List[str], workspace: str) -> None:
        self.key = key
        self.argv = argv
        self.workspace = workspace
        self.proc: Optional[subprocess.Popen] = None
        self.events: "queue.Queue[Tuple[str, Optional[str]]]" = queue.Queue()
        self.stderr_tail: List[str] = []
        self.conversation_id: Optional[str] = None
        self.last_used = time.time()
        self.turns = 0
        self.busy = False
        self.retire = False
        self.step_count = 0
        self.last_step_input = 0

    def start(self) -> None:
        command = agy_command_prefix()
        popen_kwargs: Dict[str, Any] = {}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(
            command + self.argv,
            cwd=self.workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
            **popen_kwargs,
        )
        threading.Thread(target=self._pump, args=(self.proc.stdout, "out"), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, "err"), daemon=True).start()
        track_worker_pid(self.proc.pid, True)

    def _pump(self, stream: Any, tag: str) -> None:
        try:
            for line in stream:
                self.events.put((tag, line))
        except (OSError, ValueError):
            pass
        finally:
            self.events.put((tag + "-eof", None))

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send(
        self,
        prompt: str,
        timeout: float,
        on_progress: Optional[Any] = None,
        on_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if not self.alive() or self.proc is None or self.proc.stdin is None:
            raise RuntimeError("agy stream process is not running")
        self.busy = True
        try:
            return self._send_locked(prompt, timeout, on_progress, on_event)
        finally:
            self.busy = False

    def _send_locked(
        self,
        prompt: str,
        timeout: float,
        on_progress: Optional[Any] = None,
        on_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        assert self.proc is not None and self.proc.stdin is not None

        while not self.events.empty():  # drop anything left over from a previous turn
            try:
                self.events.get_nowait()
            except queue.Empty:
                break

        message = {"event": "user", "message": {"content": prompt}}
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

        deadline = (time.monotonic() + timeout) if timeout else None
        steps = 0
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no result within {int(timeout)}s")
            else:
                remaining = 5.0
            try:
                tag, line = self.events.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                if not self.alive():
                    tail = self.stderr_tail[-1] if self.stderr_tail else "no stderr output"
                    raise RuntimeError(f"agy stream process exited: {tail}")
                continue
            if tag == "err":
                if line:
                    self.stderr_tail.append(line.strip())
                    del self.stderr_tail[:-20]
                continue
            if tag.endswith("-eof") or not line:
                continue
            event = parse_stream_line(line)
            if event is None:
                continue
            if on_event is not None:
                try:
                    on_event(event)
                except Exception as exc:  # noqa: BLE001 - diagnostics must not break a turn
                    log(f"event callback failed: {exc!r}")
            if event.get("conversation_id"):
                self.conversation_id = str(event["conversation_id"])
            if event.get("event") == "result":
                result = event.get("result")
                if isinstance(result, dict):
                    if result.get("conversation_id"):
                        self.conversation_id = str(result["conversation_id"])
                    self.last_used = time.time()
                    self.turns += 1
                    return result
            elif on_progress is not None and event.get("event") != "init":
                update = event.get("step_update")
                if isinstance(update, dict):
                    self.step_count += 1
                    step_usage = update.get("usage")
                    if isinstance(step_usage, dict) and isinstance(
                        step_usage.get("input_tokens"), (int, float)
                    ):
                        # 最后一步的输入才是真实上下文大小；整轮的合计是所有步骤之和，
                        # 有工具调用时会远大于上下文。
                        self.last_step_input = int(step_usage["input_tokens"])
                steps += 1
                parsed = progress_from_event(event)
                label, detail = parsed if parsed else ("step", None)
                try:
                    on_progress(steps, label, detail)
                except Exception as exc:  # noqa: BLE001 - progress must never break a turn
                    log(f"progress callback failed: {exc!r}")

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        track_worker_pid(proc.pid, False)
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            kill_process_tree(proc)


WORKERS: Dict[str, Worker] = {}


def reap_workers() -> None:
    now = time.time()
    for key in list(WORKERS):
        worker = WORKERS[key]
        if worker.busy:
            continue  # a long turn must not be reaped from under itself
        if worker.retire:
            # 因为会话切换了模型/强度而退休的进程：只在轮次之间停掉。
            worker.stop()
            WORKERS.pop(key, None)
            continue
        idle = WORKER_IDLE_SEC > 0 and now - worker.last_used > WORKER_IDLE_SEC
        if not worker.alive() or idle:
            worker.stop()
            WORKERS.pop(key, None)


def shutdown_workers() -> None:
    for worker in list(WORKERS.values()):
        worker.stop()
    WORKERS.clear()
    _write_worker_pids([])






def reap_orphan_workers() -> None:
    """被 SIGKILL / 任务管理器强杀的服务器来不及清理自己的会话进程，这里替它收拾。"""
    # 只要可能还有长作业在跑就不要清理：否则会把它的进程一起杀掉。
    pending = running_job_count()
    if pending:
        log(f"skipping orphan cleanup: {pending} job(s) still marked running")
        return
    pids = session._read_worker_pids()
    if not pids:
        return
    for pid in pids:
        if not session._is_agy_process(pid):
            continue
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW, timeout=15,
                )
            else:
                os.kill(pid, signal.SIGTERM)
            log(f"reaped orphaned session process {pid}")
        except (OSError, subprocess.SubprocessError):
            pass
    _write_worker_pids([])


def _reaper_loop(interval: float = 60.0) -> None:
    """即使没有新调用进来，也要定期回收空闲的会话进程。"""
    while True:
        time.sleep(interval)
        try:
            reap_workers()
        except Exception as exc:  # noqa: BLE001 - a reaper must never kill the server
            log(f"reaper error: {exc!r}")


def _install_signal_handlers() -> None:
    """让 SIGTERM/SIGINT 也把子进程带走（只靠 atexit 不够）。"""

    def handler(signum: int, _frame: Any) -> None:
        log(f"signal {signum}: stopping session processes")
        shutdown_workers()
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (AttributeError, OSError, ValueError):
            pass

atexit.register(shutdown_workers)
