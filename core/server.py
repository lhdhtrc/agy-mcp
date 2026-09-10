"""MCP 服务器本体：stdio 上的 JSON-RPC 循环、取消与进度、CLI 子命令。

传输是换行分隔的 JSON-RPC 2.0（不使用 Content-Length 分帧），只依赖标准库，
任何 MCP 客户端都能直接拉起本进程。登录状态由 CLI 自己保管，本服务器从不接触凭据。

工具调用可能阻塞几分钟，所以主循环只负责读输入，真正的调用交给别的线程；
期间到达的 `notifications/cancelled` 才能叫停那一轮。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core.agy import resolve_agy, run_agy
from core.config import (
    DEFAULT_PROTOCOL,
    DEFAULT_SESSION,
    PREWARM,
    SERVER_NAME,
    SERVER_VERSION,
    SHUTDOWN_GRACE_SEC,
    SUPPORTED_PROTOCOLS,
    log,
)
from core.diag import proxy_env_report
from core.protocol import extract_answer, join_streams, parse_json_output, text_result
from core.quota import cached_models, read_quota
from core.tasks import (
    ACTIVE_TASKS,
    SEND_LOCK,
    TASKS_LOCK,
    TOOL_QUEUE,
    ActiveTask,
    cancel_task,
    finish_task,
    register_task,
    send,
    _TASK_LOCAL,
)
from core.tools import (
    HANDLERS,
    TOOLS,
    default_session_args,
    session_flags,
    tool_status,
    worker_key,
)
from core.worker import (
    WORKERS,
    Worker,
    _install_signal_handlers,
    _reaper_loop,
    reap_orphan_workers,
    shutdown_workers,
    start_registered_worker,
)


# 只读类工具不碰会话进程，因此不排在长轮次后面干等
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


def prewarm_default_session() -> None:
    """提前把默认会话进程拉起来，让第一次真正的调用就是热轮。

    启动参数必须与"什么都不指定的一次调用"完全一致（`default_session_args`），
    否则 worker key 对不上：预热起的进程会被当成切换参数后的旧进程直接停掉，白冷启动一次。
    """
    try:
        workspace = os.path.abspath(os.getcwd())
        args = default_session_args()
        key = worker_key(DEFAULT_SESSION, workspace, args, False)
        if WORKERS.get(key) is not None:
            return
        worker, created = start_registered_worker(
            key,
            ["--input-format", "stream-json", "--output-format", "stream-json"]
            + session_flags(args, None, False),
            workspace,
        )
        if created:
            log(f"prewarmed the default session process for {workspace}")
    except Exception as exc:  # noqa: BLE001 - prewarming is best effort
        log(f"prewarm failed: {exc!r}")


def handle_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理一条 JSON-RPC 消息；通知类消息返回 None（不回响应）。"""
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
        except Exception as exc:  # 工具报错不能把服务器带崩
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


def _run_tool_call(message: Dict[str, Any], task: ActiveTask) -> None:
    """在自己的线程里跑一次工具调用；主循环继续读输入，好接住取消。"""
    _TASK_LOCAL.task = task
    error: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    try:
        result = handle_request(message)
    except Exception as exc:  # noqa: BLE001 - 单次调用出错必须不影响服务器
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
    """唯一的消费者：工具调用保持先进先出，主循环腾出来接取消。"""
    while True:
        message, task = TOOL_QUEUE.get()
        try:
            _run_tool_call(message, task)
        except Exception as exc:  # noqa: BLE001 - 不管出什么事都继续服务
            log(f"tool call loop error: {exc!r}")


def serve() -> int:
    log(f"serving {SERVER_NAME} {SERVER_VERSION}")
    _install_signal_handlers()
    reap_orphan_workers()
    if PREWARM:
        # 同步登记：与第一次调用抢同一个 key 时不会留下两个进程（预热本身只是 Popen，很快）
        prewarm_default_session()
    threading.Thread(target=_reaper_loop, daemon=True).start()
    threading.Thread(target=_tool_call_loop, daemon=True).start()
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


def probe_protocol() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """跑一轮极小的真实调用，返回（结果负载, 原始流事件）。

    流协议是逆向出来的，所以这里顺带校验形状：带会话 id 的 `init`、嵌套在
    `step_update` 下的步骤、以及末尾带 conversation_id/status/response 的 `result`。
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
    """一条命令回答：这台机器现在到底能不能用 agy-mcp？

    可选：`--skip-ask`（不花那一小轮真实额度）、`--no-proxy-required`（看不到代理只警告，不算失败）。
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
    except Exception as exc:  # noqa: BLE001 - 只报告，不抛
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


def run() -> None:
    """console_scripts 入口（`pip install .` 之后的 `agy-mcp` 命令）。"""
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":  # 也让 `python -m core.server` 可用
    raise SystemExit(main(sys.argv[1:]))
