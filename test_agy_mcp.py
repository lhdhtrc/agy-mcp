#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 agy-mcp contributors
"""agy-mcp 的离线测试：不需要网络、不需要 Antigravity 额度、不需要装 `agy`。

可以直接跑（`python3 test_agy_mcp.py`），也能用 pytest 跑。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = tempfile.mkdtemp(prefix="agy-mcp-test-")
os.environ["AGY_MCP_STATE_DIR"] = STATE_DIR
os.environ["AGY_MCP_MIN_INTERVAL_SEC"] = "0"
sys.path.insert(0, HERE)

# 导入包本身当门面：需要猴补丁的状态（如 INSTANCE_ID）打在下面两个拥有它的模块上
import core as agy_mcp  # noqa: E402  (import after the state dir is set)
import core.quota  # noqa: E402 —— 补丁打在拥有该状态的模块上
import core.session  # noqa: E402 —— 补丁要打在拥有该状态的模块上

# Antigravity CLI 的替身：既能说 print 模式（`-p ... --output-format json`），
# 也能说常驻会话进程用的 stream 传输。这样整套轮次协议都能离线测试，
# 不需要网络、账号，也不需要真的装 agy。
FAKE_AGY = r'''
import json, sys, time, uuid

# 服务器在管道上用 UTF-8；别让替身进程按本地编码去解。
sys.stdin.reconfigure(encoding="utf-8", errors="replace")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

argv = sys.argv[1:]
conversation = "conv-" + uuid.uuid4().hex[:8]
turns = 0

if "--version" in argv:
    print("9.9.9")
    sys.exit(0)
if argv[:1] == ["models"]:
    print("fake-model-a\tFake A")
    sys.exit(0)

if "stream-json" in argv and "--input-format" in argv:
    emit({"event": "init", "conversation_id": conversation, "init": {"cwd": "."}})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception:
            continue
        content = str((message.get("message") or {}).get("content", ""))
        turns += 1
        if content.startswith("slow:"):
            # 代表一个很长的 agent 轮次；取消必须能把它杀掉
            time.sleep(float(os.environ.get("FAKE_AGY_SLOW_SEC", "60")))
        if content.startswith("fail:"):
            # 代表"轮次跑了但状态不是 SUCCESS"：用量记账必须还能落盘
            emit({"event": "result", "result": {
                "conversation_id": conversation, "status": "ERROR", "error": "fake failure",
                "num_turns": turns, "usage": {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5}}})
            continue
        emit({"event": "step_update", "step_update": {
            "step_index": turns, "state": "ACTIVE", "step_type": "agent_response",
            "text_delta": "partial answer " + str(turns),
        }})
        emit({"event": "result", "result": {
            "conversation_id": conversation,
            "status": "SUCCESS",
            "response": "echo: " + content,
            "num_turns": turns,
            "usage": {"input_tokens": 10 * turns, "output_tokens": 1,
                      "total_tokens": 10 * turns + 1},
        }})
    sys.exit(0)

prompt = ""
if "-p" in argv:
    prompt = argv[argv.index("-p") + 1]
if prompt.startswith("fail:"):
    emit({"conversation_id": conversation, "status": "ERROR", "error": "fake failure",
          "num_turns": 1, "usage": {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5}})
    sys.exit(0)
emit({"conversation_id": conversation, "status": "SUCCESS", "response": "echo: " + prompt,
      "num_turns": 1, "usage": {"input_tokens": 7, "output_tokens": 1, "total_tokens": 8}})
'''


def _fake_cli() -> str:
    path = os.path.join(STATE_DIR, "fake_agy.py")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(FAKE_AGY)
    return path


def run_server_messages(requests: list, extra_env: dict) -> list:
    """像客户端一样用 stdio 驱动 MCP 服务器，返回它发回的所有消息。"""
    env = {
        **os.environ,
        "AGY_MCP_STATE_DIR": STATE_DIR,
        "AGY_MCP_MIN_INTERVAL_SEC": "0",
        "AGY_MCP_AGY_CMD": f'"{sys.executable}" "{_fake_cli()}"',
        **extra_env,
    }
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "agy_mcp.py")],
        input="\n".join(json.dumps(request) for request in requests),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=HERE,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    messages = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            messages.append(json.loads(line))
    return messages


def run_server(requests: list, extra_env: dict) -> dict:
    """驱动 MCP 服务器，返回 {请求 id: 响应}。"""
    return {
        message["id"]: message
        for message in run_server_messages(requests, extra_env)
        if "id" in message
    }


def ask(request_id: int, prompt: str, **args) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "antigravity_ask", "arguments": {"prompt": prompt, **args}},
    }


def call(request_id: int, tool: str, **args) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }


def read_store(state_dir: str) -> dict:
    with open(os.path.join(state_dir, "sessions.json"), encoding="utf-8") as handle:
        return json.load(handle)


def stored_conversation_ids(state_dir: str) -> set:
    store = read_store(state_dir)
    return {
        entry["conversation_id"]
        for instance in store["instances"].values()
        for entry in instance["sessions"].values()
    }


def latest_session_entry(state_dir: str) -> dict:
    """最近写过盘的那个实例的会话记录。"""
    store = read_store(state_dir)
    instances = [data for data in store["instances"].values() if data.get("sessions")]
    newest = max(instances, key=lambda data: float(data.get("last_seen") or 0))
    return next(iter(newest["sessions"].values()))


def _dead_pid() -> int:
    """一个确定已经不存在的 pid（用来伪造"服务器被强杀"留下的记录）。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write_worker_record(instance: str, worker_pid: int, server_pid) -> None:
    """直接写一条 workers.json 记录：某个实例的会话进程 pid + 它自己的服务器进程 pid。"""
    core.session._write_worker_records(
        {instance: {"pid": server_pid, "pids": [worker_pid], "last_seen": time.time()}}
    )


