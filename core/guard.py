"""调用护栏与用量统计：每日上限、最小间隔、跨进程文件锁、轮次串行化。

CLI 是官方客户端，但额度本是给人驱动 agent 用的。串行化调用、限制每日总量，是为了让流量
形状保持"正常"——这也是包装层在账号风险上唯一能做的事。

状态落在 `~/.agy-mcp/state.json`（当日调用数与 token 累计）与 `~/.agy-mcp/usage.jsonl`
（每轮一条明细，超限时丢掉旧的半截）。跨进程锁用 `call.lock` / `call-<hash>.lock`，
所以并行的多个客户端会排队而不是抢同一份额度。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, Optional

from core.config import (
    MAX_PARALLEL,
    STATE_DIR,
    USAGE_ROTATE_BYTES,
    log,
)


# 只有一份 state.json：跨进程串行化用的锁与状态文件都放在这里
LOCK_PATH = os.path.join(STATE_DIR, "call.lock")
STATE_LOCK_PATH = os.path.join(STATE_DIR, "state.lock")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
USAGE_PATH = os.path.join(STATE_DIR, "usage.jsonl")
# 每写够这么多条明细就检查一次是否该轮转，避免每次都去 stat 文件
_USAGE_SINCE_ROTATE = 0
LOCK_WAIT_SEC = 600


class FileLock:
    """跨进程的咨询锁：并行的客户端排队，而不是抢同一份额度。"""

    def __init__(self, path: str, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self.handle = None

    def __enter__(self) -> "FileLock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.handle = open(self.path, "a+b")
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("another Antigravity call is still running")
                time.sleep(0.5)

    def __exit__(self, *exc_info: Any) -> None:
        try:
            if self.handle is not None:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            if self.handle is not None:
                self.handle.close()
                self.handle = None


def _today() -> str:
    return time.strftime("%Y-%m-%d")


@contextlib.contextmanager
def state_guard() -> Any:
    """state.json 的读-改-写也要串行。

    默认单飞模式下 turn_guard 已经覆盖到了，但 `AGY_MCP_MAX_PARALLEL > 1` 时锁降到
    会话粒度，两个会话同时收尾就会互相覆盖当日计数与 token 累计。
    """
    with FileLock(STATE_LOCK_PATH, LOCK_WAIT_SEC):
        yield


def read_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            return {}
        return state
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
    except OSError as exc:
        log(f"could not persist guard state: {exc}")


def calls_today(state: Dict[str, Any]) -> int:
    if state.get("day") != _today():
        return 0
    try:
        return int(state.get("calls", 0))
    except (TypeError, ValueError):
        return 0


def log_usage(entry: Dict[str, Any]) -> None:
    global _USAGE_SINCE_ROTATE
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(USAGE_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        log(f"could not append usage log: {exc}")
        return
    _USAGE_SINCE_ROTATE += 1
    if _USAGE_SINCE_ROTATE >= 25:
        _USAGE_SINCE_ROTATE = 0
        rotate_usage_log()


def rotate_usage_log() -> None:
    """用量日志不能无限增长：超过上限就丢掉旧的那一半。"""
    try:
        if not USAGE_ROTATE_BYTES or os.path.getsize(USAGE_PATH) < USAGE_ROTATE_BYTES:
            return
        with open(USAGE_PATH, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        keep = lines[len(lines) // 2 :]
        with open(USAGE_PATH, "w", encoding="utf-8") as handle:
            handle.writelines(keep)
        log(f"rotated usage log: kept {len(keep)} of {len(lines)} lines")
    except OSError as exc:
        log(f"could not rotate usage log: {exc}")


def usage_stats(limit: int = 200) -> Dict[str, Any]:
    """最近若干条用量记录里轮次耗时的 p50 / p95。"""
    try:
        with open(USAGE_PATH, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
    except OSError:
        return {}
    durations = []
    for line in lines:
        try:
            value = json.loads(line).get("duration_ms")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(value, (int, float)):
            durations.append(float(value))
    if not durations:
        return {}
    durations.sort()
    pick = lambda q: durations[min(len(durations) - 1, int(q * len(durations)))]  # noqa: E731
    return {
        "samples": len(durations),
        "p50_ms": int(pick(0.5)),
        "p95_ms": int(pick(0.95)),
        "max_ms": int(durations[-1]),
    }


def merge_usage(target: Dict[str, Any], payload: Optional[Dict[str, Any]]) -> None:
    """把一轮的 token 用量累加进总数（交接会跑两轮，所以要累加）。"""
    if not payload:
        return
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            target[key] = int(target.get(key, 0) or 0) + int(value)


# 默认全局只允许一轮同时跑；AGY_MCP_MAX_PARALLEL > 1 时改成按会话并发，用信号量兜住总量。
PARALLEL_SLOTS = threading.BoundedSemaphore(MAX_PARALLEL) if MAX_PARALLEL > 1 else None


def session_lock_path(session_name: str, workspace: str) -> str:
    digest = hashlib.sha1(f"{session_name}|{workspace}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(STATE_DIR, f"call-{digest}.lock")


@contextlib.contextmanager
def turn_guard(session_name: str, workspace: str) -> Any:
    """让 Antigravity 的轮次串行执行。

    默认：任何进程之间一次只有一轮（对账号更保险）。
    设了 AGY_MCP_MAX_PARALLEL > 1 时锁按会话粒度生效，两个不同的会话可以并行，
    总量由同数量的槽位兜住。
    """
    if PARALLEL_SLOTS is None:
        with FileLock(LOCK_PATH, LOCK_WAIT_SEC):
            yield
        return
    if not PARALLEL_SLOTS.acquire(timeout=LOCK_WAIT_SEC):
        raise TimeoutError("no free Antigravity slot")
    try:
        with FileLock(session_lock_path(session_name, workspace), LOCK_WAIT_SEC):
            yield
    finally:
        PARALLEL_SLOTS.release()
