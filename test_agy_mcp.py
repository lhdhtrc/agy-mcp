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
            # stands in for a long agent turn; a cancel must kill it
            time.sleep(float(os.environ.get("FAKE_AGY_SLOW_SEC", "60")))
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


def stored_conversation_ids(state_dir: str) -> set:
    store = read_store(state_dir)
    return {
        entry["conversation_id"]
        for instance in store["instances"].values()
        for entry in instance["sessions"].values()
    }


def latest_session_entry(state_dir: str) -> dict:
    """The session record of the instance that wrote most recently."""
    store = read_store(state_dir)
    instances = [data for data in store["instances"].values() if data.get("sessions")]
    newest = max(instances, key=lambda data: float(data.get("last_seen") or 0))
    return next(iter(newest["sessions"].values()))


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
        # Two separate client calls: the previous conversation must be adopted by the
        # second run (AGY_MCP_INSTANCE_WINDOW_SEC=0 forces that in the test).
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
        # The streamed text delta is forwarded so a client can show the answer growing.
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
            # The fake reports 10 input tokens for the first turn, so a 5-token
            # ceiling makes the follow-up call compact automatically.
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
    """A hard-killed server leaves session processes behind; the next start cleans them up."""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.5)"])
    try:
        agy_mcp._write_worker_pids([sleeper.pid])
        original = agy_mcp._is_agy_process
        agy_mcp._is_agy_process = lambda pid: True  # the guard is tested separately
        try:
            agy_mcp.reap_orphan_workers()
        finally:
            agy_mcp._is_agy_process = original
        time.sleep(1.0)
        assert sleeper.poll() is not None, "orphaned session process should have been killed"
        assert agy_mcp._read_worker_pids() == []
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


def test_orphan_reaping_never_kills_unrelated_processes() -> None:
    sleeper = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.5)"])
    try:
        agy_mcp._write_worker_pids([sleeper.pid])
        agy_mcp.reap_orphan_workers()  # real guard: this pid is not the Antigravity CLI
        time.sleep(0.5)
        assert sleeper.poll() is None, "must not kill a process that is not agy"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def _make_repo_with_change() -> str:
    """A throwaway git repo with one committed file and one uncommitted edit."""
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
    """The CLI rejects `--model gemini-3.8-flash-high --effort low`, so keep them consistent."""
    model, effort, notes = agy_mcp.reconcile_model_and_effort("gemini-3.8-flash-high", "low", False)
    assert (model, effort) == ("gemini-3.8-flash-low", "low"), (model, effort)
    assert any("gemini-3.8-flash-low" in note for note in notes), notes

    # already consistent: pass both through untouched
    assert agy_mcp.reconcile_model_and_effort("gemini-3.8-flash-low", "low", True)[:2] == (
        "gemini-3.8-flash-low",
        "low",
    )

    # a family without an effort suffix keeps the model and drops the effort
    model, effort, notes = agy_mcp.reconcile_model_and_effort("claude-sonnet-4-6", "low", True)
    assert (model, effort) == ("claude-sonnet-4-6", None)
    assert any("ignored" in note for note in notes), notes

    # no model at all: the effort alone is fine
    assert agy_mcp.reconcile_model_and_effort(None, "low", False) == (None, "low", [])


def test_effort_change_is_sticky_for_the_session() -> None:
    with tempfile.TemporaryDirectory(prefix="agy-mcp-effort-") as state:
        # A real client keeps one server process; the window forces the same model here.
        env = {"AGY_MCP_STATE_DIR": state, "AGY_MCP_INSTANCE_WINDOW_SEC": "0"}
        first = run_server([ask(1, "hello", session="effort-check", effort="low")], env)
        notes = [item["text"] for item in first[1]["result"]["content"][1:]]
        assert any("gemini-3.8-flash-low" in note for note in notes), notes

        # no effort argument this time: the session keeps the value set above
        second = run_server([ask(1, "again", session="effort-check")], env)
        entry = latest_session_entry(state)
        assert entry["effort"] == "low", entry
        assert entry["model"] == "gemini-3.8-flash-low", entry
        assert second[1]["result"]["isError"] is False

        # "default" clears it again
        run_server([ask(1, "clear it", session="effort-check", effort="default", model="default")], env)
        entry = latest_session_entry(state)
        # effort is gone; the model falls back to the configured default
        assert entry["effort"] is None, entry
        assert entry["model"] == agy_mcp.DEFAULT_MODEL_ID, entry


