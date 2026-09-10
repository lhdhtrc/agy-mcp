"""会话相关的落盘状态：遗留进程 pid 记录。

服务器被强杀（SIGKILL / 任务管理器）时来不及清理，所以把会话进程的 pid 记在
`workers.json` 里，下次启动据此收拾残局——但动手前必须确认那个 pid 现在**仍然是 agy**，
避免 pid 复用误杀别的程序。

记录按**服务器实例**分租：每个实例连同自己的服务器进程 pid 一起写进去，清理时只处理
"服务器进程已经不在"的那些租。否则并行的两个 Codex 线程会互相误杀对方正在跑的会话进程
（`INSTANCE_WINDOW` 那套活跃度判定只用于会话表的接管，不能用来判 pid 归属）。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from core.agy import CREATE_NO_WINDOW, pid_alive
from core.config import AGY_CLI_HOME, SESSIONS_PATH, STATE_DIR, _env_float, log
from core.guard import FileLock

if TYPE_CHECKING:  # 只为类型标注，避免与 core/worker.py 形成导入环
    from core.worker import Worker

WORKER_PID_FILE = os.path.join(STATE_DIR, "workers.json")
# 会话表与 pid 记录都是"读-改-写"，并行的实例必须排队，否则会互相覆盖
SESSIONS_LOCK_PATH = os.path.join(STATE_DIR, "sessions.lock")
WORKERS_LOCK_PATH = os.path.join(STATE_DIR, "workers.lock")
STATE_LOCK_WAIT_SEC = 30.0
# 服务器进程早已消失、记录又超过这个年纪的，直接丢掉（单位：秒）
INSTANCE_RECORD_TTL_SEC = 7 * 86400

# 每个 MCP 服务器实例一个身份：一个 Codex 会话对应一个实例。
# 每次新起 `agy -p` 进程都要重做鉴权与模型/额度初始化（约 5 秒），常驻的
# `--input-format stream-json` 进程服务一个会话，热轮约 1.5 秒；会话表按实例隔离，
# 两个 Codex 会话才不会抢同一个 Antigravity 会话。新实例会沿用上一个实例的映射，
# 除非检测到另一个实例仍活跃。
INSTANCE_ID = f"{os.getpid()}-{int(time.time() * 1000)}"
# 判定"另一个实例仍活跃"的时间窗（秒）
ADOPT_WINDOW_SEC = _env_float("AGY_MCP_INSTANCE_WINDOW_SEC", 120.0)


def _int_pids(values: Any) -> List[int]:
    """从任意值里挑出整数 pid（容忍手工改过的文件）。"""
    if not isinstance(values, list):
        return []
    return [int(pid) for pid in values if isinstance(pid, int)]


def _read_worker_records() -> Dict[str, Dict[str, Any]]:
    """读出 `workers.json`：实例 id -> {服务器 pid, 会话进程 pid 列表, 最后写入时间}。

    旧版是扁平的 pid 列表（没有归属），统一挂到一个没有服务器 pid 的 `legacy` 租下，
    这样它下次启动就会被当成遗留进程清理掉。
    """
    try:
        with open(WORKER_PID_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    if isinstance(data, list):
        pids = _int_pids(data)
        return {"legacy": {"pid": None, "pids": pids, "last_seen": 0.0}} if pids else {}
    if not isinstance(data, dict):
        return {}
    records: Dict[str, Dict[str, Any]] = {}
    for instance_id, record in data.items():
        if not isinstance(record, dict):
            continue
        owner = record.get("pid")
        records[str(instance_id)] = {
            "pid": owner if isinstance(owner, int) else None,
            "pids": _int_pids(record.get("pids")),
            "last_seen": float(record.get("last_seen") or 0.0),
        }
    return records


def _write_worker_records(records: Dict[str, Dict[str, Any]]) -> None:
    """整表写回 `workers.json`（失败只忽略，不影响主流程）。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(WORKER_PID_FILE, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False)
    except OSError:
        pass


