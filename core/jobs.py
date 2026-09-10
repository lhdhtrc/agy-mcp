"""后台作业：记录落盘、脱离进程启动与结果回收。

作业写在 `~/.agy-mcp/jobs/<id>.json`，脱离进程的输出写到同名 `.out`；因此客户端或服务器
重启都不会丢失线索。`running_job_count()` 还用于让孤儿清理"在长作业可能还活着时不要动手"。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from core import session
from core.agy import pid_alive
from core.config import DEFAULT_SESSION, STATE_DIR, log
from core.protocol import extract_answer, parse_json_output, text_result


JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
JOBS_DIR = os.path.join(STATE_DIR, "jobs")


def _job_path(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def _job_output_path(job_id: str) -> str:
    """脱离进程的原始输出；清作业时要连它一起删，否则目录只涨不消。"""
    return os.path.join(JOBS_DIR, f"{job_id}.out")


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def write_job(job_id: str, payload: Dict[str, Any]) -> None:
    """作业落盘：客户端重启也不会丢掉一个长作业的线索。"""
    try:
        os.makedirs(JOBS_DIR, exist_ok=True)
        tmp = _job_path(job_id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, _job_path(job_id))
    except (OSError, ValueError) as exc:
        log(f"could not persist job {job_id}: {exc}")


def read_job(job_id: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_job_path(job_id), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def list_jobs() -> List[Dict[str, Any]]:
    try:
        names = sorted(name for name in os.listdir(JOBS_DIR) if name.endswith(".json"))
    except OSError:
        return []
    jobs = []
    for name in names:
        job = read_job(name[: -len(".json")])
        if job:
            jobs.append(job)
    return jobs


def running_job_count() -> int:
    """真的还在跑的作业数。

    只看 `state == "running"` 会被"进程早就没了、但没人去 collect 的"陈旧记录骗到，
    而孤儿清理又是靠它来决定动不动手的——一条僵尸记录就能让清理永远不执行。
    """
    return sum(
        1
        for job in list_jobs()
        if job.get("state") == "running" and pid_alive(int(job.get("pid") or 0))
    )


DETACHED_PROCESS = 0x00000008


def collect_detached_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """把跑完的脱离进程转成结果：它把自己的 JSON 写在了文件里。

    这里不依赖当初启动它的那个服务器，所以作业能扛过客户端重启。
    """
    if job.get("state") != "running" or pid_alive(int(job.get("pid") or 0)):
        return job
    try:
        with open(str(job.get("out") or ""), encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        text = ""
    payload = parse_json_output(text)
    answer = extract_answer(payload) if payload else None
    conversation = str(payload.get("conversation_id") or "") if payload else ""
    if answer and conversation:
        sessions = session.read_sessions()
        name = str(job.get("session") or DEFAULT_SESSION)
        entry = sessions.get(name) if isinstance(sessions.get(name), dict) else {}
        sessions[name] = dict(
            entry,
            conversation_id=conversation,
            workspace=job.get("cwd"),
            updated=time.strftime("%Y-%m-%dT%H:%M:%S"),
            calls=int(entry.get("calls", 0) or 0) + 1,
        )
        session.write_sessions(sessions)
    job = dict(
        job,
        state="done" if answer else "failed",
        conversation=conversation or None,
        finished=time.strftime("%Y-%m-%dT%H:%M:%S"),
        result=text_result(
            answer or (text.strip()[-2000:] if text.strip() else "job finished without output"),
            not answer,
        ),
    )
    write_job(str(job.get("job_id")), job)
    return job