def _server_env(extra: dict) -> dict:
    return {
        **os.environ,
        "AGY_MCP_STATE_DIR": STATE_DIR,
        "AGY_MCP_MIN_INTERVAL_SEC": "0",
        "AGY_MCP_AGY_CMD": f'"{sys.executable}" "{_fake_cli()}"',
        **extra,
    }


def start_live_server(extra_env: dict = None) -> tuple:
    """起一个**保持 stdin 打开**的服务器（模拟一个还在用的 MCP 实例）。"""
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "agy_mcp.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=HERE,
        env=_server_env(extra_env or {}),
    )
    lines: "queue.Queue[str]" = queue.Queue()

    def pump() -> None:
        for line in proc.stdout:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    return proc, lines


def live_call(proc, lines, request_id: int, tool: str, **args) -> dict:
    """向一个活着的服务器发一条请求并等它的响应（超时即失败，不挂测试）。"""
    proc.stdin.write(json.dumps(call(request_id, tool, **args)) + "\n")
    proc.stdin.flush()
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            line = lines.get(timeout=1.0)
        except queue.Empty:
            if proc.poll() is not None:
                raise AssertionError("the MCP server exited early")
            continue
        line = line.strip()
        if not line.startswith("{"):
            continue
        message = json.loads(line)
        if message.get("id") == request_id:
            return message
    raise AssertionError(f"no response for request {request_id}")


