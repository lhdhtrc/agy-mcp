"""agy-mcp 核心包。

当前全部实现都在 `impl.py`：这一步只搭骨架，把单文件搬进包里并保留双入口，
行为一字未改。后续按 docs/refactor-plan.md 逐步拆成
config / agy / session / worker / quota / jobs / prompts / tools / protocol / server。
"""

from .impl import *  # noqa: F401,F403 —— 对外暴露全部公开名
from .impl import _QUOTA_WARNED_AT  # noqa: F401 —— 仍在 impl，等配额层拆分再移走
from .session import (  # noqa: F401 —— 下划线开头的名字测试会用到，必须显式再导出
    _is_agy_process,
    _read_worker_pids,
    _write_worker_pids,
)
