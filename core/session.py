"""会话相关的落盘状态：遗留进程 pid 记录。

服务器被强杀（SIGKILL / 任务管理器）时来不及清理，所以把会话进程的 pid 记在
`workers.json` 里，下次启动据此收拾残局——但动手前必须确认那个 pid 现在**仍然是 agy**，
避免 pid 复用误杀别的程序。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from core.agy import CREATE_NO_WINDOW
from core.config import AGY_CLI_HOME, SESSIONS_PATH, STATE_DIR, _env_float, log

WORKER_PID_FILE = os.path.join(STATE_DIR, "workers.json")

# 每个 MCP 服务器实例一个身份：一个 Codex 会话对应一个实例
INSTANCE_ID = f"{os.getpid()}-{int(time.time() * 1000)}"
# 判定"另一个实例仍活跃"的时间窗（秒）
ADOPT_WINDOW_SEC = _env_float("AGY_MCP_INSTANCE_WINDOW_SEC", 120.0)


def _read_worker_pids() -> List[int]:
    """读出上次记录的会话进程 pid（文件缺失或损坏时返回空列表）。"""
    try:
        with open(WORKER_PID_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return [int(pid) for pid in data if isinstance(pid, int)]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _write_worker_pids(pids: List[int]) -> None:
    """把会话进程 pid 写回磁盘（去重排序；失败只忽略，不影响主流程）。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(WORKER_PID_FILE, "w", encoding="utf-8") as handle:
            json.dump(sorted(set(pids)), handle)
    except OSError:
        pass


def track_worker_pid(pid: Optional[int], add: bool) -> None:
    """记录/移除一个会话进程 pid，供"被强杀后下次启动清理"使用。"""
    if not pid:
        return
    pids = _read_worker_pids()
    if add:
        pids.append(pid)
    elif pid in pids:
        pids.remove(pid)
    _write_worker_pids(pids)


def _is_agy_process(pid: int) -> bool:
    """防止 pid 复用误杀：只有该 pid 现在看起来仍是 CLI 才动手。"""
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
    # 旧版扁平结构：当成一个可被接管的实例暴露出去。
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