def _stop(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.kill()
    proc.wait(timeout=30)


def test_extract_answer() -> None:
    assert agy_mcp.extract_answer({"response": "PONG\n", "conversation_id": "x"}) == "PONG"
    # 被沙箱拒绝的轮次会返回 SUCCESS + 空回答：绝不能退回去取元数据当答案。
    denied = {
        "conversation_id": "982a645c-81fe-4a2a-b72c-470709b6a6fa",
        "status": "SUCCESS",
        "response": "",
        "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
    }
    assert agy_mcp.extract_answer(denied) is None
    assert agy_mcp.extract_answer({"result": {"response": "nested"}}) == "nested"
    assert agy_mcp.extract_answer({"content": [{"text": "a"}, {"text": "b"}]}) == "a\nb"
    assert agy_mcp.extract_answer({"status": "SUCCESS", "usage": {"input_tokens": 1}}) is None


def test_seed_prompt() -> None:
    assert agy_mcp.seed_prompt("do it", None) == "do it"
    seeded = agy_mcp.seed_prompt("do it", "  remembered: VIOLET-7391  ")
    assert "VIOLET-7391" in seeded and "do it" in seeded and seeded.index("VIOLET") < seeded.index("do it")


def test_merge_usage() -> None:
    total: dict = {}
    agy_mcp.merge_usage(total, {"usage": {"input_tokens": 10, "output_tokens": 2}})
    agy_mcp.merge_usage(total, {"usage": {"input_tokens": 5, "output_tokens": 1}})
    agy_mcp.merge_usage(total, None)
    assert total == {"input_tokens": 15, "output_tokens": 3}


def test_sessions_are_scoped_per_instance() -> None:
    sessions = {"default": {"conversation_id": "conversation-1", "workspace": "/tmp"}}
    agy_mcp.write_sessions(sessions)
    assert agy_mcp.read_sessions()["default"]["conversation_id"] == "conversation-1"

    store = json.load(open(agy_mcp.SESSIONS_PATH, encoding="utf-8"))
    assert "instances" in store, "store must be namespaced by server instance"

    # 第二个实例（另一个客户端会话）在第一个仍活跃时不得继承它的会话。
    real_id = core.session.INSTANCE_ID
    try:
        core.session.INSTANCE_ID = "other-instance-1"
        assert agy_mcp.read_sessions() == {}
    finally:
        core.session.INSTANCE_ID = real_id

    # 陈旧实例会被接管，所以重启后的服务器仍能接着原来的会话。
    store["instances"][real_id]["last_seen"] = 0
    with open(agy_mcp.SESSIONS_PATH, "w", encoding="utf-8") as handle:
        json.dump(store, handle)
    try:
        core.session.INSTANCE_ID = "other-instance-2"
        assert agy_mcp.read_sessions()["default"]["conversation_id"] == "conversation-1"
    finally:
        core.session.INSTANCE_ID = real_id


def test_resolve_agy_honours_env() -> None:
    with tempfile.NamedTemporaryFile(suffix="agy") as handle:
        os.environ["AGY_BIN"] = handle.name
        try:
            assert agy_mcp.resolve_agy() == handle.name
        finally:
            os.environ.pop("AGY_BIN", None)


def test_disabled_actions_become_an_error() -> None:
    payload = {
        "conversation_id": "c",
        "status": "SUCCESS",
        "response": "",
        "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
    }
    assert agy_mcp.extract_answer(payload) is None  # the tool layer turns this into isError


def test_mcp_handshake_and_tool_list() -> None:
    """完全按客户端的方式用 stdio 驱动服务器；这一条不会真的调用 `agy`。"""
    responses = run_server(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ],
        {},
    )

    init = responses[1]["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["serverInfo"]["name"] == "antigravity"
    assert init["capabilities"]["tools"] == {"listChanged": False}

    names = [tool["name"] for tool in responses[2]["result"]["tools"]]
    assert names == [
        "antigravity_ask",
        "antigravity_models",
        "antigravity_agents",
        "antigravity_sessions",
        "antigravity_quota",
        "antigravity_submit",
        "antigravity_job",
        "antigravity_status",
    ], names
    ask = next(tool for tool in responses[2]["result"]["tools"] if tool["name"] == "antigravity_ask")
    assert ask["inputSchema"]["required"] == ["prompt"]
    assert "handoff" in ask["inputSchema"]["properties"]
    assert responses[3]["result"] == {}


def test_stream_transport_reuses_one_conversation() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-stream-") as state:
        responses = run_server(
            [
                ask(1, "first", session="stream-check"),
                ask(2, "second", session="stream-check"),
            ],
            {"AGY_MCP_STATE_DIR": state, "AGY_MCP_TRANSPORT": "stream"},
        )
        assert responses[1]["result"]["content"][0]["text"] == "echo: first"
        assert responses[2]["result"]["content"][0]["text"] == "echo: second"
        sessions = read_store(state)
        tracked = [entry for inst in sessions["instances"].values() for entry in inst["sessions"].values()]
        assert len({entry["conversation_id"] for entry in tracked}) == 1, tracked
        assert tracked[0]["num_turns"] == 2, tracked


def test_oneshot_transport_still_answers() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-oneshot-") as state:
        responses = run_server(
            [ask(1, "hello", session="oneshot-check")],
            {"AGY_MCP_STATE_DIR": state, "AGY_MCP_TRANSPORT": "oneshot"},
        )
        assert responses[1]["result"]["content"][0]["text"] == "echo: hello"


def test_handoff_starts_a_new_conversation_with_the_digest() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-handoff-") as state:
        # 两次独立的客户端调用：第二次必须接管上一次的会话
        # （测试里用 AGY_MCP_INSTANCE_WINDOW_SEC=0 强制接管）。
        env = {
            "AGY_MCP_STATE_DIR": state,
            "AGY_MCP_TRANSPORT": "stream",
            "AGY_MCP_INSTANCE_WINDOW_SEC": "0",
        }
        run_server([ask(1, "remember VIOLET-7391", session="handoff-check")], env)
        before = stored_conversation_ids(state)
        assert len(before) == 1, before

        responses = run_server(
            [ask(1, "what was the token?", session="handoff-check", handoff=True)], env
        )
        after = stored_conversation_ids(state)
        assert after - before, f"handoff must land in a new conversation: {before} -> {after}"

        answer = responses[1]["result"]["content"][0]["text"]
        # 摘要轮跑在旧会话上，随后把摘要作为前情提要喂给新会话。
        assert "echo: Summarize the conversation above" in answer, answer
        assert answer.endswith("what was the token?"), answer


def test_cancelled_turn_is_dropped_and_frees_the_session() -> None:
    """客户端按 Esc 会发 notifications/cancelled：必须真的停掉这一轮，而不是当没看见。"""
    with tempfile.TemporaryDirectory(prefix="agy-mcp-cancel-") as state:
        env = {
            **os.environ,
            "AGY_MCP_STATE_DIR": state,
            "AGY_MCP_MIN_INTERVAL_SEC": "0",
            "AGY_MCP_TRANSPORT": "stream",
            "AGY_MCP_AGY_CMD": f'"{sys.executable}" "{_fake_cli()}"',
        }
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "agy_mcp.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=HERE,
            env=env,
            bufsize=1,
        )

        def write(payload: dict) -> None:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

        try:
            write({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
            write(ask(1, "slow: think for a while", session="cancel-check", timeout_sec=120))
            time.sleep(1.5)
            started = time.time()
            write({
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 1, "reason": "user"},
            })
            write(ask(2, "quick", session="cancel-check", timeout_sec=60))
            assert proc.stdin is not None
            proc.stdin.close()
            out, _ = proc.communicate(timeout=90)
            elapsed = time.time() - started
        finally:
            if proc.poll() is None:
                proc.kill()

        responses = {}
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                payload = json.loads(line)
                if "id" in payload:
                    responses[payload["id"]] = payload

        assert 1 not in responses, f"a cancelled request must not answer: {responses.get(1)}"
        assert responses[2]["result"]["content"][0]["text"] == "echo: quick"
        assert elapsed < 30, f"cancel did not interrupt the slow turn ({elapsed:.1f}s)"
        assert proc.returncode == 0


def test_progress_notifications_are_emitted() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-progress-") as state:
        request = ask(1, "hello", session="progress-check")
        request["params"]["_meta"] = {"progressToken": "tok-42"}
        messages = run_server_messages(
            [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}, request],
            {"AGY_MCP_STATE_DIR": state, "AGY_MCP_PROGRESS_INTERVAL_MS": "0"},
        )
        updates = [
            message
            for message in messages
            if message.get("method") == "notifications/progress"
        ]
        assert updates, "expected at least one progress notification"
        assert all(u["params"]["progressToken"] == "tok-42" for u in updates)
        assert any("queued" in str(u["params"]["message"]) for u in updates)
        assert any(u["params"]["progress"] >= 1 for u in updates), updates
        # 流式文字片段会被转发出去，客户端据此显示回答在逐步生成。
        assert any("agent_response" in str(u["params"]["message"]) for u in updates), updates
        assert any("partial answer" in str(u["params"]["message"]) for u in updates), updates


def test_self_test_reports_ready() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-selftest-") as state:
        env = {
            **os.environ,
            "AGY_MCP_STATE_DIR": state,
            "AGY_MCP_AGY_CMD": f'"{sys.executable}" "{_fake_cli()}"',
        }
        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "agy_mcp.py"),
                "--self-test",
                "--no-proxy-required",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            cwd=HERE,
            env=env,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "self-test OK" in proc.stdout, proc.stdout
        assert "live turn" in proc.stdout, proc.stdout


