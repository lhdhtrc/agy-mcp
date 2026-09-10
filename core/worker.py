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
from core.agy import CREATE_NO_WINDOW, agy_command_prefix, kill_process_tree
from core.session import track_worker_pid  # Worker 生命周期要用
from core.jobs import running_job_count  # 已抽出，正常导入
from core.config import (
    WORKER_IDLE_SEC,
    log,
)
from core.protocol import parse_stream_line, progress_from_event


def _option_value(argv: List[str], flag: str) -> Optional[str]:
    """取 `--flag value` 里的值（没这个开关就返回 None）。"""
    if flag in argv:
        index = argv.index(flag) + 1
        if index < len(argv):
            return str(argv[index])
    return None

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
        # 起这个进程时用的"会话位置"：用来判断它能不能直接承接这一轮
        # （预热的空进程没有 --conversation，用它续接就会把历史丢掉）
        self.started_conversation = _option_value(argv, "--conversation")
        self.started_continue_recent = "-c" in argv

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
# WORKERS 会被主循环、工具线程、预热线程与回收线程同时碰，字典操作要互斥
WORKERS_LOCK = threading.Lock()


def start_registered_worker(key: str, argv: List[str], workspace: str) -> Tuple[Worker, bool]:
    """起一个会话进程并登记；同名进程已经在跑时丢掉新起的那个。

    返回（实际使用的 worker, 是否由本函数新起）。预热线程与第一次真实调用会抢同一个 key，
    不这样做就会出现两个进程、而且注册表只认后写的那个，先起的直接失联（既不回收也不停）。
    """
    worker = Worker(key, argv, workspace)
    worker.start()
    with WORKERS_LOCK:
        existing = WORKERS.get(key)
        if existing is not None and existing.alive():
            worker.stop()
            return existing, False
        WORKERS[key] = worker
    return worker, True


def reap_workers() -> None:
    now = time.time()
    doomed: List[Worker] = []
    with WORKERS_LOCK:
        for key in list(WORKERS):
            worker = WORKERS[key]
            if worker.busy:
                continue  # a long turn must not be reaped from under itself
            if worker.retire:
                # 因为会话切换了模型/强度而退休的进程：只在轮次之间停掉。
                doomed.append(WORKERS.pop(key))
                continue
            idle = WORKER_IDLE_SEC > 0 and now - worker.last_used > WORKER_IDLE_SEC
            if not worker.alive() or idle:
                doomed.append(WORKERS.pop(key))
    for worker in doomed:
        worker.stop()


def shutdown_workers() -> None:
    with WORKERS_LOCK:
        doomed = list(WORKERS.values())
        WORKERS.clear()
    for worker in doomed:
        worker.stop()
    # 只注销本实例自己的记录：别的 Codex 线程的会话进程还在跑
    session.clear_worker_pids()






def reap_orphan_workers() -> None:
    """被 SIGKILL / 任务管理器强杀的服务器来不及清理自己的会话进程，这里替它收拾。

    **只清理服务器进程已经不在的实例**：workers.json 是所有 MCP 实例共用的，
    另一个 Codex 线程的服务器仍然活着时，它的会话进程是"在用"而不是"孤儿"，
    杀掉它会让那一轮当场失败。
    """
    # 只要可能还有长作业在跑就不要清理：否则会把它的进程一起杀掉。
    pending = running_job_count()
    if pending:
        log(f"skipping orphan cleanup: {pending} job(s) still marked running")
        return
    records = session._read_worker_records()
    if not records:
        return
    live = session.live_instance_ids()
    for instance_id, record in records.items():
        if instance_id in live:
            continue
        for pid in record.get("pids") or []:
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

    def drop_dead_instances(current: Dict[str, Any]) -> None:
        # 在锁里重新判一次活跃度：清理期间刚启动的实例不能被误删记录
        live_now = session.live_instance_ids()
        for instance_id in list(current):
            if instance_id not in live_now:
                current.pop(instance_id, None)

    session._mutate_worker_records(drop_dead_instances)


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
