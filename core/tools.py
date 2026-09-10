"""八个工具的 handler：ask / models / agents / sessions / quota / submit / job / status。

这一层只依赖下面的各层（config / agy / session / worker / quota / jobs / prompts /
protocol / guard / tasks / diag），不碰协议循环本身的细节，所以可以单独测试与复用。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from core.agy import CREATE_NO_WINDOW, agy_command_prefix, resolve_agy, run_agy
from core.config import (
    AGY_CLI_HOME,
    AUTO_HANDOFF,
    DEFAULT_MODEL,
    DEFAULT_SESSION,
    DEFAULT_TIMEOUT_SEC,
    LONG_CONTEXT_TOKENS,
    MAX_CALLS_PER_DAY,
    MAX_DIFF_CHARS,
    MAX_PROMPT_CHARS,
    METADATA_TIMEOUT_SEC,
    MIN_INTERVAL_SEC,
    SESSIONS_PATH,
    TIMEOUT_GRACE_SEC,
    UNLIMITED_PRINT_TIMEOUT,
    WORKER_IDLE_SEC,
)
from core.diag import collect_diff, proxy_env_report
from core.guard import (
    USAGE_PATH,
    calls_today,
    log_usage,
    merge_usage,
    read_state,
    state_guard,
    turn_guard,
    usage_stats,
    write_state,
    _today,
)
from core.jobs import (
    DETACHED_PROCESS,
    JOBS,
    JOBS_DIR,
    JOBS_LOCK,
    _job_path,
    _job_output_path,
    _remove_quietly,
    collect_detached_job,
    list_jobs,
    read_job,
    write_job,
)
from core.prompts import (
    attach_diff,
    attach_files,
    attach_no_web,
    handoff_prompt,
    seed_prompt,
)
from core.protocol import (
    extract_answer,
    join_streams,
    parse_json_output,
    text_result,
)
from core.quota import (
    MODELS_CACHE_TTL,
    QUOTA_CACHE_TTL,
    cached_models,
    parse_models,
    quota_warning,
    read_quota,
    reconcile_model_and_effort,
    refresh_quota_in_background,
    resolve_model,
    summarize_quota,
)
from core.session import (
    newest_conversation_since,
    read_last_conversations,
    read_sessions,
    remember_partial_turn,
    write_sessions,
)
from core.tasks import current_task, is_cancelled, notify_progress
from core.worker import WORKERS, WORKERS_LOCK, Worker, reap_workers, start_registered_worker


def session_flags(args: Dict[str, Any], conversation: Optional[str], continue_recent: bool) -> List[str]:
    """拼出一次会话进程必须长期保持一致的参数。"""
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


def worker_key(
    session_name: str, workspace: str, args: Dict[str, Any], continue_recent: bool = False
) -> str:
    """常驻会话进程的复用 key：会话名 + workspace + 影响进程启动的参数。

    预热与真实调用必须走同一个函数，否则两边的 key 对不上（预热白做）。
    会话 id 不能进 key：否则第一次追问就会再冷启动一个进程。
    """
    return f"{session_name}|{workspace}|{' '.join(session_flags(args, None, continue_recent))}"


def default_session_args() -> Dict[str, Any]:
    """一次"什么都不指定"的调用实际使用的 model / effort。

    不传 model 时也不是"没有模型"，而是解析成默认模型并写进启动参数，
    所以预热必须带上同样的参数，否则 key 永远对不上。
    """
    model, _ = resolve_model(None)
    model, effort, _ = reconcile_model_and_effort(model, None, False)
    return {"model": model or "", "effort": effort or ""}


def worker_matches_intent(
    worker: Worker, conversation: Optional[str], continue_recent: bool
) -> bool:
    """现有常驻进程能不能直接承接这一轮。

    两个坑：预热的空进程没有 `--conversation`（拿它续接会静默丢掉历史）；已经聊过
    几轮的进程无法回到全新会话（`new_session` 会失效）。所以要看它"停在哪"。
    """
    if worker.turns == 0:
        return worker.started_conversation == (conversation or None) and (
            worker.started_continue_recent == bool(continue_recent)
        )
    if conversation:
        return worker.conversation_id == conversation
    if continue_recent:
        return True
    return False  # 想要一个全新会话，但它已经有历史


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

    timeout_sec = args.get("timeout_sec")
    if timeout_sec is None:  # 显式传 0 表示"不限时"，不能被默认值顶掉
        timeout_sec = DEFAULT_TIMEOUT_SEC
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
    # 整轮 usage 的输入合计：失败轮次也要记进用量日志，所以先给默认值，
    # 否则"拿到 payload 但没走到成功分支"时 log_usage 会引用未赋值变量直接抛错。
    total_input = 0
    denied: List[str] = []
    out = ""
    err = ""
    code = 0
    worker: Optional[Worker] = None

    notify_progress(0, "queued for Antigravity", force=True)
    try:
        with turn_guard(session_name, workspace):
            with state_guard():
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
                key = worker_key(session_name, workspace, args, continue_recent)
                prefix = f"{session_name}|{workspace}|"
                busy_keys: List[str] = []
                retired: List[Worker] = []
                with WORKERS_LOCK:
                    for stale in [k for k in WORKERS if k.startswith(prefix) and k != key]:
                        stale_worker = WORKERS[stale]
                        if stale_worker.busy:
                            busy_keys.append(stale)
                        else:
                            retired.append(WORKERS.pop(stale))
                if busy_keys:
                    # 切换模型/强度绝不能打断正在跑的工作：把旧进程标记为退休，
                    # 等它到下一个轮次边界再停。
                    notes.append(
                        "model/effort change takes effect from the next turn; "
                        "the running Antigravity turn is left to finish"
                    )
                    with WORKERS_LOCK:
                        for stale in busy_keys:
                            if stale in WORKERS:
                                WORKERS[stale].retire = True
                for stale_worker in retired:
                    stale_worker.stop()

                with WORKERS_LOCK:
                    worker = WORKERS.get(key)
                    if worker is not None and (
                        not worker.alive() or not worker_matches_intent(worker, conversation, continue_recent)
                    ):
                        # 活着的进程，但它停在别的会话上（预热的空进程 / 已经聊过几轮却要
                        # new_session / 想切到另一个 conversation）：不能直接拿它接着跑。
                        WORKERS.pop(key, None)
                        stale_worker = worker
                        worker = None
                    else:
                        stale_worker = None
                if stale_worker is not None:
                    stale_worker.stop()

                stream_args = ["--input-format", "stream-json", "--output-format", "stream-json"]
                digest: Optional[str] = None
                if handoff and (worker is not None or conversation):
                    if worker is None:
                        worker, created = start_registered_worker(
                            key,
                            stream_args + session_flags(args, conversation, False),
                            workspace,
                        )
                        if created:
                            notes.append(
                                "started a long-lived Antigravity session process; later calls reuse it "
                                "(set AGY_MCP_TRANSPORT=oneshot to force one process per call)"
                            )
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
                    with WORKERS_LOCK:
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
                    worker, created = start_registered_worker(
                        key,
                        stream_args + start_flags,
                        workspace,
                    )
                    if created:
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
                total_input = int(usage_tokens.get("input_tokens", 0) or 0)
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

            with state_guard():
                # 重新读一次：`AGY_MCP_MAX_PARALLEL > 1` 时别的会话可能刚写过计数
                state = read_state()
                same_day = state.get("day") == _today()
                write_state(
                    {
                        "day": _today(),
                        "calls": calls_today(state) + (1 if succeeded else 0),
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
    """跑一条只读的 CLI 子命令并把输出原样返回。"""
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


def tool_submit(args: Dict[str, Any]) -> Dict[str, Any]:
    """在后台跑一轮，让长作业不阻塞客户端；结果靠 antigravity_job 回收。"""
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
    state = str(record.get("state") or "running")
    summary: Dict[str, Any] = {"job_id": job_id, "state": state}
    if state == "running":
        summary["hint"] = "poll with antigravity_job; the job keeps running while you do other work"
        return text_result(json.dumps(summary, ensure_ascii=False))
    # 起进程就失败的情况：别让调用方去轮询一个永远不会产出结果的作业
    blocks = (record.get("result") or {}).get("content") or []
    summary["error"] = blocks[0].get("text") if blocks else "could not start the job"
    return text_result(json.dumps(summary, ensure_ascii=False), True)


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
                for path in (_job_path(job_id), _job_output_path(job_id)):
                    _remove_quietly(path)
            else:
                for name in os.listdir(JOBS_DIR):
                    if name.endswith(".json") or name.endswith(".out") or name.endswith(".tmp"):
                        _remove_quietly(os.path.join(JOBS_DIR, name))
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


HANDLERS = {
    "antigravity_ask": tool_ask,
    "antigravity_models": tool_models,
    "antigravity_agents": lambda args: tool_simple(["agent"], "agent"),
    "antigravity_sessions": tool_sessions,
    "antigravity_quota": tool_quota,
    "antigravity_submit": tool_submit,
    "antigravity_job": tool_job,
    "antigravity_status": tool_status,
}