def test_auto_handoff_compacts_a_long_conversation() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-auto-") as state:
        env = {
            "AGY_MCP_STATE_DIR": state,
            # 替身第一轮上报 10 个 input token，所以把阈值设成 5 就能让
            # 下一次调用自动压缩。
            "AGY_MCP_AUTO_HANDOFF": "1",
            "AGY_MCP_LONG_CONTEXT_TOKENS": "5",
            "AGY_MCP_INSTANCE_WINDOW_SEC": "0",
        }
        run_server([ask(1, "remember VIOLET-7391", session="auto-check")], env)
        before = stored_conversation_ids(state)

        responses = run_server([ask(1, "status?", session="auto-check")], env)
        after = stored_conversation_ids(state)
        assert after - before, f"auto-handoff must start a new conversation: {before} -> {after}"
        answer = responses[1]["result"]["content"][0]["text"]
        assert "echo: Summarize the conversation above" in answer, answer
        assert any("auto-handoff" in note["text"] for note in responses[1]["result"]["content"][1:])


def test_defaults_auto_approve_and_keep_the_sandbox() -> None:
    flags = agy_mcp.session_flags({}, None, False)
    assert "--dangerously-skip-permissions" in flags
    assert "--sandbox" in flags
    assert "--dangerously-skip-permissions" not in agy_mcp.session_flags(
        {"skip_permissions": False}, None, False
    )


def test_files_parameter_is_prepended_to_the_prompt() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-files-") as state:
        responses = run_server(
            [ask(1, "review it", session="files-check", files=["/tmp/a.py", "/tmp/b.py"])],
            {"AGY_MCP_STATE_DIR": state},
        )
        text = responses[1]["result"]["content"][0]["text"]
        assert "/tmp/a.py" in text and "/tmp/b.py" in text, text
        assert text.endswith("review it"), text


def test_prompt_size_guard() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-guard-") as state:
        responses = run_server(
            [ask(1, "x" * 500, session="guard-check")],
            {"AGY_MCP_STATE_DIR": state, "AGY_MCP_MAX_PROMPT_CHARS": "100"},
        )
        result = responses[1]["result"]
        assert result["isError"] is True
        assert "AGY_MCP_MAX_PROMPT_CHARS" in result["content"][0]["text"]


def test_models_are_returned_structured() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-models-") as state:
        responses = run_server([call(1, "antigravity_models")], {"AGY_MCP_STATE_DIR": state})
        payload = json.loads(responses[1]["result"]["content"][0]["text"])
        assert payload["models"][0] == {"id": "fake-model-a", "label": "Fake A"}, payload


def test_sessions_record_token_totals() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-tokens-") as state:
        run_server([ask(1, "hello", session="token-check")], {"AGY_MCP_STATE_DIR": state})
        store = read_store(state)
        entry = [e for inst in store["instances"].values() for e in inst["sessions"].values()][0]
        assert entry["input_tokens"] == 10 and entry["output_tokens"] == 1, entry


def test_orphaned_workers_are_reaped() -> None:
    """被强杀的服务器会留下会话进程；下次启动要把它们清掉。"""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.5)"])
    try:
        # 记录归属的服务器进程已经不在了 —— 这才是"孤儿"
        _write_worker_record("server-that-died", sleeper.pid, _dead_pid())
        original = core.session._is_agy_process
        core.session._is_agy_process = lambda pid: True  # 守卫逻辑另有用例覆盖
        try:
            agy_mcp.reap_orphan_workers()
        finally:
            core.session._is_agy_process = original
        time.sleep(1.0)
        assert sleeper.poll() is not None, "orphaned session process should have been killed"
        assert agy_mcp._read_worker_pids() == []
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def test_a_live_instance_keeps_its_workers() -> None:
    """另一个还活着的 MCP 实例（= 另一条 Codex 线程）的会话进程不是孤儿，不能动。"""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.5)"])
    try:
        # 服务器进程就是本测试进程：还活着
        _write_worker_record("another-live-thread", sleeper.pid, os.getpid())
        original = core.session._is_agy_process
        core.session._is_agy_process = lambda pid: True
        try:
            agy_mcp.reap_orphan_workers()
        finally:
            core.session._is_agy_process = original
        time.sleep(0.5)
        assert sleeper.poll() is None, "a live instance's session process must not be reaped"
        assert sleeper.pid in agy_mcp._read_worker_pids(), "its pid record must survive too"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def test_orphan_reaping_never_kills_unrelated_processes() -> None:
    sleeper = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.5)"])
    try:
        _write_worker_record("server-that-died", sleeper.pid, _dead_pid())
        agy_mcp.reap_orphan_workers()  # real guard: this pid is not the Antigravity CLI
        time.sleep(0.5)
        assert sleeper.poll() is None, "must not kill a process that is not agy"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def test_a_second_thread_does_not_break_the_first_one() -> None:
    """第二个 Codex 线程（= 第二个 MCP 实例）启动时，不能把第一个线程的热会话进程当孤儿杀掉。

    这是最实际的并行场景：workers.json 是所有实例共用的，谁都不该动"服务器还活着"的租。
    """
    first, first_lines = start_live_server()
    second = None
    third = None
    try:
        live_call(first, first_lines, 1, "antigravity_ask", prompt="a")
        second, second_lines = start_live_server()  # 启动时就会跑一次孤儿回收
        live_call(second, second_lines, 1, "antigravity_ask", prompt="b")

        # 第一个线程的常驻进程如果被杀，这一轮就会重新冷启动（假 CLI 里轮次从 1 重新数）
        text = live_call(
            first, first_lines, 2, "antigravity_ask", prompt="c", output_format="json"
        )["result"]["content"][0]["text"]
        assert json.loads(text)["num_turns"] == 2, "the first thread lost its warm session process"

        owners = {record.get("pid") for record in core.session._read_worker_records().values()}
        assert {first.pid, second.pid} <= owners, f"both live instances should be tracked: {owners}"

        # 第一个实例被强杀（来不及清理自己）后，下一个实例启动时只该清掉它的记录
        _stop(first)
        third, third_lines = start_live_server()
        # 等它真正跑完启动时的孤儿回收（服务器是先回收再读 stdin 的）
        live_call(third, third_lines, 1, "antigravity_ask", prompt="d")
        owners = {record.get("pid") for record in core.session._read_worker_records().values()}
        assert first.pid not in owners, "the killed instance's record should be reaped"
        assert second.pid in owners, "the live instance's record must be left alone"
    finally:
        _stop(first)
        _stop(second)
        _stop(third)


