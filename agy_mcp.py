#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 agy-mcp contributors
"""agy-mcp: expose the Antigravity CLI (agy) to MCP clients such as Codex.

Transport: MCP over stdio, newline-delimited JSON-RPC 2.0 (no Content-Length framing).
Only the Python standard library is used, so any MCP client can spawn this file
directly without installing dependencies.

The CLI keeps its own Google sign-in state; this server never touches credentials.
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
SERVER_VERSION = "0.1.3"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2024-11-05"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEFAULT_TIMEOUT_SEC = 300
TIMEOUT_GRACE_SEC = 30

# Guard rails: the CLI is the vendor's own client, but quota is meant for a human
# driving an agent. Serialising calls and capping daily volume keeps the traffic
# shape ordinary, which is the main thing a wrapper can do about account risk.
STATE_DIR = os.environ.get("AGY_MCP_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".agy-mcp")
LOCK_PATH = os.path.join(STATE_DIR, "call.lock")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
USAGE_PATH = os.path.join(STATE_DIR, "usage.jsonl")
SESSIONS_PATH = os.path.join(STATE_DIR, "sessions.json")
_USAGE_SINCE_ROTATE = 0
# Antigravity CLI keeps its own state (conversation ids, workspace index) here.
AGY_CLI_HOME = os.environ.get("AGY_CLI_HOME") or os.path.join(
    os.path.expanduser("~"), ".gemini", "antigravity-cli"
)
LOCK_WAIT_SEC = 600
DEFAULT_MIN_INTERVAL_SEC = 5.0
DEFAULT_MAX_CALLS_PER_DAY = 200
DEFAULT_SESSION = "default"
ANSWER_KEYS = ("response", "result", "text", "output", "content", "message", "answer")
# A fresh `agy -p` process re-does auth + model/quota init (~5s) on every call. A long-lived
# `--input-format stream-json` process serves one conversation and answers warm turns in ~1.5s.
DEFAULT_WORKER_IDLE_SEC = 900.0
# One Codex thread spawns one MCP server; scope sessions per server instance so two threads
# never fight over the same Antigravity conversation. A fresh instance adopts the previous
# instance's map unless another instance still looks alive.
INSTANCE_ID = f"{os.getpid()}-{int(time.time() * 1000)}"
DEFAULT_ADOPT_WINDOW_SEC = 120.0
DEFAULT_LONG_CONTEXT_TOKENS = 100000
DEFAULT_SHUTDOWN_GRACE_SEC = 10.0
DEFAULT_USAGE_ROTATE_MB = 5.0
DEFAULT_PROGRESS_INTERVAL_MS = 400
DEFAULT_MAX_PROMPT_CHARS = 100000
HANDOFF_PROMPT = (
    "Summarize the conversation above into a handoff brief that a brand-new session can pick up from.\n"
    "Requirements:\n"
    "1) keep the goal, the conclusions reached, key decisions and why, open items, files or paths involved, "
    "and constraints that must be respected;\n"
    "2) facts and conclusions only, no pleasantries;\n"
    "3) at most 400 words;\n"
    "4) write the brief in the same language as the conversation above;\n"
    "5) output the brief only, with no preamble or closing."
)


def handoff_prompt() -> str:
    """The handoff digest prompt; override with AGY_MCP_HANDOFF_PROMPT."""
    return os.environ.get("AGY_MCP_HANDOFF_PROMPT") or HANDOFF_PROMPT


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


MIN_INTERVAL_SEC = _env_float("AGY_MCP_MIN_INTERVAL_SEC", DEFAULT_MIN_INTERVAL_SEC)
MAX_CALLS_PER_DAY = _env_int("AGY_MCP_MAX_CALLS_PER_DAY", DEFAULT_MAX_CALLS_PER_DAY)


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


def _read_store() -> Dict[str, Any]:
    try:
        with open(SESSIONS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"instances": {}}
    if not isinstance(data, dict):
        return {"instances": {}}
    if isinstance(data.get("instances"), dict):
        return data
    # Legacy flat store: expose it as an adoptable instance.
    return {"instances": {"legacy": {"sessions": data, "last_seen": 0.0}}}


def read_sessions() -> Dict[str, Any]:
    """Session map for THIS MCP server instance (= one Codex conversation)."""
    instances = _read_store().get("instances") or {}
    mine = instances.get(INSTANCE_ID)
    if isinstance(mine, dict):
        sessions = mine.get("sessions")
        return sessions if isinstance(sessions, dict) else {}
    others = [
        (iid, data)
        for iid, data in instances.items()
        if isinstance(data, dict) and iid != INSTANCE_ID
    ]
    now = time.time()
    alive = [iid for iid, data in others if now - float(data.get("last_seen") or 0) <= ADOPT_WINDOW_SEC]
    if alive:
        return {}  # a parallel Codex thread is active: give this one its own conversation
    if others:
        best = max(others, key=lambda kv: float(kv[1].get("last_seen") or 0))
        sessions = best[1].get("sessions")
        return dict(sessions) if isinstance(sessions, dict) else {}
    return {}


def write_sessions(sessions: Dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        store = _read_store()
        instances = store.setdefault("instances", {})
        now = time.time()
        for iid in [
            iid
            for iid, data in list(instances.items())
            if isinstance(data, dict) and now - float(data.get("last_seen") or 0) > 7 * 86400
        ]:
            instances.pop(iid, None)
        instances[INSTANCE_ID] = {"sessions": sessions, "last_seen": now}
        with open(SESSIONS_PATH, "w", encoding="utf-8") as handle:
            json.dump(store, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        log(f"could not persist sessions: {exc}")


def remember_partial_turn(
    sessions: Dict[str, Any],
    entry: Dict[str, Any],
    session_name: str,
    workspace: str,
    worker: Optional["Worker"],
) -> None:
    """Keep the conversation id of a turn that failed mid-flight so the next call resumes it."""
    if worker is None or not worker.conversation_id:
        return
    sessions[session_name] = {
        "conversation_id": worker.conversation_id,
        "workspace": workspace,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "calls": int(entry.get("calls", 0) or 0),
        "num_turns": worker.turns,
        "last_model": entry.get("last_model"),
        "last_input_tokens": int(entry.get("last_input_tokens", 0) or 0),
        "last_error": "turn interrupted; resumed on the next call",
    }
    write_sessions(sessions)


def read_last_conversations() -> Dict[str, str]:
    """workspace path -> conversation id, as tracked by the Antigravity CLI."""
    try:
        path = os.path.join(AGY_CLI_HOME, "cache", "last_conversations.json")
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {str(key): str(value) for key, value in data.items()}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def newest_conversation_since(since_ts: float) -> Optional[str]:
    """Fallback capture: newest conversation store touched during our call window."""
    conv_dir = os.path.join(AGY_CLI_HOME, "conversations")
    try:
        names = os.listdir(conv_dir)
    except OSError:
        return None
    newest: Optional[str] = None
    newest_mtime = 0.0
    for name in names:
        if not name.endswith(".db"):
            continue
        try:
            mtime = os.path.getmtime(os.path.join(conv_dir, name))
        except OSError:
            continue
        if mtime >= since_ts - 3 and mtime > newest_mtime:
            newest_mtime = mtime
            newest = name[: -len(".db")]
    return newest


def parse_json_output(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        else:
            return None
    return payload if isinstance(payload, dict) else None


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


def seed_prompt(prompt: str, digest: Optional[str]) -> str:
    """Handoff: start a fresh conversation that has read a digest of the previous one."""
    if not digest:
        return prompt
    return (
        "前情提要（上一段 Antigravity 会话的交接摘要）：\n"
        f"{digest.strip()}\n\n"
        "以上是背景。请在此基础上继续完成下面的任务：\n\n"
        f"{prompt}"
    )


def attach_files(prompt: str, files: Any) -> str:
    """Prepend file paths the agent should read itself (cheaper than pasting contents)."""
    if not isinstance(files, list):
        return prompt
    paths = [str(item).strip() for item in files if str(item).strip()]
    if not paths:
        return prompt
    listing = "\n".join(f"- {path}" for path in paths)
    return (
        "Read these files yourself with your file tools before answering:\n"
        f"{listing}\n\n"
        f"Then complete this task:\n\n{prompt}"
    )


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


def log(message: str) -> None:
    sys.stderr.write(f"[agy-mcp] {message}\n")
    sys.stderr.flush()


def resolve_agy() -> str:
    """Locate the agy executable: env override, PATH, then known install dirs."""
    override = os.environ.get("AGY_BIN")
    if override and os.path.exists(override):
        return override

    found = shutil.which("agy") or shutil.which("agy.exe")
    if found:
        return found

    exe = "agy.exe" if os.name == "nt" else "agy"
    candidates = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "agy", "bin", exe))
    candidates.append(os.path.join(os.path.expanduser("~"), ".local", "bin", exe))
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    raise FileNotFoundError(
        "agy executable not found; install the Antigravity CLI or set AGY_BIN"
    )


def agy_command_prefix() -> List[str]:
    """Command prefix used to invoke the CLI.

    `AGY_MCP_AGY_CMD` replaces it entirely (space-separated, no shell), which is handy for
    wrappers, containers and tests: e.g. `AGY_MCP_AGY_CMD="wsl agy"` or a fake CLI script.
    """
    override = os.environ.get("AGY_MCP_AGY_CMD")
    if override and override.strip():
        return shlex.split(override)
    return [resolve_agy()]


def run_agy(
    argv: List[str],
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Tuple[int, str, str]:
    """Run one `agy` invocation, cleaning up the whole process group on timeout."""
    command = agy_command_prefix()
    popen_kwargs: Dict[str, Any] = {}
    if os.name != "nt":
        # Own process group so a timed-out run can be killed together with its helpers.
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        command + argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )
    # Expose the process to a running tool call so a client cancellation can kill it.
    task = current_task()
    if task is not None:
        task.process = proc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        raise
    finally:
        if task is not None:
            task.process = None
    return proc.returncode, out or "", err or ""


def text_result(text: str, is_error: bool = False, notes: Optional[List[str]] = None) -> Dict[str, Any]:
    content = [{"type": "text", "text": text}]
    for note in notes or []:
        content.append({"type": "text", "text": f"[agy-mcp] {note}"})
    return {"content": content, "isError": is_error}


def join_streams(code: int, out: str, err: str) -> str:
    body = out.strip()
    err = err.strip()
    if not body and err:
        body = err
    elif err:
        body = f"{body}\n\n[stderr]\n{err}"
    if code != 0 and not body:
        body = f"agy exited with code {code}"
    return body


def kill_process_tree(proc: subprocess.Popen) -> None:
    """agy spawns helper processes; make sure a stopped worker leaves none behind."""
    try:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


class Worker:
    """A long-lived `agy --input-format stream-json` process bound to one conversation."""

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

        deadline = time.monotonic() + timeout
        steps = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no result within {int(timeout)}s")
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
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
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
                steps += 1
                update = event.get("step_update")
                update = update if isinstance(update, dict) else {}
                step_type = str(update.get("step_type") or event.get("step_type") or "step")
                state = str(update.get("state") or "")
                label = f"{step_type} {state}".strip()
                delta = update.get("text_delta")
                detail = summarize_delta(delta) if isinstance(delta, str) else None
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
WORKER_IDLE_SEC = _env_float("AGY_MCP_WORKER_IDLE_SEC", DEFAULT_WORKER_IDLE_SEC)
ADOPT_WINDOW_SEC = _env_float("AGY_MCP_INSTANCE_WINDOW_SEC", DEFAULT_ADOPT_WINDOW_SEC)
LONG_CONTEXT_TOKENS = _env_int("AGY_MCP_LONG_CONTEXT_TOKENS", DEFAULT_LONG_CONTEXT_TOKENS)
SHUTDOWN_GRACE_SEC = _env_float("AGY_MCP_SHUTDOWN_GRACE_SEC", DEFAULT_SHUTDOWN_GRACE_SEC)
AUTO_HANDOFF = _env_int("AGY_MCP_AUTO_HANDOFF", 0) != 0
USAGE_ROTATE_BYTES = int(_env_float("AGY_MCP_USAGE_ROTATE_MB", DEFAULT_USAGE_ROTATE_MB) * 1024 * 1024)
PROGRESS_INTERVAL_MS = _env_int("AGY_MCP_PROGRESS_INTERVAL_MS", DEFAULT_PROGRESS_INTERVAL_MS)
MAX_PROMPT_CHARS = _env_int("AGY_MCP_MAX_PROMPT_CHARS", DEFAULT_MAX_PROMPT_CHARS)
MAX_PARALLEL = max(1, _env_int("AGY_MCP_MAX_PARALLEL", 1))
PREWARM = _env_int("AGY_MCP_PREWARM", 0) != 0


def reap_workers() -> None:
    now = time.time()
    for key in list(WORKERS):
        worker = WORKERS[key]
        if worker.busy:
            continue  # a long turn must not be reaped from under itself
        idle = WORKER_IDLE_SEC > 0 and now - worker.last_used > WORKER_IDLE_SEC
        if not worker.alive() or idle:
            worker.stop()
            WORKERS.pop(key, None)


def shutdown_workers() -> None:
    for worker in list(WORKERS.values()):
        worker.stop()
    WORKERS.clear()
    _write_worker_pids([])


WORKER_PID_FILE = os.path.join(STATE_DIR, "workers.json")


def _read_worker_pids() -> List[int]:
    try:
        with open(WORKER_PID_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return [int(pid) for pid in data if isinstance(pid, int)]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _write_worker_pids(pids: List[int]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(WORKER_PID_FILE, "w", encoding="utf-8") as handle:
            json.dump(sorted(set(pids)), handle)
    except OSError:
        pass


def track_worker_pid(pid: Optional[int], add: bool) -> None:
    """Remember session process ids so a hard-killed server can be cleaned up later."""
    if not pid:
        return
    pids = _read_worker_pids()
    if add:
        pids.append(pid)
    elif pid in pids:
        pids.remove(pid)
    _write_worker_pids(pids)


def _is_agy_process(pid: int) -> bool:
    """Guard against pid reuse: only kill a process that still looks like the CLI."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True, text=True, timeout=15,
            ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "agy" in out.lower()


