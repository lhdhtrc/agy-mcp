#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 agy-mcp contributors
"""Offline tests for agy-mcp: no network, no Antigravity quota, no `agy` required.

Run directly (`python3 test_agy_mcp.py`) or through pytest.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = tempfile.mkdtemp(prefix="agy-mcp-test-")
os.environ["AGY_MCP_STATE_DIR"] = STATE_DIR
os.environ["AGY_MCP_MIN_INTERVAL_SEC"] = "0"
sys.path.insert(0, HERE)

import agy_mcp  # noqa: E402  (import after the state dir is set)

# A stand-in for the Antigravity CLI: speaks print mode (`-p ... --output-format json`) and
# the stream transport used by the resident session process. Lets the whole turn protocol be
# tested offline, with no network, no account and no `agy` installed.
FAKE_AGY = r'''
import json, sys, time, uuid

# The server speaks UTF-8 on the pipes; do not let the fixture's locale decode it differently.
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
            time.sleep(60)  # stands in for a long agent turn; a cancel must kill it
        emit({"event": "step_update", "step_type": "text", "step_index": turns})
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
    """Drive the MCP server over stdio and return every parsed message it sent."""
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
    """Drive the MCP server and return {request id: response}."""
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


def tracked_conversation_ids(response: dict) -> set:
    report = json.loads(response["result"]["content"][0]["text"])
    return {entry["conversation_id"] for entry in report["tracked_sessions"]}


def test_extract_answer() -> None:
    assert agy_mcp.extract_answer({"response": "PONG\n", "conversation_id": "x"}) == "PONG"
    # A sandbox-denied turn is SUCCESS with an empty response: never fall back to metadata.
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

    # A second instance (another client thread) must not inherit while the first looks alive.
    real_id = agy_mcp.INSTANCE_ID
    try:
        agy_mcp.INSTANCE_ID = "other-instance-1"
        assert agy_mcp.read_sessions() == {}
    finally:
        agy_mcp.INSTANCE_ID = real_id

    # A stale instance is adopted, so a restarted server keeps its conversation.
    store["instances"][real_id]["last_seen"] = 0
    with open(agy_mcp.SESSIONS_PATH, "w", encoding="utf-8") as handle:
        json.dump(store, handle)
    try:
        agy_mcp.INSTANCE_ID = "other-instance-2"
        assert agy_mcp.read_sessions()["default"]["conversation_id"] == "conversation-1"
    finally:
        agy_mcp.INSTANCE_ID = real_id


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
    """Drive the server over stdio exactly like a client would; no `agy` call happens."""
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
        responses = run_server(
            [
                ask(1, "remember VIOLET-7391", session="handoff-check"),
                call(2, "antigravity_sessions"),
                ask(3, "what was the token?", session="handoff-check", handoff=True),
                call(4, "antigravity_sessions"),
            ],
            {"AGY_MCP_STATE_DIR": state, "AGY_MCP_TRANSPORT": "stream"},
        )
        before = tracked_conversation_ids(responses[2])
        after = tracked_conversation_ids(responses[4])
        assert len(before) == 1 and len(after) == 1
        assert before != after, f"handoff must land in a new conversation: {before} -> {after}"

        answer = responses[3]["result"]["content"][0]["text"]
        # The digest turn runs on the old conversation, then the prompt is seeded into a new one.
        assert "echo: Summarize the conversation above" in answer, answer
        assert answer.endswith("what was the token?"), answer


def test_cancelled_turn_is_dropped_and_frees_the_session() -> None:
    """Esc in the client sends notifications/cancelled; that must stop the turn, not just be ignored."""
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
            {"AGY_MCP_STATE_DIR": state},
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


def test_auto_handoff_compacts_a_long_conversation() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-auto-") as state:
        responses = run_server(
            [
                ask(1, "remember VIOLET-7391", session="auto-check"),
                call(2, "antigravity_sessions"),
                ask(3, "status?", session="auto-check"),
                call(4, "antigravity_sessions"),
            ],
            {
                "AGY_MCP_STATE_DIR": state,
                # The fake reports 10 input tokens for the first turn, so a 5-token
                # ceiling makes the follow-up call compact automatically.
                "AGY_MCP_AUTO_HANDOFF": "1",
                "AGY_MCP_LONG_CONTEXT_TOKENS": "5",
            },
        )
        before = tracked_conversation_ids(responses[2])
        after = tracked_conversation_ids(responses[4])
        assert before != after, f"auto-handoff must start a new conversation: {before} -> {after}"
        answer = responses[3]["result"]["content"][0]["text"]
        assert "echo: Summarize the conversation above" in answer, answer
        assert any("auto-handoff" in note["text"] for note in responses[3]["result"]["content"][1:])


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