def _make_repo_with_change() -> str:
    """一次性 git 仓库：一个已提交的文件，外加一处未提交的改动。"""
    repo = tempfile.mkdtemp(prefix="agy-mcp-repo-")
    identity = ["-c", "user.email=test@example.com", "-c", "user.name=test"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    path = os.path.join(repo, "demo.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("value = 1\n")
    subprocess.run(["git"] + identity + ["add", "demo.py"], cwd=repo, check=True)
    subprocess.run(["git"] + identity + ["commit", "-qm", "init"], cwd=repo, check=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("added_line = 42\n")
    return repo


def test_diff_is_captured_locally_and_attached() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-diff-") as state:
        repo = _make_repo_with_change()
        responses = run_server(
            [ask(1, "review it", session="diff-check", cwd=repo, diff=True)],
            {"AGY_MCP_STATE_DIR": state},
        )
        text = responses[1]["result"]["content"][0]["text"]
        assert "added_line = 42" in text, text[:400]
        assert "demo.py" in text, text[:400]
        assert text.endswith("review it"), text[-200:]
        notes = [item["text"] for item in responses[1]["result"]["content"][1:]]
        assert any("attached the local git diff" in note for note in notes), notes


def test_diff_on_a_non_repo_is_reported_not_fatal() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-norepo-") as plain:
        with tempfile.TemporaryDirectory(prefix="agy-mcp-diff2-") as state:
            responses = run_server(
                [ask(1, "hello", session="norepo-check", cwd=plain, diff=True)],
                {"AGY_MCP_STATE_DIR": state},
            )
            result = responses[1]["result"]
            assert result["isError"] is False, result
            assert result["content"][0]["text"] == "echo: hello"
            notes = [item["text"] for item in result["content"][1:]]
            assert any("not a git working tree" in note for note in notes), notes


def _quota_payload(gemini_percent: float, third_party_percent: float) -> dict:
    return {
        "command": {
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {"id": "gemini-5h", "name": "5h", "remaining_fraction": gemini_percent / 100},
                            {"id": "gemini-weekly", "name": "weekly", "remaining_fraction": 1.0},
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": [
                            {
                                "id": "3p-5h",
                                "name": "5h",
                                "remaining_fraction": third_party_percent / 100,
                            }
                        ],
                    },
                ]
            }
        }
    }


def test_effort_is_reconciled_with_the_model_id() -> None:
    """CLI 不接受 `--model gemini-3.8-flash-high --effort low`，所以两者要保持一致。"""
    model, effort, notes = core.quota.reconcile_model_and_effort("gemini-3.8-flash-high", "low", False)
    assert (model, effort) == ("gemini-3.8-flash-low", "low"), (model, effort)
    assert any("gemini-3.8-flash-low" in note for note in notes), notes

    # 本来就一致：模型与强度都原样透传
    assert core.quota.reconcile_model_and_effort("gemini-3.8-flash-low", "low", True)[:2] == (
        "gemini-3.8-flash-low",
        "low",
    )

    # 没有强度后缀的模型系列：保留模型、放弃 effort
    model, effort, notes = core.quota.reconcile_model_and_effort("claude-sonnet-4-6", "low", True)
    assert (model, effort) == ("claude-sonnet-4-6", None)
    assert any("ignored" in note for note in notes), notes

    # 完全没有模型：只传 effort 也可以
    assert core.quota.reconcile_model_and_effort(None, "low", False) == (None, "low", [])


def test_effort_change_is_sticky_for_the_session() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-effort-") as state:
        # 真实客户端只保留一个服务器进程；这里用窗口设置强制走同一模型。
        env = {"AGY_MCP_STATE_DIR": state, "AGY_MCP_INSTANCE_WINDOW_SEC": "0"}
        first = run_server([ask(1, "hello", session="effort-check", effort="low")], env)
        notes = [item["text"] for item in first[1]["result"]["content"][1:]]
        assert any("gemini-3.8-flash-low" in note for note in notes), notes

        # 这次不传 effort：会话沿用它上面设过的值
        second = run_server([ask(1, "again", session="effort-check")], env)
        entry = latest_session_entry(state)
        assert entry["effort"] == "low", entry
        assert entry["model"] == "gemini-3.8-flash-low", entry
        assert second[1]["result"]["isError"] is False

        # "default" 重新把它清掉
        run_server([ask(1, "clear it", session="effort-check", effort="default", model="default")], env)
        entry = latest_session_entry(state)
        # effort 已清除；模型回落到配置的默认值
        assert entry["effort"] is None, entry
        assert entry["model"] == agy_mcp.DEFAULT_MODEL_ID, entry


