#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 agy-mcp contributors
"""agy-mcp：把 Antigravity CLI（agy）封装成 MCP 服务器，供 Codex 等 MCP 客户端调用。

传输方式：stdio 上的 MCP，换行分隔的 JSON-RPC 2.0（不使用 Content-Length 分帧）。
只依赖 Python 标准库，任何 MCP 客户端都能直接拉起本文件，无需安装依赖。

登录状态由 CLI 自己保管，本服务器从不接触凭据。
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

SERVER_NAME = "antigravity"
SERVER_VERSION = "0.1.8"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2024-11-05"
# 进程控制（定位 CLI、跑命令、杀进程树）已抽到 core/agy.py
from core.agy import (
    CREATE_NO_WINDOW,
    _git,
    agy_command_prefix,
    kill_process_tree,
    pid_alive,
    resolve_agy,
    run_agy,
)
# 0 表示不限时：真实的 agent 作业可能跑很久，而 CLI 自带的 print 超时默认只有 5 分钟，
# 正是它把长任务掐断的。
DEFAULT_TIMEOUT_SEC = int(os.environ.get("AGY_MCP_DEFAULT_TIMEOUT_SEC") or 0)
TIMEOUT_GRACE_SEC = 30
# 元数据类调用（models / quota / version）仍需短超时，否则 status 之类可能一直挂着。
UNLIMITED_PRINT_TIMEOUT = "24h"
# 路径与通用工具已抽到 core/config.py（唯一读环境变量的地方）
from core.config import (
    METADATA_TIMEOUT_SEC,  # noqa: E402
    MIN_INTERVAL_SEC,
    MAX_CALLS_PER_DAY,
    WORKER_IDLE_SEC,
    LONG_CONTEXT_TOKENS,
    SHUTDOWN_GRACE_SEC,
    AUTO_HANDOFF,
    USAGE_ROTATE_BYTES,
    PROGRESS_INTERVAL_MS,
    MAX_PROMPT_CHARS,
    MAX_DIFF_CHARS,
    MAX_PARALLEL,
    PREWARM,
    MODEL_PREFERENCE,
    QUOTA_WARN_PERCENT,
    QUOTA_REFRESH_SEC,
    DEFAULT_MIN_INTERVAL_SEC,
    DEFAULT_MAX_CALLS_PER_DAY,
    DEFAULT_SESSION,
    DEFAULT_WORKER_IDLE_SEC,
    DEFAULT_LONG_CONTEXT_TOKENS,
    DEFAULT_SHUTDOWN_GRACE_SEC,
    DEFAULT_USAGE_ROTATE_MB,
    DEFAULT_PROGRESS_INTERVAL_MS,
    DEFAULT_MAX_PROMPT_CHARS,
    DEFAULT_MAX_DIFF_CHARS,
    DEFAULT_MODEL,
    DEFAULT_MODEL_PREFERENCE,
    DEFAULT_QUOTA_WARN_PERCENT,
    DEFAULT_QUOTA_REFRESH_SEC,
    DEFAULT_MODEL_ID,
    AGY_CLI_HOME,
    SESSIONS_PATH,
    STATE_DIR,
    _env_float,
    _env_int,
    log,
)
# 协议层纯函数（已搬 text_result / join_streams / parse_json_output / 进度与流解析，剩余逐个搬）
from core.protocol import (  # noqa: E402,F401
    join_streams,
    parse_json_output,
    parse_stream_line,
    progress_from_event,
    summarize_delta,
    text_result,
)
# 额度解析、后台刷新、配额告警与 model=auto 选型已抽到 core/quota.py
from core import quota as quota  # noqa: E402
from core.quota import *  # noqa: E402,F401,F403 —— 名字多且会被测试补丁，集中导入
# 提示词拼装（files / diff / no_web / 交接摘要）已抽到 core/prompts.py
from core.prompts import (  # noqa: E402,F401
    HANDOFF_PROMPT,
    attach_diff,
    attach_files,
    attach_no_web,
    handoff_prompt,
    seed_prompt,
)
# 后台作业（落盘、脱离进程、结果回收）已抽到 core/jobs.py
from core.jobs import (  # noqa: E402,F401
    DETACHED_PROCESS,
    JOBS,
    JOBS_DIR,
    JOBS_LOCK,
    _job_path,
    collect_detached_job,
    list_jobs,
    read_job,
    running_job_count,
    write_job,
)
# 常驻会话进程与回收/信号处理已抽到 core/worker.py
from core.worker import (  # noqa: E402,F401
    WORKERS,
    Worker,
    _install_signal_handlers,
    _reaper_loop,
    reap_orphan_workers,
    reap_workers,
    shutdown_workers,
)
# 会话进程 pid 的落盘与清理已抽到 core/session.py；
# 会被测试猴补丁的 _is_agy_process / _read_worker_pids 必须走模块对象调用
from core import session  # noqa: E402
# 会话表本体已抽到 core/session.py（测试仍按顶层名调用，故按名导入）
from core.session import (  # noqa: E402,F401
    _read_worker_pids,
    _write_worker_pids,
    read_last_conversations,
    read_sessions,
    remember_partial_turn,
    newest_conversation_since,
    track_worker_pid,
    write_sessions,
)

# 护栏：CLI 是官方客户端，但额度本是给人驱动 agent 用的。串行化调用、限制每日总量，
# 是为了让流量形状保持"正常"——这也是包装层在账号风险上唯一能做的事。
LOCK_PATH = os.path.join(STATE_DIR, "call.lock")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
USAGE_PATH = os.path.join(STATE_DIR, "usage.jsonl")
_USAGE_SINCE_ROTATE = 0
# Antigravity CLI 自己的状态（会话 id、workspace 索引）放在这里。
LOCK_WAIT_SEC = 600
ANSWER_KEYS = ("response", "result", "text", "output", "content", "message", "answer")
# 每次新起 `agy -p` 进程都要重做鉴权与模型/额度初始化（约 5 秒）；常驻的
# `--input-format stream-json` 进程服务一个会话，热轮约 1.5 秒。
# 一个 Codex 会话对应一个 MCP 服务器实例，因此会话表按实例隔离，两个会话不会抢同一个
# Antigravity 会话；新实例会沿用上一个实例的映射，除非检测到另一个实例仍活跃。
class FileLock:
    """Cross-process advisory lock so parallel clients queue instead of racing."""

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
    """Keep the usage log from growing without bound: drop the older half past the limit."""
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
    """p50/p95 turn duration over the most recent usage entries."""
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


def extract_answer(node: Any, depth: int = 0) -> Optional[str]:
    """Pull the answer out of the CLI result without grabbing unrelated metadata.

    The result payload is `{conversation_id, status, response, ...}`; never fall back to
    "any string anywhere", or a metadata id gets returned as the answer.
    """
    if depth > 4 or node is None:
        return None
    if isinstance(node, str):
        return node.strip() or None
    if isinstance(node, dict):
        for key in ANSWER_KEYS:
            if key in node:
                found = extract_answer(node[key], depth + 1)
                if found:
                    return found
        return None
    if isinstance(node, list):
        parts = [extract_answer(item, depth + 1) for item in node]
        joined = "\n".join(part for part in parts if part)
        return joined or None
    return None


def merge_usage(target: Dict[str, Any], payload: Optional[Dict[str, Any]]) -> None:
    """Fold a turn's token usage into a running total (handoff runs two turns)."""
    if not payload:
        return
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            target[key] = int(target.get(key, 0) or 0) + int(value)


