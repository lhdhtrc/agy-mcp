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
from core.config import DEFAULT_SESSION, STATE_DIR, log


JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
JOBS_DIR = os.path.join(STATE_DIR, "jobs")


def _job_path(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def write_job(job_id: str, payload: Dict[str, Any]) -> None:
    """Jobs live on disk so a client restart does not lose track of a long run."""
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
    return sum(1 for job in list_jobs() if job.get("state") == "running")


DETACHED_PROCESS = 0x00000008


def collect_detached_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a finished detached run into a result: the process wrote its JSON to a file.

    Nothing here depends on the server that started it, which is what lets a job
    survive a client restart.
    """
    if job.get("state") != "running" or pid_alive(int(job.get("pid") or 0)):
        return job
    try:
        with open(str(job.get("out") or ""), encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        text = ""
    from core.impl import extract_answer, parse_json_output, text_result  # 临时债：等 protocol.py 抽出后改回
    payload = parse_json_output(text)
    answer = extract_answer(payload) if payload else None
    conversation = str(payload.get("conversation_id") or "") if payload else ""
    if answer and conversation:
        sessions = read_sessions()
        name = str(job.get("session") or DEFAULT_SESSION)
        entry = sessions.get(name) if isinstance(sessions.get(name), dict) else {}
        sessions[name] = dict(
            entry,
            conversation_id=conversation,
            workspace=job.get("cwd"),
            updated=time.strftime("%Y-%m-%dT%H:%M:%S"),
            calls=int(entry.get("calls", 0) or 0) + 1,
        )
        write_sessions(sessions)
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