def test_stream_fixture_still_parses() -> None:
    """防止 CLI 协议漂移的回归守卫：录下来的真实流必须一直能解析。"""
    fixture = os.path.join(HERE, "tests", "fixtures", "stream_turn.ndjson")
    with open(fixture, encoding="utf-8") as handle:
        events = [agy_mcp.parse_stream_line(line) for line in handle]
    events = [event for event in events if event is not None]
    assert len(events) >= 4, events

    kinds = [event.get("event") for event in events]
    assert kinds[0] == "init" and kinds[-1] == "result", kinds
    assert "step_update" in kinds, kinds

    init = events[0]
    assert init.get("conversation_id"), init
    steps = [event["step_update"] for event in events if isinstance(event.get("step_update"), dict)]
    assert all(step.get("step_type") for step in steps), steps
    assert any(step.get("state") == "ACTIVE" for step in steps), steps

    result = events[-1]["result"]
    for key in ("conversation_id", "status", "response", "usage"):
        assert key in result, (key, result)
    assert result["status"] == "SUCCESS"
    assert agy_mcp.extract_answer(result) == "OK"

    # 常驻进程用的同一套辅助函数，必须能从这份 fixture 里推导出进度
    active = next(event for event in events if event.get("step_update", {}).get("state") == "ACTIVE")
    label, detail = agy_mcp.progress_from_event(active)
    assert "agent_response" in label, label
    assert detail == "OK", detail
    assert agy_mcp.progress_from_event(init) is None


def test_model_defaults_and_auto_selection() -> None:
    assert core.quota.resolve_model(None) == (agy_mcp.DEFAULT_MODEL_ID, [])
    assert core.quota.resolve_model("claude-sonnet-4-6") == ("claude-sonnet-4-6", [])

    models = "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-sonnet-4-6\tClaude Sonnet 4.6"
    original_models, original_quota = core.quota.cached_models, core.quota.read_quota
    core.quota.cached_models = lambda: (0, models)
    try:
        core.quota.QUOTA_CACHE.update({"ts": time.time(), "payload": _quota_payload(90, 5)})
        chosen, notes = core.quota.resolve_model("auto")
        assert chosen == "gemini-3.8-flash-high", (chosen, notes)
        assert any("headroom 90%" in note for note in notes), notes

        core.quota.QUOTA_CACHE.update({"ts": time.time(), "payload": _quota_payload(2, 80)})
        chosen, notes = core.quota.resolve_model("auto")
        assert chosen == "claude-sonnet-4-6", (chosen, notes)
    finally:
        core.quota.cached_models, core.quota.read_quota = original_models, original_quota
        core.quota.QUOTA_CACHE.update({"ts": 0.0, "payload": None})


def test_quota_warning_is_warn_only_and_cooldown_limited() -> None:
    original = dict(core.quota.QUOTA_CACHE)
    try:
        core.quota.QUOTA_CACHE.update({"ts": time.time(), "payload": _quota_payload(90, 90)})
        core.quota._QUOTA_WARNED_AT = 0.0
        assert core.quota.quota_warning() is None

        core.quota.QUOTA_CACHE.update({"ts": time.time(), "payload": _quota_payload(4, 90)})
        warning = core.quota.quota_warning()
        assert warning and "Gemini Models" in warning, warning
        # 冷却：同样的状态不能每次调用都唠叨一遍
        assert core.quota.quota_warning() is None
    finally:
        core.quota.QUOTA_CACHE.update(original)
        core.quota._QUOTA_WARNED_AT = 0.0


def test_new_session_starts_a_fresh_conversation() -> None:
    """`new_session` 必须真的换会话，而不是把常驻进程里的旧上下文接着用。"""
    messages = run_server_messages(
        [ask(1, "first"), ask(2, "second", new_session=True, output_format="json")],
        {"AGY_MCP_TRANSPORT": "stream"},
    )
    text = next(message["result"]["content"][0]["text"] for message in messages if message.get("id") == 2)
    payload = json.loads(text)
    # 假 CLI 每个进程自己数轮次：1 说明换了一个会话进程，2 说明续接了旧会话
    assert payload["num_turns"] == 1, f"new_session must start over, got {payload['num_turns']} turn(s)"


def test_prewarm_reuses_the_first_session_process() -> None:
    """预热过的进程要直接被第一次调用复用，而不是被当成"参数变了的旧进程"停掉。"""
    env = {
        **os.environ,
        "AGY_MCP_STATE_DIR": STATE_DIR,
        "AGY_MCP_MIN_INTERVAL_SEC": "0",
        "AGY_MCP_PREWARM": "1",
        "AGY_MCP_AGY_CMD": f'"{sys.executable}" "{_fake_cli()}"',
    }
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "agy_mcp.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=HERE,
        env=env,
    )
    try:
        time.sleep(2.0)  # 等预热把进程登记好（预热只是 Popen，远快于此）
        out, err = proc.communicate(json.dumps(ask(1, "hi")) + "\n", timeout=90)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert "prewarmed the default session process" in err, err
    assert "started a long-lived Antigravity session process" not in out, out