def reap_orphan_workers() -> None:
    """A server killed with SIGKILL/TerminateProcess cannot clean up its session processes."""
    pids = _read_worker_pids()
    if not pids:
        return
    for pid in pids:
        if not _is_agy_process(pid):
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
    """Recycle idle session processes even when no call is coming in."""
    while True:
        time.sleep(interval)
        try:
            reap_workers()
        except Exception as exc:  # noqa: BLE001 - a reaper must never kill the server
            log(f"reaper error: {exc!r}")


def _install_signal_handlers() -> None:
    """Make SIGTERM/SIGINT shut the child processes down too (atexit is not enough)."""

    def handler(signum: int, _frame: Any) -> None:
        log(f"signal {signum}: stopping session processes")
        shutdown_workers()
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (AttributeError, OSError, ValueError):
            pass


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


def summarize_delta(text: str, limit: int = 120) -> str:
    """Collapse a streamed text delta into a short single-line preview."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return "…" + collapsed[-limit:]


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


atexit.register(shutdown_workers)


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
            "description": "Prompt sent to the Antigravity CLI in non-interactive print mode.",
        },
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Paths the Antigravity agent should read itself before answering, instead of "
                "pasting file contents into the prompt."
            ),
        },
        "model": {"type": "string", "description": "Optional Antigravity model id."},
        "effort": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "Reasoning effort for this Antigravity session.",
        },
        "cwd": {
            "type": "string",
            "description": "Working directory of the Antigravity session (its workspace).",
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
            "description": "Start a fresh conversation for this session instead of resuming the stored one.",
        },
        "handoff": {
            "type": "boolean",
            "default": False,
            "description": (
                "Compact the current conversation into a handoff digest, start a NEW conversation and "
                "answer there with that digest as background. Use when the conversation has grown long."
            ),
        },
        "agent": {"type": "string", "description": "Optional Antigravity agent name."},
        "mode": {
            "type": "string",
            "enum": ["plan", "accept-edits"],
            "description": "Antigravity execution mode; omit to use the CLI default.",
        },
        "sandbox": {
            "type": "boolean",
            "default": True,
            "description": "Run the session with terminal restrictions enabled (default true).",
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
            "description": "Continue the most recent Antigravity conversation.",
        },
        "conversation": {
            "type": "string",
            "description": "Resume a specific Antigravity conversation id (overrides the stored session).",
        },
        "output_format": {
            "type": "string",
            "enum": ["text", "json"],
            "default": "text",
            "description": "CLI print-mode output format returned verbatim.",
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
            "description": "Do not expand slash commands or skills in the prompt (default true).",
        },
        "timeout_sec": {
            "type": "number",
            "default": DEFAULT_TIMEOUT_SEC,
            "description": "Maximum seconds to wait for the Antigravity CLI.",
        },
        "extra_args": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Extra raw agy flags appended verbatim.",
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "antigravity_ask",
        "description": (
            "Run one non-interactive prompt through the local Antigravity CLI (agy) and "
            "return its answer. Uses the Google account already signed in to agy, so it "
            "consumes that Antigravity/Gemini quota instead of the current provider."
        ),
        "inputSchema": ASK_SCHEMA,
    },
    {
        "name": "antigravity_models",
        "description": "List the Antigravity models available to the signed-in agy account.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "antigravity_agents",
        "description": "List the Antigravity agents defined for the signed-in agy account.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "antigravity_sessions",
        "description": (
            "List or forget the Antigravity conversations this server has been tracking, "
            "together with the conversations the CLI knows about locally."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "forget"],
                    "default": "list",
                    "description": "list tracked sessions, or forget one (session) / all ('*').",
                },
                "session": {
                    "type": "string",
                    "description": "Session name for action=forget; '*' clears every tracked session.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "antigravity_quota",
        "description": (
            "Show remaining Antigravity quota (weekly and 5-hour windows per model group). "
            "Answered by the CLI itself: starts no turn and spends no quota."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "antigravity_status",
        "description": (
            "Report the resolved agy path, CLI version, working directory and whether the "
            "account can list models (a sign-in probe). Use this to diagnose failures."
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

    flags = session_flags(args, conversation, continue_recent)
    requested_format = str(args.get("output_format") or "text")
    transport = str(os.environ.get("AGY_MCP_TRANSPORT") or "stream").strip().lower()

    timeout_sec = args.get("timeout_sec") or DEFAULT_TIMEOUT_SEC
    try:
        timeout_sec = float(timeout_sec)
    except (TypeError, ValueError):
        return text_result("timeout_sec must be a number.", True)
    if timeout_sec <= 0:
        return text_result("timeout_sec must be greater than zero.", True)

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
                # The worker holds the live conversation, so the conversation id must NOT be part
                # of the key: otherwise the first follow-up call would cold-start a second process.
                base_flags = session_flags(args, None, continue_recent)
                key = f"{session_name}|{workspace}|{' '.join(base_flags)}"
                for stale in [k for k in WORKERS if k.startswith(f"{session_name}|{workspace}|") and k != key]:
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
                        timeout_sec + TIMEOUT_GRACE_SEC,
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
                    timeout_sec + TIMEOUT_GRACE_SEC,
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
                        + [f"--print-timeout", f"{int(timeout_sec)}s", "--output-format", "json"]
                    )
                    _, digest_out, _ = run_agy(
                        digest_argv, cwd=workspace, timeout=timeout_sec + TIMEOUT_GRACE_SEC
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
                argv += ["--print-timeout", f"{int(timeout_sec)}s", "--output-format", "json"]
                code, out, err = run_agy(argv, cwd=workspace, timeout=timeout_sec + TIMEOUT_GRACE_SEC)
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
                current_input = int(usage_tokens.get("input_tokens", 0) or 0)
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
    if requested_format == "json":
        return text_result(json.dumps(payload, ensure_ascii=False), False, notes)
    return text_result(answer if answer else out.strip(), False, notes)


def tool_simple(argv: List[str], label: str) -> Dict[str, Any]:
    try:
        code, out, err = run_agy(argv, timeout=DEFAULT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return text_result(f"agy {label} timed out.", True)
    except FileNotFoundError as exc:
        return text_result(str(exc), True)
    return text_result(join_streams(code, out, err), code != 0)


def parse_models(text: str) -> List[Dict[str, str]]:
    """`agy models` prints one `id<TAB>label` record per line."""
    models: List[Dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        identifier, _, label = line.partition("\t")
        if identifier.strip():
            models.append({"id": identifier.strip(), "label": label.strip()})
    return models


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


QUOTA_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None}
QUOTA_CACHE_TTL = _env_float("AGY_MCP_QUOTA_CACHE_SEC", 60.0)
MODELS_CACHE: Dict[str, Any] = {"ts": 0.0, "output": None, "code": 1}
MODELS_CACHE_TTL = _env_float("AGY_MCP_MODELS_CACHE_SEC", 300.0)


def cached_models() -> Tuple[int, str]:
    """`agy models` costs a network round trip (~4s); cache it for status calls."""
    if MODELS_CACHE.get("output") is not None and (
        time.time() - float(MODELS_CACHE.get("ts") or 0)
    ) < MODELS_CACHE_TTL:
        return int(MODELS_CACHE.get("code") or 0), str(MODELS_CACHE["output"])
    code, out, err = run_agy(["models"], timeout=DEFAULT_TIMEOUT_SEC)
    MODELS_CACHE.update({"ts": time.time(), "output": (out or err).strip(), "code": code})
    return code, str(MODELS_CACHE["output"])


def read_quota() -> Dict[str, Any]:
    """`-p "/quota"` is answered by the CLI itself: no turn, no quota spent, no conversation."""
    if QUOTA_CACHE.get("payload") and (time.time() - float(QUOTA_CACHE.get("ts") or 0)) < QUOTA_CACHE_TTL:
        return QUOTA_CACHE["payload"]
    code, out, err = run_agy(["-p", "/quota", "--output-format", "json"], timeout=90)
    payload = parse_json_output(out)
    if payload:
        QUOTA_CACHE["ts"] = time.time()
        QUOTA_CACHE["payload"] = payload
    else:
        payload = {"status": "ERROR", "error": join_streams(code, out, err)}
    return payload


def tool_quota(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        payload = read_quota()
    except subprocess.TimeoutExpired:
        return text_result("agy /quota timed out.", True)
    except FileNotFoundError as exc:
        return text_result(str(exc), True)

    command = payload.get("command")
    groups: List[Dict[str, Any]] = []
    if isinstance(command, dict):
        data = command.get("data")
        if isinstance(data, dict) and isinstance(data.get("groups"), list):
            for group in data["groups"]:
                if not isinstance(group, dict):
                    continue
                buckets = []
                for bucket in group.get("buckets") or []:
                    if not isinstance(bucket, dict):
                        continue
                    fraction = bucket.get("remaining_fraction")
                    buckets.append(
                        {
                            "id": bucket.get("id"),
                            "name": bucket.get("name"),
                            "remaining_percent": round(float(fraction) * 100, 1)
                            if isinstance(fraction, (int, float))
                            else None,
                            "reset_time": bucket.get("reset_time"),
                        }
                    )
                groups.append({"group": group.get("name"), "buckets": buckets})

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
    # `errors="replace"`: a child that hands back lone surrogates (invalid byte sequences it
    # decoded with surrogateescape) must never take the whole server down on the way out.
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

        # Tool calls can block for minutes; run them off the loop so a
        # `notifications/cancelled` arriving meanwhile can stop the turn.
        if message.get("method") == "tools/call" and message.get("id") is not None:
            params = message.get("params") or {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            task = ActiveTask(message["id"], meta.get("progressToken"))
            register_task(task)
            if str(params.get("name") or "") in FAST_TOOLS:
                # Read-only helpers do not touch a session process: never queue them
                # behind a long Antigravity turn.
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

    # stdin closed: let quick in-flight calls flush their answer, then stop the rest.
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