def collect_diff(cwd: str, base: Optional[str], limit: int) -> Tuple[str, str]:
    """Capture the working tree diff locally.

    Letting the agent run `git diff` itself proved unreliable under the sandbox
    (a plain request did not finish in minutes), so read it here and pass it as text.
    """
    code, out = _git(["rev-parse", "--is-inside-work-tree"], cwd)
    if code != 0 or "true" not in out.lower():
        return "", "not a git working tree, so no diff was attached"

    target = base or "HEAD"
    code, out = _git(["diff", target], cwd)
    if code != 0:
        # 还没有提交（或 ref 不存在）：退回到"未暂存 + 已暂存"。
        _, unstaged = _git(["diff"], cwd)
        _, staged = _git(["diff", "--cached"], cwd)
        out = unstaged + staged
        target = "the index"

    _, status = _git(["status", "--short"], cwd)
    parts = []
    if status.strip():
        parts.append("Changed paths (git status --short):\n" + status.strip())
    if out.strip():
        diff_text = out
        if limit and len(diff_text) > limit:
            diff_text = diff_text[:limit] + f"\n…(diff truncated; {len(out)} chars total)"
        parts.append(f"Diff (`git diff {target}`):\n{diff_text.strip()}")
    if not parts:
        return "", "no uncommitted changes found, so no diff was attached"
    return "\n\n".join(parts), ""


def mask_proxy(value: str) -> str:
    """Hide credentials in a proxy URL before showing it back to the model."""
    if "@" in value:
        scheme, _, rest = value.partition("://")
        return f"{scheme}://***@{rest.rpartition('@')[2]}" if scheme else f"***@{value.rpartition('@')[2]}"
    return value


def proxy_env_report() -> Dict[str, Any]:
    report: Dict[str, Any] = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        value = os.environ.get(key)
        if value:
            report[key] = mask_proxy(value) if "PROXY" in key.upper() and "NO_PROXY" not in key.upper() else value
    return report




class ActiveTask:
    """A tool call running off the main loop, so cancellations can reach it."""

    def __init__(self, request_id: Any, progress_token: Any = None) -> None:
        self.request_id = request_id
        self.progress_token = progress_token
        self.last_progress = 0.0
        self.cancelled = threading.Event()
        self.suppress_response = False
        self.worker: Optional["Worker"] = None
        self.process: Optional[subprocess.Popen] = None

    def bind(self, worker: Optional["Worker"]) -> None:
        self.worker = worker
        if worker is not None and self.cancelled.is_set():
            worker.stop()  # cancelled while this call was still starting up


ACTIVE_TASKS: Dict[Any, ActiveTask] = {}
TASKS_LOCK = threading.Lock()
SEND_LOCK = threading.Lock()
TOOL_QUEUE: "queue.Queue[Tuple[Dict[str, Any], ActiveTask]]" = queue.Queue()
_TASK_LOCAL = threading.local()


def current_task() -> Optional[ActiveTask]:
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
    """Stop the turn behind `request_id`: the client no longer waits, so neither should we."""
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




def notify_progress(
    progress: float,
    message: str,
    task: Optional[ActiveTask] = None,
    force: bool = False,
) -> None:
    """Tell the client how a long turn is going (only if it asked for progress)."""
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


def session_flags(args: Dict[str, Any], conversation: Optional[str], continue_recent: bool) -> List[str]:
    """Flags that must hold for the life of a session process."""
    flags: List[str] = []
    if args.get("json_schema"):
        flags += ["--json-schema", str(args["json_schema"])]
    if args.get("model"):
        flags += ["--model", str(args["model"])]
    if args.get("effort"):
        flags += ["--effort", str(args["effort"])]
    if args.get("agent"):
        flags += ["--agent", str(args["agent"])]
    if args.get("mode"):
        flags += ["--mode", str(args["mode"])]
    if conversation:
        flags += ["--conversation", conversation]
    elif continue_recent:
        flags.append("-c")
    if args.get("sandbox", True):
        flags.append("--sandbox")
    if args.get("skip_permissions", True):
        flags.append("--dangerously-skip-permissions")
    if args.get("disable_slash_commands", True):
        flags.append("--disable-slash-commands")
    extra = args.get("extra_args")
    if isinstance(extra, list):
        flags += [str(item) for item in extra]
    return flags


PARALLEL_SLOTS = threading.BoundedSemaphore(MAX_PARALLEL) if MAX_PARALLEL > 1 else None
FAST_TOOLS = frozenset(
    {
        "antigravity_status",
        "antigravity_models",
        "antigravity_agents",
        "antigravity_quota",
        "antigravity_sessions",
        "antigravity_submit",
        "antigravity_job",
    }
)