def test_prewarmed_process_is_not_used_to_resume_a_conversation() -> None:
    """预热的空进程没有 `--conversation`，拿它续接会静默丢掉历史；引擎必须认出这一点。"""
    args = core.tools.default_session_args()
    flags = core.tools.session_flags(args, None, False)
    worker = core.worker.Worker(
        core.tools.worker_key("default", "ws", args),
        ["--input-format", "stream-json", "--output-format", "stream-json"] + flags,
        "ws",
    )
    assert core.tools.worker_matches_intent(worker, None, False) is True  # 全新会话：能用
    assert core.tools.worker_matches_intent(worker, "conv-1", False) is False  # 续接：不能用
    assert core.tools.worker_matches_intent(worker, None, True) is False  # continue_session：也不能用
    # 聊过几轮的进程无法回到全新会话（new_session 的语义）
    worker.turns = 3
    worker.conversation_id = "conv-9"
    assert core.tools.worker_matches_intent(worker, None, False) is False
    assert core.tools.worker_matches_intent(worker, "conv-9", False) is True


def test_interrupted_turn_keeps_sticky_settings() -> None:
    """中途失败（超时 / 进程被杀）不能把会话粘住的 model、effort 与 token 累计抹掉。"""
    entry = {
        "conversation_id": "conv-sticky",
        "workspace": os.path.join(STATE_DIR, "sticky-ws"),
        "calls": 3,
        "num_turns": 5,
        "model": "gemini-3.8-flash-low",
        "effort": "low",
        "input_tokens": 120,
        "output_tokens": 12,
    }
    sessions = {"default": dict(entry)}
    worker = types.SimpleNamespace(conversation_id="conv-sticky", turns=6)
    core.session.remember_partial_turn(sessions, entry, "default", entry["workspace"], worker)
    merged = sessions["default"]
    assert merged["num_turns"] == 6 and "interrupted" in merged["last_error"]
    assert merged["model"] == "gemini-3.8-flash-low", "sticky model must survive an interrupted turn"
    assert merged["effort"] == "low", "sticky effort must survive an interrupted turn"
    assert (merged["input_tokens"], merged["output_tokens"]) == (120, 12)


def test_finished_jobs_do_not_block_orphan_cleanup() -> None:
    """进程早就没了的陈旧作业记录，不能把孤儿清理一直挡住。"""
    job_id = "job-stale-1"
    core.jobs.write_job(
        job_id,
        {
            "job_id": job_id,
            "state": "running",
            "pid": _dead_pid(),
            "out": core.jobs._job_output_path(job_id),
        },
    )
    try:
        assert core.jobs.running_job_count() == 0, "a dead job must not count as running"
    finally:
        core.jobs._remove_quietly(core.jobs._job_path(job_id))


def test_register_script_can_add_a_second_entry() -> None:
    """多账号要能注册第二条条目：`--id` 只影响自己那条，不碰别人的。"""
    import register_agy_mcp as reg

    with tempfile.TemporaryDirectory(prefix="agy-mcp-register-") as tmp:
        path = os.path.join(tmp, "config.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[mcp_servers.other]\ncommand = 'x'\n")

        block_b = reg.build_block(
            "/py", ["/tools/agy-mcp/agy_mcp.py"], {"A": "1"}, server_id="antigravity-b"
        )
        assert "[mcp_servers.antigravity-b]" in block_b
        assert reg.write_codex_config(path, block_b, False, False, "antigravity-b") == "updated"

        block_a = reg.build_block(
            "/py", ["/tools/agy-mcp/agy_mcp.py"], {}, server_id="antigravity"
        )
        reg.write_codex_config(path, block_a, False, False, "antigravity")

        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert "[mcp_servers.antigravity]" in text
        assert "[mcp_servers.antigravity-b]" in text, "第二条条目不能被第一条覆盖"
        assert "[mcp_servers.other]" in text
        assert reg.read_existing_env(path, "antigravity-b") == {"A": "1"}

        # 移除第一条时，第二条要留下
        reg.write_codex_config(path, "", True, False, "antigravity")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert "[mcp_servers.antigravity]" not in text
        assert "[mcp_servers.antigravity-b]" in text

        # 共享 Codex 工具时不要把我们自己的条目（任意 id）共享出去，否则递归
        entries = reg.codex_tool_entries(path, "antigravity-b")
        assert "antigravity-b" not in entries and "antigravity" not in entries
        assert "other" in entries


def test_failed_turn_reports_the_error_instead_of_crashing() -> None:
    """状态非 SUCCESS 的轮次要如实报错。

    曾经会因为"用量记账用的 total_input 只在成功分支里赋值"而抛 UnboundLocalError，
    用户看到的是内部错误、用量日志里也没有这条失败记录。
    """
    responses = run_server([ask(1, "fail:please")], {})
    result = responses[1]["result"]
    text = result["content"][0]["text"]
    assert result["isError"] is True, result
    assert "ERROR" in text and "fake failure" in text, text
    assert "cannot access local variable" not in text, text

    with open(os.path.join(STATE_DIR, "usage.jsonl"), encoding="utf-8") as handle:
        last = json.loads(handle.readlines()[-1])
    assert last["ok"] is False
    assert last["total_input_tokens"] == 5, last  # 失败轮次也要把用量记下来


def test_browser_hook_script_denies() -> None:
    """钩子脚本按 agy 的 PreToolUse 契约返回硬拒决定。"""
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "hooks", "deny_browser.py")],
        input='{"toolCall": {"name": "read_browser_page", "args": {}}, "stepIdx": 3}',
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["decision"] == "deny"
    assert payload["reason"].strip()