def test_stream_fixture_still_parses() -> None:
    """Regression guard against CLI protocol drift: a recorded real stream must stay parsable."""
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

    # the same helpers the live worker uses must derive progress from the fixture
    active = next(event for event in events if event.get("step_update", {}).get("state") == "ACTIVE")
    label, detail = agy_mcp.progress_from_event(active)
    assert "agent_response" in label, label
    assert detail == "OK", detail
    assert agy_mcp.progress_from_event(init) is None


def test_model_defaults_and_auto_selection() -> None:
    assert agy_mcp.resolve_model(None) == (agy_mcp.DEFAULT_MODEL_ID, [])
    assert agy_mcp.resolve_model("claude-sonnet-4-6") == ("claude-sonnet-4-6", [])

    models = "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-sonnet-4-6\tClaude Sonnet 4.6"
    original_models, original_quota = agy_mcp.cached_models, agy_mcp.read_quota
    agy_mcp.cached_models = lambda: (0, models)
    try:
        agy_mcp.QUOTA_CACHE.update({"ts": time.time(), "payload": _quota_payload(90, 5)})
        chosen, notes = agy_mcp.resolve_model("auto")
        assert chosen == "gemini-3.8-flash-high", (chosen, notes)
        assert any("headroom 90%" in note for note in notes), notes

        agy_mcp.QUOTA_CACHE.update({"ts": time.time(), "payload": _quota_payload(2, 80)})
        chosen, notes = agy_mcp.resolve_model("auto")
        assert chosen == "claude-sonnet-4-6", (chosen, notes)
    finally:
        agy_mcp.cached_models, agy_mcp.read_quota = original_models, original_quota
        agy_mcp.QUOTA_CACHE.update({"ts": 0.0, "payload": None})


def test_quota_warning_is_warn_only_and_cooldown_limited() -> None:
    original = dict(agy_mcp.QUOTA_CACHE)
    try:
        agy_mcp.QUOTA_CACHE.update({"ts": time.time(), "payload": _quota_payload(90, 90)})
        agy_mcp._QUOTA_WARNED_AT = 0.0
        assert agy_mcp.quota_warning() is None

        agy_mcp.QUOTA_CACHE.update({"ts": time.time(), "payload": _quota_payload(4, 90)})
        warning = agy_mcp.quota_warning()
        assert warning and "Gemini Models" in warning, warning
        # cooldown: the same state must not nag on every call
        assert agy_mcp.quota_warning() is None
    finally:
        agy_mcp.QUOTA_CACHE.update(original)
        agy_mcp._QUOTA_WARNED_AT = 0.0


def test_orphan_reaping_never_kills_unrelated_processes() -> None:
    sleeper = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.5)"])
    try:
        agy_mcp._write_worker_pids([sleeper.pid])
        agy_mcp.reap_orphan_workers()  # real guard: this pid is not the Antigravity CLI
        time.sleep(0.5)
        assert sleeper.poll() is None, "must not kill a process that is not agy"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def test_read_only_tools_do_not_queue_behind_a_turn() -> None:
    """`models` must answer while a long turn is still running, not after it."""
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
    test_diff_is_captured_locally_and_attached,
    test_diff_on_a_non_repo_is_reported_not_fatal,
    test_model_defaults_and_auto_selection,
    test_stream_fixture_still_parses,
    test_effort_is_reconciled_with_the_model_id,
    test_effort_change_is_sticky_for_the_session,
    test_quota_warning_is_warn_only_and_cooldown_limited,
    test_orphan_reaping_never_kills_unrelated_processes,
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
