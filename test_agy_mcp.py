#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 agy-mcp contributors
"""Offline tests for agy-mcp: no network, no Antigravity quota, no `agy` required.

Run directly (`python3 test_agy_mcp.py`) or through pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = tempfile.mkdtemp(prefix="agy-mcp-test-")
os.environ["AGY_MCP_STATE_DIR"] = STATE_DIR
os.environ["AGY_MCP_MIN_INTERVAL_SEC"] = "0"
sys.path.insert(0, HERE)

import agy_mcp  # noqa: E402  (import after the state dir is set)


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
    messages = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
        ]
    )
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "agy_mcp.py")],
        input=messages, capture_output=True, text=True, timeout=60, cwd=HERE,
        env={**os.environ, "AGY_MCP_STATE_DIR": STATE_DIR},
    )
    assert proc.returncode == 0, proc.stderr
    responses = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
            if "id" in payload:
                responses[payload["id"]] = payload

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


TESTS = (
    test_extract_answer,
    test_seed_prompt,
    test_merge_usage,
    test_sessions_are_scoped_per_instance,
    test_resolve_agy_honours_env,
    test_disabled_actions_become_an_error,
    test_mcp_handshake_and_tool_list,
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