def test_register_script_installs_the_browser_hook() -> None:
    """--disable-browser 写/删 hooks.json：只动自己那条，别家的钩子和我方文件都要保住。"""
    import register_agy_mcp as reg

    with tempfile.TemporaryDirectory(prefix="agy-mcp-hooks-") as tmp:
        path = os.path.join(tmp, "hooks.json")
        args = (sys.executable, os.path.join(HERE, "hooks", "deny_browser.py"))

        assert reg.write_browser_hook(path, *args, remove=False, dry_run=False) == "updated"
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        assert reg.BROWSER_HOOK_NAME in data
        entry = data[reg.BROWSER_HOOK_NAME]["PreToolUse"][0]
        assert "browser" in entry["matcher"] and "playwright" in entry["matcher"]
        assert "deny_browser.py" in entry["hooks"][0]["command"]
        # 命令必须是 cmd 能直接跑的形式：带引号会被 cmd 拆坏，而"钩子跑不起来"= 每一轮都被判死
        command = entry["hooks"][0]["command"]
        if os.name == "nt":
            assert '"' not in command, command
        assert reg.verify_hook_command(command)[0] is True, command
        assert reg.verify_hook_command(f'"{sys.executable}" "x.py"')[0] is (os.name != "nt")

        # 幂等：内容一样就不再写
        assert reg.write_browser_hook(path, *args, remove=False, dry_run=False) == "unchanged"

        # 手工写的钩子必须原样保留
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"someone-else": {"enabled": False}}, handle)
        reg.write_browser_hook(path, *args, remove=False, dry_run=False)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        assert "someone-else" in data and reg.BROWSER_HOOK_NAME in data

        # 卸载只摘自己那条
        assert reg.write_browser_hook(path, *args, remove=True, dry_run=False) == "updated"
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        assert reg.BROWSER_HOOK_NAME not in data and "someone-else" in data

        # 只剩自己一条时，卸载把文件删掉
        reg.write_browser_hook(path, *args, remove=False, dry_run=False)
        assert os.path.exists(path)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({reg.BROWSER_HOOK_NAME: {}}, handle)
        assert reg.write_browser_hook(path, *args, remove=True, dry_run=False) == "removed"
        assert not os.path.exists(path)

        # 文件不是 JSON 时拒绝动手
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        try:
            reg.write_browser_hook(path, *args, remove=False, dry_run=False)
        except SystemExit:
            pass
        else:
            raise AssertionError("must refuse to rewrite a non-JSON hooks.json")


def test_read_only_tools_do_not_queue_behind_a_turn() -> None:
    """长轮次还在跑的时候 `models` 就得能回答，而不是排在它后面。"""
    with tempfile.TemporaryDirectory(prefix="agy-mcp-fast-") as state:
        env = {
            **os.environ,
            "AGY_MCP_STATE_DIR": state,
            "AGY_MCP_MIN_INTERVAL_SEC": "0",
            "AGY_MCP_TRANSPORT": "stream",
            "AGY_MCP_AGY_CMD": f'"{sys.executable}" "{_fake_cli()}"',
            "FAKE_AGY_SLOW_SEC": "4",
        }
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "agy_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", cwd=HERE, env=env, bufsize=1,
        )
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(ask(1, "slow: take your time", session="fast-check")) + "\n")
            proc.stdin.flush()
            time.sleep(1.0)
            proc.stdin.write(json.dumps(call(2, "antigravity_models")) + "\n")
            proc.stdin.flush()
            proc.stdin.close()
            out, _ = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()

        order = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                payload = json.loads(line)
                if "id" in payload:
                    order.append(payload["id"])
        assert order[:2] == [2, 1], f"models should answer before the slow turn finishes: {order}"


TESTS = (
    test_extract_answer,
    test_seed_prompt,
    test_merge_usage,
    test_sessions_are_scoped_per_instance,
    test_resolve_agy_honours_env,
    test_disabled_actions_become_an_error,
    test_mcp_handshake_and_tool_list,
    test_stream_transport_reuses_one_conversation,
    test_oneshot_transport_still_answers,
    test_handoff_starts_a_new_conversation_with_the_digest,
    test_cancelled_turn_is_dropped_and_frees_the_session,
    test_progress_notifications_are_emitted,
    test_auto_handoff_compacts_a_long_conversation,
    test_self_test_reports_ready,
    test_defaults_auto_approve_and_keep_the_sandbox,
    test_files_parameter_is_prepended_to_the_prompt,
    test_prompt_size_guard,
    test_models_are_returned_structured,
    test_sessions_record_token_totals,
    test_orphaned_workers_are_reaped,
    test_a_live_instance_keeps_its_workers,
    test_orphan_reaping_never_kills_unrelated_processes,
    test_a_second_thread_does_not_break_the_first_one,
    test_diff_is_captured_locally_and_attached,
    test_diff_on_a_non_repo_is_reported_not_fatal,
    test_model_defaults_and_auto_selection,
    test_stream_fixture_still_parses,
    test_effort_is_reconciled_with_the_model_id,
    test_effort_change_is_sticky_for_the_session,
    test_quota_warning_is_warn_only_and_cooldown_limited,
    test_new_session_starts_a_fresh_conversation,
    test_prewarm_reuses_the_first_session_process,
    test_prewarmed_process_is_not_used_to_resume_a_conversation,
    test_interrupted_turn_keeps_sticky_settings,
    test_finished_jobs_do_not_block_orphan_cleanup,
    test_register_script_can_add_a_second_entry,
    test_failed_turn_reports_the_error_instead_of_crashing,
    test_browser_hook_script_denies,
    test_register_script_installs_the_browser_hook,
    test_read_only_tools_do_not_queue_behind_a_turn,
)


def main() -> int:
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc!r}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