def session_lock_path(session_name: str, workspace: str) -> str:
    digest = hashlib.sha1(f"{session_name}|{workspace}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(STATE_DIR, f"call-{digest}.lock")


@contextlib.contextmanager
def turn_guard(session_name: str, workspace: str) -> Any:
    """Serialize Antigravity turns.

    Default: one turn at a time across every process (extra safety for the account).
    With AGY_MCP_MAX_PARALLEL > 1 the lock becomes per session, so two different
    conversations may run side by side, bounded by that many slots.
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


def prewarm_default_session() -> None:
    """Start the default session process early so the first real call is already warm."""
    try:
        workspace = os.path.abspath(os.getcwd())
        base_flags = session_flags(
            {"sandbox": True, "skip_permissions": True, "disable_slash_commands": True}, None, False
        )
        key = f"{DEFAULT_SESSION}|{workspace}|{' '.join(base_flags)}"
        if key in WORKERS:
            return
        worker = Worker(
            key,
            ["--input-format", "stream-json", "--output-format", "stream-json"] + base_flags,
            workspace,
        )
        worker.start()
        WORKERS[key] = worker
        log(f"prewarmed the default session process for {workspace}")
    except Exception as exc:  # noqa: BLE001 - prewarming is best effort
        log(f"prewarm failed: {exc!r}")


ASK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "发送给 Antigravity CLI 的提示词（非交互 print 模式）。",
        },
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Paths the Antigravity agent should read itself before answering, instead of "
                "pasting file contents into the prompt."
            ),
        },
        "diff": {
            "type": "boolean",
            "default": False,
            "description": (
                "Attach the working tree diff (captured locally with git) as context; "
                "handy for 'review my changes' without the agent having to run git itself."
            ),
        },
        "diff_base": {
            "type": "string",
            "description": "配合 diff 指定对比的 git ref（默认 HEAD）。",
        },
        "no_web": {
            "type": "boolean",
            "default": False,
            "description": (
                "Tell the agent not to browse or use browser tools. Use it for analysis tasks: "
                "browsing costs many steps and the CLI's browser driver is often unavailable."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Antigravity model id (see antigravity_models), or 'auto' to pick one whose "
                f"quota group still has room. Default: {DEFAULT_MODEL}."
            ),
        },
        "effort": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "本会话的思考强度。",
        },
        "cwd": {
            "type": "string",
            "description": "Antigravity 会话的工作目录（即它的 workspace）。",
        },
        "session": {
            "type": "string",
            "default": DEFAULT_SESSION,
            "description": (
                "Named Antigravity conversation to keep continuity across calls. Calls with the "
                "same session name resume the same conversation; use new_session to restart it."
            ),
        },
        "new_session": {
            "type": "boolean",
            "default": False,
            "description": "为该会话重开一个会话，而不是续接已记录的那个。",
        },
        "handoff": {
            "type": "boolean",
            "default": False,
            "description": (
                "Compact the current conversation into a handoff digest, start a NEW conversation and "
                "answer there with that digest as background. Use when the conversation has grown long."
            ),
        },
        "agent": {"type": "string", "description": "可选的 Antigravity agent 名称。"},
        "mode": {
            "type": "string",
            "enum": ["plan", "accept-edits"],
            "description": "Antigravity 执行模式；省略则用 CLI 默认。",
        },
        "sandbox": {
            "type": "boolean",
            "default": True,
            "description": "以终端受限方式运行会话（默认 true）。",
        },
        "skip_permissions": {
            "type": "boolean",
            "default": True,
            "description": (
                "Auto-approve Antigravity tool permissions (default true). Headless runs cannot "
                "prompt for approval, so without this the agent cannot even read a file; the "
                "terminal sandbox stays on unless you disable it."
            ),
        },
        "continue_session": {
            "type": "boolean",
            "default": False,
            "description": "续接最近一次 Antigravity 会话。",
        },
        "conversation": {
            "type": "string",
            "description": "指定要续接的会话 id（覆盖本会话记录的那个）。",
        },
        "output_format": {
            "type": "string",
            "enum": ["text", "json"],
            "default": "text",
            "description": "CLI print 模式的输出格式，原样返回。",
        },
        "json_schema": {
            "type": "string",
            "description": (
                "Optional JSON schema (inline string or path) passed to the CLI with --json-schema, "
                "so the Antigravity answer is structured. Implies a per-schema session process."
            ),
        },
        "disable_slash_commands": {
            "type": "boolean",
            "default": True,
            "description": "不展开提示词里的斜杠命令与技能（默认 true）。",
        },
        "timeout_sec": {
            "type": "number",
            "default": DEFAULT_TIMEOUT_SEC,
            "description": "等待 Antigravity CLI 的秒数上限。",
        },
        "extra_args": {
            "type": "array",
            "items": {"type": "string"},
            "description": "追加的 agy 原始参数，原样拼接。",
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "antigravity_ask",
        "description": (
            "通过本机 Antigravity CLI（agy）跑一轮非交互提问并返回回答；"
            "默认续接同一会话。用的是 agy 已登录的 Google 账号额度（Antigravity / Gemini），"
            "不占用当前供应商的额度。"
        ),
        "inputSchema": ASK_SCHEMA,
    },
    {
        "name": "antigravity_models",
        "description": "列出当前 agy 账号可用的 Antigravity 模型。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "antigravity_agents",
        "description": "列出当前 agy 账号可用的 agent。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "antigravity_sessions",
        "description": (
            "查看或遗忘本服务器跟踪的 Antigravity 会话，"
            "并列出 CLI 本地已有的会话。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "forget"],
                    "default": "list",
                    "description": "list 列出跟踪的会话；forget 遗忘指定会话，传 '*' 清空全部。",
                },
                "session": {
                    "type": "string",
                    "description": "action=forget 时的会话名；'*' 表示清空全部跟踪的会话。",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "antigravity_quota",
        "description": (
            "查看 Antigravity 剩余额度（按模型组的周窗口与 5 小时窗口）。"
            "由 CLI 自身回答：不起 turn、不扣额度。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "antigravity_submit",
        "description": (
            "在后台启动一轮 Antigravity 作业并立刻返回 job id。"
            "长作业（深度分析、长文档）用它避免阻塞；之后用 antigravity_job 取结果。"
            "参数与 antigravity_ask 相同。"
        ),
        "inputSchema": ASK_SCHEMA,
    },
    {
        "name": "antigravity_job",
        "description": "查询、列出或清理由 antigravity_submit 启动的后台作业。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "list", "forget"], "default": "get"},
                "job_id": {"type": "string", "description": "antigravity_submit 返回的 job id。"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "antigravity_status",
        "description": (
            "报告 agy 路径、CLI 版本、工作目录、代理可见性、当日调用与 token、"
            "耗时统计与登录探测；排查故障先看它。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def tool_ask(args: Dict[str, Any]) -> Dict[str, Any]:
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return text_result("antigravity_ask requires a non-empty 'prompt' string.", True)
    if is_cancelled():
        return text_result("Antigravity turn cancelled before it started.", True)
    if MAX_PROMPT_CHARS and len(prompt) > MAX_PROMPT_CHARS:
        return text_result(
            f"prompt is {len(prompt)} characters, over AGY_MCP_MAX_PROMPT_CHARS={MAX_PROMPT_CHARS}; "
            "pass file paths in `files` (or set cwd and let the agent read them) instead of pasting contents",
            True,
        )
    prompt = attach_files(prompt, args.get("files"))
    if args.get("no_web"):
        prompt = attach_no_web(prompt)

    workspace = os.path.abspath(str(args.get("cwd"))) if args.get("cwd") else os.path.abspath(os.getcwd())
    if not os.path.isdir(workspace):
        return text_result(f"cwd does not exist: {workspace}", True)

    session_name = str(args.get("session") or DEFAULT_SESSION).strip() or DEFAULT_SESSION
    new_session = bool(args.get("new_session", False))
    continue_recent = bool(args.get("continue_session", False))
    handoff = bool(args.get("handoff", False))
    explicit_conversation = str(args["conversation"]).strip() if args.get("conversation") else None

    sessions = read_sessions()
    entry = sessions.get(session_name)
    entry = entry if isinstance(entry, dict) else {}
    notes: List[str] = []
    resumed = False
    warning = quota_warning()
    if warning:
        notes.append(warning)
    refresh_quota_in_background()

    if args.get("diff") or args.get("diff_base"):
        diff_text, diff_note = collect_diff(workspace, args.get("diff_base"), MAX_DIFF_CHARS)
        if diff_note:
            notes.append(diff_note)
        if diff_text:
            prompt = attach_diff(prompt, diff_text, workspace)
            notes.append(f"attached the local git diff ({len(diff_text)} chars)")

    conversation = explicit_conversation
    if conversation is None and not new_session and not continue_recent:
        stored = entry.get("conversation_id")
        stored_workspace = entry.get("workspace")
        if stored and stored_workspace == workspace:
            conversation = str(stored)
            resumed = True
        elif stored:
            notes.append(
                f'session "{session_name}" belongs to {stored_workspace}; '
                f"started a new Antigravity conversation in {workspace}"
            )

    if not handoff and AUTO_HANDOFF and conversation and LONG_CONTEXT_TOKENS > 0:
        previous_input = int(entry.get("last_input_tokens", 0) or 0)
        if previous_input >= LONG_CONTEXT_TOKENS:
            handoff = True
            notes.append(
                f"auto-handoff: the previous turn resent about {previous_input} input tokens; "
                "compacting into a fresh conversation (AGY_MCP_AUTO_HANDOFF=1)"
            )

    # 模型与思考强度在会话内粘住：中途改一次会一直生效，直到调用方传 "default"
    # （或改指定另一个模型/强度）。
    def _clears(value: Any) -> bool:
        return str(value or "").strip().lower() in ("", "default")

    sticky_model = entry.get("model")
    sticky_effort = entry.get("effort")
    model_arg, effort_arg = args.get("model"), args.get("effort")
    model_explicit = not _clears(model_arg)
    effort_explicit = not _clears(effort_arg)

    wanted_model = (
        model_arg
        if model_explicit
        else (None if str(model_arg or "").strip().lower() == "default" else sticky_model)
    )
    wanted_effort = (
        str(effort_arg).strip().lower()
        if effort_explicit
        else (None if str(effort_arg or "").strip().lower() == "default" else sticky_effort)
    )

    resolved_model, model_notes = resolve_model(wanted_model)
    resolved_model, resolved_effort, effort_notes = reconcile_model_and_effort(
        resolved_model, wanted_effort, bool(model_explicit or sticky_model)
    )
    notes.extend(model_notes + effort_notes)
    if effort_explicit and resolved_effort != (sticky_effort or None):
        notes.append(f"reasoning effort for session '{session_name}' is now {resolved_effort or 'the CLI default'}")
    if model_explicit and resolved_model != (sticky_model or None):
        notes.append(f"model for session '{session_name}' is now {resolved_model}")
    args["model"] = resolved_model or ""
    args["effort"] = resolved_effort or ""
    session_model = resolved_model or None
    session_effort = resolved_effort or None

    flags = session_flags(args, conversation, continue_recent)
    requested_format = str(args.get("output_format") or "text")
    transport = str(os.environ.get("AGY_MCP_TRANSPORT") or "stream").strip().lower()

    timeout_sec = args.get("timeout_sec") or DEFAULT_TIMEOUT_SEC
    try:
        timeout_sec = float(timeout_sec)
    except (TypeError, ValueError):
        return text_result("timeout_sec must be a number.", True)
    if timeout_sec < 0:
        return text_result("timeout_sec must be zero (no limit) or a positive number.", True)
    # 0 表示不限时：绝不能传一个很小的 --print-timeout，否则 CLI 会把轮次掐断。
    print_timeout = f"{int(timeout_sec)}s" if timeout_sec > 0 else UNLIMITED_PRINT_TIMEOUT
    call_timeout = (timeout_sec + TIMEOUT_GRACE_SEC) if timeout_sec > 0 else None

    payload: Optional[Dict[str, Any]] = None
    captured: Optional[str] = None
    status_value: Any = None
    usage_tokens: Dict[str, Any] = {}
    denied: List[str] = []
    out = ""
    err = ""
    code = 0
    worker: Optional[Worker] = None

    notify_progress(0, "queued for Antigravity", force=True)
    try:
        with turn_guard(session_name, workspace):
            state = read_state()
            used = calls_today(state)
            if MAX_CALLS_PER_DAY and used >= MAX_CALLS_PER_DAY:
                return text_result(
                    f"Daily Antigravity cap reached ({used}/{MAX_CALLS_PER_DAY}). "
                    "Raise AGY_MCP_MAX_CALLS_PER_DAY (0 disables the cap) if this volume is intended.",
                    True,
                )
            last = state.get("last_call_ts")
            if MIN_INTERVAL_SEC > 0 and isinstance(last, (int, float)):
                gap = MIN_INTERVAL_SEC - (time.time() - last)
                if gap > 0:
                    time.sleep(gap)

            before_map = read_last_conversations()
            reap_workers()
            started = time.time()

            if transport == "stream":
                # 会话由常驻进程持有，所以会话 id 不能进 key：否则第一次追问就会再冷启动一个进程。
                base_flags = session_flags(args, None, continue_recent)
                key = f"{session_name}|{workspace}|{' '.join(base_flags)}"
                for stale in [k for k in WORKERS if k.startswith(f"{session_name}|{workspace}|") and k != key]:
                    # 切换模型/强度绝不能打断正在跑的工作：把旧进程标记为退休，
                    # 等它到下一个轮次边界再停。
                    stale_worker = WORKERS[stale]
                    if stale_worker.busy:
                        stale_worker.retire = True
                        notes.append(
                            "model/effort change takes effect from the next turn; "
                            "the running Antigravity turn is left to finish"
                        )
                    else:
                        WORKERS.pop(stale).stop()
                worker = WORKERS.get(key)
                if worker is not None and not worker.alive():
                    worker.stop()
                    WORKERS.pop(key, None)
                    worker = None

                stream_args = ["--input-format", "stream-json", "--output-format", "stream-json"]
                digest: Optional[str] = None
                if handoff and (worker is not None or conversation):
                    if worker is None:
                        worker = Worker(
                            key,
                            stream_args + session_flags(args, conversation, False),
                            workspace,
                        )
                        worker.start()
                        WORKERS[key] = worker
                    digest_payload = worker.send(
                        handoff_prompt(),
                        call_timeout,
                        on_progress=lambda step, label, detail: notify_progress(
                            step, f"handoff digest: {label}" + (f" — {detail}" if detail else "")
                        ),
                    )
                    digest = extract_answer(digest_payload) or ""
                    merge_usage(usage_tokens, digest_payload)
                    worker.stop()
                    WORKERS.pop(key, None)
                    worker = None
                    conversation = None  # the real turn must land in a brand-new conversation
                    if digest:
                        notes.append("handoff: carried a digest of the previous conversation into a new one")

                if worker is None:
                    start_flags = (
                        session_flags(args, conversation, continue_recent)
                        if (conversation and not handoff)
                        else session_flags(args, None, False)
                        if handoff
                        else base_flags
                    )
                    worker = Worker(
                        key,
                        stream_args + start_flags,
                        workspace,
                    )
                    worker.start()
                    WORKERS[key] = worker
                    notes.append(
                        "started a long-lived Antigravity session process; later calls reuse it "
                        "(set AGY_MCP_TRANSPORT=oneshot to force one process per call)"
                    )
                task = current_task()
                if task is not None:
                    task.bind(worker)
                if is_cancelled():
                    return text_result("Antigravity turn cancelled.", True)
                payload = worker.send(
                    seed_prompt(prompt, digest),
                    call_timeout,
                    on_progress=lambda step, label, detail: notify_progress(
                        step, f"step {step}: {label}" + (f" — {detail}" if detail else "")
                    ),
                )
            else:
                digest = None
                if handoff and conversation:
                    digest_argv = (
                        ["-p", handoff_prompt()]
                        + session_flags(args, conversation, False)
                        + ["--print-timeout", print_timeout, "--output-format", "json"]
                    )
                    _, digest_out, _ = run_agy(
                        digest_argv, cwd=workspace, timeout=call_timeout
                    )
                    digest_payload = parse_json_output(digest_out)
                    digest = extract_answer(digest_payload) if digest_payload else None
                    merge_usage(usage_tokens, digest_payload)
                    conversation = None
                    if digest:
                        notes.append("handoff: carried a digest of the previous conversation into a new one")
                send_flags = session_flags(args, None, False) if handoff else flags
                if is_cancelled():
                    return text_result("Antigravity turn cancelled.", True)
                argv = ["-p", seed_prompt(prompt, digest)] + send_flags
                argv += ["--print-timeout", print_timeout, "--output-format", "json"]
                code, out, err = run_agy(argv, cwd=workspace, timeout=call_timeout)
                payload = parse_json_output(out)

            duration_ms = int((time.time() - started) * 1000)

            if payload:
                raw_id = payload.get("conversation_id") or payload.get("conversationId")
                if isinstance(raw_id, str) and raw_id.strip():
                    captured = raw_id.strip()
                status_value = payload.get("status")
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    usage_tokens = {
                        key: value for key, value in usage.items() if isinstance(value, (int, float))
                    }
                raw_denied = payload.get("denied_actions")
                if isinstance(raw_denied, list):
                    for item in raw_denied:
                        if isinstance(item, dict):
                            denied.append(str(item.get("display_name") or item.get("action") or "unknown"))
                        elif isinstance(item, str):
                            denied.append(item)

            succeeded = (
                code == 0
                and payload is not None
                and str(status_value or "SUCCESS").upper() in ("SUCCESS", "OK")
            )

            if succeeded and captured is None:
                workspace_key = workspace
                after_map = read_last_conversations()
                if after_map.get(workspace_key) and after_map.get(workspace_key) != before_map.get(workspace_key):
                    captured = after_map[workspace_key]
                else:
                    captured = newest_conversation_since(started)
            if succeeded and captured is None and conversation is None:
                notes.append(
                    "could not capture the Antigravity conversation id; "
                    "the next call in this session will start a fresh conversation"
                )

            if succeeded and captured:
                previous = entry.get("conversation_id")
                total_input = int(usage_tokens.get("input_tokens", 0) or 0)
                # 上下文大小取最后一步的输入，而不是整轮合计（那是各步骤之和）。
                current_input = (
                    worker.last_step_input
                    if worker is not None and worker.last_step_input
                    else total_input
                )
                if (
                    not handoff
                    and LONG_CONTEXT_TOKENS > 0
                    and current_input >= LONG_CONTEXT_TOKENS
                    and int(entry.get("last_input_tokens", 0) or 0) < LONG_CONTEXT_TOKENS
                ):
                    notes.append(
                        f"this Antigravity conversation now resends about {current_input} input tokens per "
                        f"turn (turn {payload.get('num_turns') if payload else '?'}); consider "
                        "handoff: true to compact it into a fresh conversation, or new_session: true to "
                        "drop the history"
                    )
                sessions[session_name] = {
                    "conversation_id": captured,
                    "workspace": workspace,
                    "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "calls": int(entry.get("calls", 0) or 0) + 1 if previous == captured else 1,
                    "num_turns": payload.get("num_turns") if payload else None,
                    "model": session_model,
                    "effort": session_effort,
                    "last_model": args.get("model") or (payload.get("model") if payload else None),
                    "last_input_tokens": current_input,
                    "input_tokens": int(entry.get("input_tokens", 0) or 0) + current_input,
                    "output_tokens": int(entry.get("output_tokens", 0) or 0)
                    + int(usage_tokens.get("output_tokens", 0) or 0),
                }
                write_sessions(sessions)

            same_day = state.get("day") == _today()
            write_state(
                {
                    "day": _today(),
                    "calls": used + (1 if succeeded else 0),
                    "last_call_ts": time.time(),
                    "total_tokens": (int(state.get("total_tokens", 0) or 0) if same_day else 0)
                    + int(usage_tokens.get("total_tokens", 0) or 0),
                    "input_tokens": (int(state.get("input_tokens", 0) or 0) if same_day else 0)
                    + int(usage_tokens.get("input_tokens", 0) or 0),
                    "output_tokens": (int(state.get("output_tokens", 0) or 0) if same_day else 0)
                    + int(usage_tokens.get("output_tokens", 0) or 0),
                }
            )
            log_usage(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "tool": "antigravity_ask",
                    "transport": transport,
                    "session": session_name,
                    "conversation": captured,
                    "resumed": resumed,
                    "model": args.get("model"),
                    "cwd": workspace,
                    "prompt_chars": len(prompt),
                    "duration_ms": duration_ms,
                    "exit_code": code,
                    "status": status_value,
                    "ok": succeeded,
                    "denied_actions": denied,
                    "steps": worker.step_count if worker is not None else None,
                    "total_input_tokens": total_input,
                    "usage": usage_tokens,
                }
            )
    except TimeoutError as exc:
        remember_partial_turn(sessions, entry, session_name, workspace, worker)
        if worker is not None:
            worker.stop()
            WORKERS.pop(worker.key, None)
        log_usage(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "tool": "antigravity_ask",
                "transport": transport,
                "session": session_name,
                "conversation": getattr(worker, "conversation_id", None),
                "ok": False,
                "error": str(exc),
            }
        )
        return text_result(f"Antigravity is busy: {exc}", True)
    except RuntimeError as exc:
        remember_partial_turn(sessions, entry, session_name, workspace, worker)
        if worker is not None:
            worker.stop()
            WORKERS.pop(worker.key, None)
        log_usage(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "tool": "antigravity_ask",
                "transport": transport,
                "session": session_name,
                "conversation": getattr(worker, "conversation_id", None),
                "ok": False,
                "error": str(exc),
            }
        )
        return text_result(f"Antigravity session failed: {exc}", True)
    except subprocess.TimeoutExpired:
        return text_result(f"agy did not finish within {int(timeout_sec) + TIMEOUT_GRACE_SEC}s.", True)
    except FileNotFoundError as exc:
        return text_result(str(exc), True)

    if denied:
        notes.append(
            "the sandbox denied these Antigravity tool permissions: "
            + ", ".join(denied)
            + ". Pass skip_permissions=true (or add an allow rule) if the answer needs them."
        )
    if code != 0:
        return text_result(join_streams(code, out, err), True, notes)
    if payload is None:
        return text_result(join_streams(code, out, err), False, notes)
    if status_value is not None and str(status_value).upper() not in ("SUCCESS", "OK"):
        detail = payload.get("error") or join_streams(code, out, err)
        return text_result(f"Antigravity reported status {status_value}: {detail}", True, notes)

    answer = extract_answer(payload)
    if not answer and denied:
        return text_result(
            "Antigravity produced no answer because the sandbox denied the tool it needed.", True, notes
        )
    if not answer and not out.strip():
        # 状态 SUCCESS 却没有任何文字：这一轮跑了但没产出，通常是预算全花在工具调用上，
        # 或者撞到了模型的上下文上限。
        spent = int(usage_tokens.get("input_tokens", 0) or 0)
        hint = (
            f"Antigravity finished without any text (status={status_value}, "
            f"{payload.get('num_turns', '?')} turn(s), ~{spent} input tokens). "
            "Usual causes: the turn spent itself on tool calls (browsing, file reads) or the "
            "conversation is too large. Try a narrower prompt, pass the data in directly "
            "(files/diff), raise timeout_sec, or start a new session."
        )
        return text_result(hint, True, notes)
    if requested_format == "json":
        return text_result(json.dumps(payload, ensure_ascii=False), False, notes)
    return text_result(answer if answer else out.strip(), False, notes)


def tool_simple(argv: List[str], label: str) -> Dict[str, Any]:
    try:
        code, out, err = run_agy(argv, timeout=METADATA_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return text_result(f"agy {label} timed out.", True)
    except FileNotFoundError as exc:
        return text_result(str(exc), True)
    return text_result(join_streams(code, out, err), code != 0)


def tool_models(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        code, models_text = cached_models()
    except subprocess.TimeoutExpired:
        return text_result("agy models timed out.", True)
    except FileNotFoundError as exc:
        return text_result(str(exc), True)
    models = parse_models(models_text)
    payload: Dict[str, Any] = {"count": len(models), "models": models}
    if not models:
        payload["raw"] = models_text.strip()
    return text_result(json.dumps(payload, ensure_ascii=False, indent=2), code != 0 and not models)


def tool_quota(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        payload = read_quota()
    except subprocess.TimeoutExpired:
        return text_result("agy /quota timed out.", True)
    except FileNotFoundError as exc:
        return text_result(str(exc), True)

    groups = summarize_quota(payload)
    report = {
        "table": (payload.get("response") or "").strip(),
        "groups": groups,
        "cached_for_sec": QUOTA_CACHE_TTL,
        "note": "answered by the CLI itself; no turn ran and no quota was spent",
    }
    ok = bool(groups) or bool(report["table"])
    return text_result(json.dumps(report, ensure_ascii=False, indent=2), not ok)


def tool_status(args: Dict[str, Any]) -> Dict[str, Any]:
    report: Dict[str, Any] = {"cwd": os.getcwd(), "python": sys.version.split()[0]}
    state = read_state()
    report["guard"] = {
        "min_interval_sec": MIN_INTERVAL_SEC,
        "max_calls_per_day": MAX_CALLS_PER_DAY or "unlimited",
        "calls_today": calls_today(state),
        "tokens_today": {
            "total": int(state.get("total_tokens", 0) or 0) if state.get("day") == _today() else 0,
            "input": int(state.get("input_tokens", 0) or 0) if state.get("day") == _today() else 0,
            "output": int(state.get("output_tokens", 0) or 0) if state.get("day") == _today() else 0,
        },
        "usage_log": USAGE_PATH,
        "durations": usage_stats(),
    }
    report["proxy"] = proxy_env_report() or "(no proxy env visible to this server)"
    report["agy_cli_home"] = AGY_CLI_HOME
    reap_workers()
    report["workers"] = {
        "transport": str(os.environ.get("AGY_MCP_TRANSPORT") or "stream"),
        "idle_reap_sec": WORKER_IDLE_SEC,
        "active": [
            {"session": key.split("|")[0], "workspace": key.split("|")[1], "turns": worker.turns}
            for key, worker in WORKERS.items()
        ],
    }
    report["sessions"] = {
        "tracked": sorted(read_sessions()),
        "default": DEFAULT_SESSION,
        "store": SESSIONS_PATH,
    }
    try:
        report["agy_path"] = resolve_agy()
    except FileNotFoundError as exc:
        report["agy_path"] = None
        report["error"] = str(exc)
        return text_result(json.dumps(report, ensure_ascii=False, indent=2), True)

    code, out, err = run_agy(["--version"], timeout=60)
    report["version"] = (out or err).strip()
    report["version_exit_code"] = code

    code, models_output = cached_models()
    report["models_exit_code"] = code
    report["models_output"] = models_output[:2000]
    report["models_cached_for_sec"] = MODELS_CACHE_TTL
    report["signed_in"] = code == 0
    return text_result(json.dumps(report, ensure_ascii=False, indent=2), code != 0)


def tool_sessions(args: Dict[str, Any]) -> Dict[str, Any]:
    action = str(args.get("action") or "list").lower()
    name = args.get("session")

    if action == "forget":
        sessions = read_sessions()
        target = str(name or "").strip()
        if not target:
            return text_result("action=forget needs a 'session' name (use '*' to clear all).", True)
        if target == "*":
            forgotten = sorted(sessions)
            write_sessions({})
        elif target in sessions:
            forgotten = [target]
            sessions.pop(target, None)
            write_sessions(sessions)
        else:
            return text_result(f"no tracked session named '{target}'.", True)
        return text_result(f"forgot: {', '.join(forgotten)}")

    sessions = read_sessions()
    tracked = []
    for session_name in sorted(sessions):
        entry = sessions[session_name]
        if not isinstance(entry, dict):
            continue
        tracked.append(
            {
                "session": session_name,
                "conversation_id": entry.get("conversation_id"),
                "workspace": entry.get("workspace"),
                "calls": entry.get("calls"),
                "num_turns": entry.get("num_turns"),
                "model": entry.get("model"),
                "effort": entry.get("effort"),
                "input_tokens": entry.get("input_tokens"),
                "output_tokens": entry.get("output_tokens"),
                "last_error": entry.get("last_error"),
                "updated": entry.get("updated"),
            }
        )

    recent = []
    conversations_dir = os.path.join(AGY_CLI_HOME, "conversations")
    try:
        names = [n for n in os.listdir(conversations_dir) if n.endswith(".db")]
    except OSError:
        names = []
    entries = []
    for name_only in names:
        path = os.path.join(conversations_dir, name_only)
        try:
            entries.append((os.path.getmtime(path), name_only[: -len(".db")]))
        except OSError:
            continue
    for mtime, conversation_id in sorted(entries, reverse=True)[:10]:
        recent.append(
            {
                "conversation_id": conversation_id,
                "last_modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime)),
            }
        )

    report = {
        "tracked_sessions": tracked,
        "cli_conversations": recent,
        "workspace_index": read_last_conversations(),
        "store": SESSIONS_PATH,
    }
    return text_result(json.dumps(report, ensure_ascii=False, indent=2))


HANDLERS = {
    "antigravity_ask": tool_ask,
    "antigravity_models": tool_models,
    "antigravity_agents": lambda args: tool_simple(["agent"], "agent"),
    "antigravity_sessions": tool_sessions,
    "antigravity_quota": tool_quota,
    # 这两个函数定义在后面：延迟解析，好让这张表跟其他工具放一起
    "antigravity_submit": lambda args: tool_submit(args),
    "antigravity_job": lambda args: tool_job(args),
    "antigravity_status": tool_status,
}


def handle_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        params = message.get("params") or {}
        requested = params.get("protocolVersion")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method in ("notifications/cancelled", "cancelled"):
        params = message.get("params") or {}
        cancel_task(params.get("requestId"))
        return None
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(str(name))
        if handler is None:
            return text_result(f"Unknown tool: {name}", True)
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}
        try:
            return handler(args)
        except Exception as exc:  # keep the server alive on tool errors
            log(f"tool {name} failed: {exc!r}")
            return text_result(f"{name} failed: {exc}", True)
    if method == "resources/list":
        return {"resources": []}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": []}
    if method == "logging/setLevel":
        return {}

    if request_id is None:
        return None
    return None


def send(payload: Dict[str, Any]) -> None:
    # 用 errors="replace"：子进程回传孤立代理字符（它用 surrogateescape 解出的非法字节）时，
    # 绝不能因为写响应就把整个服务器弄崩。
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace")
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def _run_tool_call(message: Dict[str, Any], task: ActiveTask) -> None:
    """Run one tool call in its own thread; the main loop keeps reading for cancellations."""
    _TASK_LOCAL.task = task
    error: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    try:
        result = handle_request(message)
    except Exception as exc:  # noqa: BLE001 - a bad call must not kill the server
        log(f"tool call failed: {exc!r}")
        error = {"code": -32603, "message": f"Internal error: {exc}"}
    finally:
        finish_task(task.request_id)
        _TASK_LOCAL.task = None

    if task.suppress_response or task.cancelled.is_set():
        log(f"dropped the response for cancelled request {task.request_id!r}")
        return
    with SEND_LOCK:
        if error is not None:
            send({"jsonrpc": "2.0", "id": task.request_id, "error": error})
        elif result is None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": task.request_id,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
        else:
            send({"jsonrpc": "2.0", "id": task.request_id, "result": result})


def _tool_call_loop() -> None:
    """Single consumer: tool calls stay FIFO, while the main loop keeps reading for cancels."""
    while True:
        message, task = TOOL_QUEUE.get()
        try:
            _run_tool_call(message, task)
        except Exception as exc:  # noqa: BLE001 - keep serving whatever happens
            log(f"tool call loop error: {exc!r}")


def serve() -> int:
    log(f"serving {SERVER_NAME} {SERVER_VERSION}")
    _install_signal_handlers()
    reap_orphan_workers()
    threading.Thread(target=_reaper_loop, daemon=True).start()
    threading.Thread(target=_tool_call_loop, daemon=True).start()
    if PREWARM:
        threading.Thread(target=prewarm_default_session, daemon=True).start()
    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log("skipping malformed JSON line")
            continue
        if not isinstance(message, dict):
            continue

        # 工具调用可能阻塞数分钟，所以放到循环之外执行，
        # 这样期间到达的 `notifications/cancelled` 才能叫停这一轮。
        if message.get("method") == "tools/call" and message.get("id") is not None:
            params = message.get("params") or {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            task = ActiveTask(message["id"], meta.get("progressToken"))
            register_task(task)
            if str(params.get("name") or "") in FAST_TOOLS:
                # 只读类工具不碰会话进程：绝不排在长轮次后面干等。
                threading.Thread(target=_run_tool_call, args=(message, task), daemon=True).start()
            else:
                TOOL_QUEUE.put((message, task))
            continue

        try:
            result = handle_request(message)
        except Exception as exc:
            log(f"handler error: {exc!r}")
            if message.get("id") is not None:
                with SEND_LOCK:
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": message.get("id"),
                            "error": {"code": -32603, "message": f"Internal error: {exc}"},
                        }
                    )
            continue

        if message.get("id") is None:
            continue
        with SEND_LOCK:
            if result is None:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {
                            "code": -32601,
                            "message": f"Method not found: {message.get('method')}",
                        },
                    }
                )
            else:
                send({"jsonrpc": "2.0", "id": message.get("id"), "result": result})

    # stdin 已关闭：先让很快的在跑调用把回答刷出去，再停掉剩下的。
    deadline = time.monotonic() + SHUTDOWN_GRACE_SEC
    while time.monotonic() < deadline:
        with TASKS_LOCK:
            if not ACTIVE_TASKS:
                break
        time.sleep(0.1)
    with TASKS_LOCK:
        pending = list(ACTIVE_TASKS)
    for request_id in pending:
        cancel_task(request_id)
    shutdown_workers()
    return 0


PROBE_PROMPT = "Reply with exactly one word: OK"


def tool_submit(args: Dict[str, Any]) -> Dict[str, Any]:
    """Run a turn in the background so a long Antigravity job does not block the client."""
    job_id = f"job-{int(time.time() * 1000)}-{len(JOBS) + 1}"
    record = {
        "job_id": job_id,
        "state": "running",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "prompt_chars": len(str(args.get("prompt") or "")),
        "session": args.get("session") or DEFAULT_SESSION,
        "model": args.get("model"),
        "effort": args.get("effort"),
        "pid": None,
        "out": os.path.join(JOBS_DIR, f"{job_id}.out"),
        "cwd": os.path.abspath(str(args.get("cwd"))) if args.get("cwd") else os.path.abspath(os.getcwd()),
    }
    try:
        # 故意脱离进程：这一轮不能依赖本服务器存活，所以输出写文件而不是管道。
        # 代价是：没有进度通知，也不能取消。
        workspace = record["cwd"]
        prompt = str(args.get("prompt") or "")
        if args.get("files"):
            prompt = attach_files(prompt, args.get("files"))
        if args.get("diff") or args.get("diff_base"):
            diff_text, _ = collect_diff(workspace, args.get("diff_base"), MAX_DIFF_CHARS)
            if diff_text:
                prompt = attach_diff(prompt, diff_text, workspace)
        session_name = str(record["session"])
        entry = read_sessions().get(session_name)
        entry = entry if isinstance(entry, dict) else {}
        conversation = entry.get("conversation_id") if entry.get("workspace") == workspace else None
        model, _ = resolve_model(args.get("model"))
        model, effort, _ = reconcile_model_and_effort(model, args.get("effort"), bool(args.get("model")))
        flags = session_flags(
            {**args, "model": model or "", "effort": effort or ""},
            str(conversation) if conversation else None,
            False,
        )
        argv = (
            ["-p", prompt]
            + flags
            + ["--print-timeout", UNLIMITED_PRINT_TIMEOUT, "--output-format", "json"]
        )
        detached = (
            {"start_new_session": True}
            if os.name != "nt"
            else {"creationflags": DETACHED_PROCESS | CREATE_NO_WINDOW}
        )
        os.makedirs(JOBS_DIR, exist_ok=True)
        with open(record["out"], "wb") as handle:
            proc = subprocess.Popen(
                agy_command_prefix() + argv,
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                **detached,
            )
        record["pid"] = proc.pid
        record["conversation"] = str(conversation) if conversation else None
    except (OSError, subprocess.SubprocessError) as exc:
        record.update({"state": "failed", "result": text_result(f"could not start job: {exc}", True)})
    write_job(job_id, record)
    with JOBS_LOCK:
        JOBS[job_id] = record
    return text_result(
        json.dumps(
            {
                "job_id": job_id,
                "state": "running",
                "hint": "poll with antigravity_job; the job keeps running while you do other work",
            },
            ensure_ascii=False,
        )
    )


def tool_job(args: Dict[str, Any]) -> Dict[str, Any]:
    action = str(args.get("action") or "get").lower()
    job_id = str(args.get("job_id") or "")
    if action == "list":
        jobs = [
            {key: value for key, value in entry.items() if key != "result"}
            for entry in list_jobs()
        ]
        return text_result(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2))
    if action == "forget":
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
            if not job_id:
                JOBS.clear()
        try:
            if job_id:
                os.remove(_job_path(job_id))
            else:
                for name in os.listdir(JOBS_DIR):
                    if name.endswith(".json"):
                        os.remove(os.path.join(JOBS_DIR, name))
        except OSError:
            pass
        return text_result("jobs cleared")

    with JOBS_LOCK:
        entry = JOBS.get(job_id)
    if entry is None:
        # 内存里没有：服务器可能重启过，去磁盘上找。
        entry = read_job(job_id)
    if entry is None:
        return text_result(f"unknown job: {job_id or '(no job_id)'}", True)
    if entry.get("state") == "running":
        entry = collect_detached_job(entry)
    state = entry.get("state")
    result = entry.get("result")
    if state == "running":
        return text_result(
            json.dumps(
                {
                    "job_id": job_id,
                    "state": "running",
                    "since": entry.get("created"),
                    "note": (
                        "if this server restarted, the turn from the previous process cannot be "
                        "collected; check the session with antigravity_sessions"
                    ),
                },
                ensure_ascii=False,
            )
        )
    return result if isinstance(result, dict) else text_result(str(result))


def probe_protocol() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Run one tiny turn and return (result payload, raw stream events).

    The stream protocol is reverse engineered, so this is also the shape check:
    `init` with a conversation id, `step_update` nested under `step_update`, and a
    terminal `result` carrying conversation_id/status/response.
    """
    workspace = os.path.abspath(os.getcwd())
    flags = session_flags(
        {"sandbox": True, "skip_permissions": True, "disable_slash_commands": True}, None, False
    )
    events: List[Dict[str, Any]] = []
    transport = str(os.environ.get("AGY_MCP_TRANSPORT") or "stream").lower()
    if transport == "stream":
        worker = Worker(
            "self-test",
            ["--input-format", "stream-json", "--output-format", "stream-json"] + flags,
            workspace,
        )
        worker.start()
        try:
            payload = worker.send(PROBE_PROMPT, 180, on_event=events.append)
        finally:
            worker.stop()
        return payload, events

    _, out, err = run_agy(
        ["-p", PROBE_PROMPT] + flags + ["--print-timeout", "180s", "--output-format", "json"],
        cwd=workspace,
        timeout=210,
    )
    return parse_json_output(out) or {"status": "ERROR", "error": join_streams(0, out, err)[:200]}, events


def self_test(argv: List[str]) -> int:
    """One command that answers: is this machine actually able to use agy-mcp right now?

    Options: `--skip-ask` (do not spend a small live turn), `--no-proxy-required`
    (treat a missing proxy as a warning instead of a failure).
    """
    checks: List[Tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))
        print(f"[{'ok  ' if ok else 'FAIL'}] {name}: {detail}")

    try:
        record("agy binary", True, resolve_agy())
    except FileNotFoundError as exc:
        record("agy binary", False, str(exc))

    proxies = proxy_env_report()
    proxy_optional = "--no-proxy-required" in argv
    record(
        "proxy env",
        bool(proxies) or proxy_optional,
        ", ".join(f"{key}={value}" for key, value in proxies.items())
        if proxies
        else "no HTTP_PROXY/HTTPS_PROXY visible (required on networks that cannot reach Google directly)",
    )

    try:
        code, out, err = run_agy(["--version"], timeout=60)
        record("agy --version", code == 0, (out or err).strip() or f"exit {code}")
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        record("agy --version", False, repr(exc))

    try:
        code, models = cached_models()
        first_line = models.splitlines()[0] if models else ""
        record("signed in (agy models)", code == 0, first_line or "no output")
    except Exception as exc:  # noqa: BLE001
        record("signed in (agy models)", False, repr(exc))

    try:
        payload = read_quota()
        table = [line for line in (payload.get("response") or "").splitlines() if line.strip()]
        ok = bool(table) or bool(payload.get("command"))
        record("quota (/quota, spends no quota)", ok, table[0] if table else str(payload.get("error", "")))
    except Exception as exc:  # noqa: BLE001
        record("quota (/quota, spends no quota)", False, repr(exc))

    if "--skip-ask" not in argv:
        try:
            payload, events = probe_protocol()
            answer = extract_answer(payload)
            record(
                "live turn (spends a little quota)",
                bool(answer) and "OK" in answer.upper(),
                answer or str(payload.get("error") or payload.get("status") or "no answer"),
            )
            missing = [key for key in ("conversation_id", "status", "response") if key not in payload]
            record(
                "result shape",
                not missing,
                "conversation_id/status/response present" if not missing else f"missing {missing}",
            )
            if events:
                init_ok = any(event.get("event") == "init" and event.get("conversation_id") for event in events)
                step = [event.get("step_update") for event in events if isinstance(event.get("step_update"), dict)]
                step_ok = any(update.get("step_type") for update in step)
                result_ok = any(event.get("event") == "result" for event in events)
                record(
                    "stream protocol (init/step_update/result)",
                    init_ok and step_ok and result_ok,
                    f"init={init_ok} step_update={step_ok} result={result_ok}",
                )
        except Exception as exc:  # noqa: BLE001
            record("live turn (spends a little quota)", False, repr(exc))

    failed = [name for name, ok, _ in checks if not ok]
    print()
    if failed:
        print(f"self-test FAILED: {', '.join(failed)}")
        return 1
    print("self-test OK: agy-mcp is ready")
    return 0


def main(argv: List[str]) -> int:
    if "--self-test" in argv:
        return self_test(argv)
    if "--list-tools" in argv:
        print(json.dumps(TOOLS, ensure_ascii=False, indent=2))
        return 0
    if "--status" in argv:
        print(tool_status({})["content"][0]["text"])
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