def _prune_worker_records(records: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """丢掉"服务器早就不在了、记录也很旧"的租，避免文件无限增长。"""
    now = time.time()
    for instance_id in list(records):
        record = records[instance_id]
        owner = record.get("pid")
        if isinstance(owner, int) and pid_alive(owner):
            continue
        if now - float(record.get("last_seen") or 0.0) > INSTANCE_RECORD_TTL_SEC:
            records.pop(instance_id, None)
    return records


def _mutate_worker_records(mutate: Any) -> Dict[str, Dict[str, Any]]:
    """在跨进程锁里完成一次 workers.json 的读-改-写（锁等不到就照常写）。"""
    lock: Optional[FileLock] = FileLock(WORKERS_LOCK_PATH, STATE_LOCK_WAIT_SEC)
    try:
        lock.__enter__()
    except TimeoutError:
        lock = None
    try:
        records = _prune_worker_records(_read_worker_records())
        mutate(records)
        _write_worker_records(records)
        return records
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


def _read_worker_pids() -> List[int]:
    """全部会话进程 pid（不分实例；诊断与测试用）。"""
    return sorted({pid for record in _read_worker_records().values() for pid in record["pids"]})


def _write_worker_pids(pids: List[int], instance: Optional[str] = None) -> None:
    """覆盖某个实例（默认本实例）的会话进程 pid 列表。"""
    owner = instance or INSTANCE_ID

    def mutate(records: Dict[str, Dict[str, Any]]) -> None:
        records[owner] = {
            "pid": os.getpid(),
            "pids": sorted(set(_int_pids(list(pids)))),
            "last_seen": time.time(),
        }

    _mutate_worker_records(mutate)


def track_worker_pid(pid: Optional[int], add: bool, instance: Optional[str] = None) -> None:
    """记录/移除一个会话进程 pid，供"被强杀后下次启动清理"使用。"""
    if not pid:
        return
    owner = instance or INSTANCE_ID

    def mutate(records: Dict[str, Dict[str, Any]]) -> None:
        record = records.get(owner)
        if not isinstance(record, dict) or not isinstance(record.get("pids"), list):
            record = {"pid": None, "pids": [], "last_seen": 0.0}
            records[owner] = record
        pids: List[int] = record["pids"]
        if add:
            if pid not in pids:
                pids.append(pid)
        elif pid in pids:
            pids.remove(pid)
        record["pid"] = os.getpid()
        record["last_seen"] = time.time()

    _mutate_worker_records(mutate)


def clear_worker_pids(instance: Optional[str] = None) -> None:
    """只清掉某个实例自己的记录；别的实例的线索不能动。"""
    owner = instance or INSTANCE_ID

    def mutate(records: Dict[str, Dict[str, Any]]) -> None:
        records.pop(owner, None)

    _mutate_worker_records(mutate)


def live_instance_ids() -> set:
    """还活着的服务器实例：本实例 + workers.json 里服务器进程仍在的那些。

    判据是"那个服务器进程还活着"，不是时间窗——时间窗分辨不出"刚被强杀的实例"和
    "正在跑长轮次的实例"，而误杀后者会让另一条 Codex 线程的轮次当场失败。
    """
    live = {INSTANCE_ID}
    for instance_id, record in _read_worker_records().items():
        if instance_id == INSTANCE_ID:
            continue
        owner = record.get("pid")
        if isinstance(owner, int) and pid_alive(owner):
            live.add(str(instance_id))
    return live


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
    """本 MCP 服务器实例（等于一个 Codex 会话）自己的会话表。"""
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
    # 并行的实例各写各的租，但整表是读-改-写；不串行的话后写的会把先写的整段覆盖掉。
    lock: Optional[FileLock] = FileLock(SESSIONS_LOCK_PATH, STATE_LOCK_WAIT_SEC)
    try:
        lock.__enter__()
    except TimeoutError:
        lock = None
    try:
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
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


def remember_partial_turn(
    sessions: Dict[str, Any],
    entry: Dict[str, Any],
    session_name: str,
    workspace: str,
    worker: Optional["Worker"],
) -> None:
    """记住中途失败的轮次的会话 id，好让下一次调用接着它继续。"""
    if worker is None or not worker.conversation_id:
        return
    # 在旧记录上"改"而不是"换"：粘住的 model / effort 与累计 token 必须留着，
    # 否则一次超时就会把用户设过的强度悄悄抹掉（文档承诺"改一次就一直生效"）。
    sessions[session_name] = dict(
        entry,
        conversation_id=worker.conversation_id,
        workspace=workspace,
        updated=time.strftime("%Y-%m-%dT%H:%M:%S"),
        calls=int(entry.get("calls", 0) or 0),
        num_turns=worker.turns,
        last_error="turn interrupted; resumed on the next call",
    )
    write_sessions(sessions)


def read_last_conversations() -> Dict[str, str]:
    """工作目录 -> 会话 id，这是 Antigravity CLI 自己记的映射。"""
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
    """兜底捕获：在我们这次调用时间窗内被改动过的、最新的那个会话存档。"""
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
