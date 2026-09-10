"""路径、通用工具与环境变量解析：唯一读取环境变量的地方。

`_env_float` / `_env_int` 负责把环境变量解析成数值；`log` 统一把诊断信息写到 stderr
（stdout 是 MCP 协议的通道，绝不能污染）。
"""

from __future__ import annotations

import os
import sys

STATE_DIR = os.environ.get("AGY_MCP_STATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".agy-mcp"
)
# 元数据类调用（models / quota / version）的超时：这类调用要短，避免 status 卡死
METADATA_TIMEOUT_SEC = 300
SESSIONS_PATH = os.path.join(STATE_DIR, "sessions.json")
# Antigravity CLI 自己的状态（会话 id、workspace 索引）放在这里
AGY_CLI_HOME = os.environ.get("AGY_CLI_HOME") or os.path.join(
    os.path.expanduser("~"), ".gemini", "antigravity-cli"
)

# MCP 服务器标识与协议版本（版本号发版时改这里）
SERVER_NAME = "antigravity"
SERVER_VERSION = "0.2.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2024-11-05"
# 0 表示不限时：真实的 agent 作业可能跑很久，而 CLI 自带的 print 超时默认只有 5 分钟，
# 正是它把长任务掐断的。
DEFAULT_TIMEOUT_SEC = int(os.environ.get("AGY_MCP_DEFAULT_TIMEOUT_SEC") or 0)
TIMEOUT_GRACE_SEC = 30
# 元数据类调用（models / quota / version）仍需短超时，否则 status 之类可能一直挂着。
UNLIMITED_PRINT_TIMEOUT = "24h"


def _env_float(name: str, default: float) -> float:
    """读一个浮点型环境变量；缺失或非法时用默认值。"""
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    """读一个整型环境变量；缺失或非法时用默认值。"""
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def log(message: str) -> None:
    """诊断日志统一走 stderr，避免污染 MCP 的 stdout 通道。"""
    sys.stderr.write(f"[agy-mcp] {message}\n")
    sys.stderr.flush()

# 各类默认值：集中一处，其它模块按名导入（环境变量覆盖留在各自的 *_SEC / *_TOKENS 常量里）
DEFAULT_MIN_INTERVAL_SEC = 5.0
DEFAULT_MAX_CALLS_PER_DAY = 200
DEFAULT_SESSION = "default"
DEFAULT_WORKER_IDLE_SEC = 900.0
DEFAULT_LONG_CONTEXT_TOKENS = 100000
DEFAULT_SHUTDOWN_GRACE_SEC = 10.0
DEFAULT_USAGE_ROTATE_MB = 5.0
DEFAULT_PROGRESS_INTERVAL_MS = 400
DEFAULT_MAX_PROMPT_CHARS = 100000
DEFAULT_MAX_DIFF_CHARS = 60000
DEFAULT_MODEL = "gemini-3.8-flash-high"
DEFAULT_MODEL_PREFERENCE = (
    "gemini-3.8-flash-high,gemini-3.1-pro-high,claude-sonnet-4-6,"
    "claude-opus-4-6-thinking,gpt-oss-120b-medium"
)
DEFAULT_QUOTA_WARN_PERCENT = 10.0
DEFAULT_QUOTA_REFRESH_SEC = 300.0
DEFAULT_MODEL_ID = os.environ.get("AGY_MCP_DEFAULT_MODEL", DEFAULT_MODEL)

# 环境变量派生的可调项（阈值/开关）：读环境变量只在这里发生
MIN_INTERVAL_SEC = _env_float("AGY_MCP_MIN_INTERVAL_SEC", DEFAULT_MIN_INTERVAL_SEC)
MAX_CALLS_PER_DAY = _env_int("AGY_MCP_MAX_CALLS_PER_DAY", DEFAULT_MAX_CALLS_PER_DAY)
WORKER_IDLE_SEC = _env_float("AGY_MCP_WORKER_IDLE_SEC", DEFAULT_WORKER_IDLE_SEC)
LONG_CONTEXT_TOKENS = _env_int("AGY_MCP_LONG_CONTEXT_TOKENS", DEFAULT_LONG_CONTEXT_TOKENS)
SHUTDOWN_GRACE_SEC = _env_float("AGY_MCP_SHUTDOWN_GRACE_SEC", DEFAULT_SHUTDOWN_GRACE_SEC)
AUTO_HANDOFF = _env_int("AGY_MCP_AUTO_HANDOFF", 0) != 0
USAGE_ROTATE_BYTES = int(_env_float("AGY_MCP_USAGE_ROTATE_MB", DEFAULT_USAGE_ROTATE_MB) * 1024 * 1024)
PROGRESS_INTERVAL_MS = _env_int("AGY_MCP_PROGRESS_INTERVAL_MS", DEFAULT_PROGRESS_INTERVAL_MS)
MAX_PROMPT_CHARS = _env_int("AGY_MCP_MAX_PROMPT_CHARS", DEFAULT_MAX_PROMPT_CHARS)
MAX_DIFF_CHARS = _env_int("AGY_MCP_MAX_DIFF_CHARS", DEFAULT_MAX_DIFF_CHARS)
MAX_PARALLEL = max(1, _env_int("AGY_MCP_MAX_PARALLEL", 1))
PREWARM = _env_int("AGY_MCP_PREWARM", 0) != 0
MODEL_PREFERENCE = [
    item.strip()
    for item in (os.environ.get("AGY_MCP_MODEL_PREFERENCE") or DEFAULT_MODEL_PREFERENCE).split(",")
    if item.strip()
]
QUOTA_WARN_PERCENT = _env_float("AGY_MCP_QUOTA_WARN_PERCENT", DEFAULT_QUOTA_WARN_PERCENT)
QUOTA_REFRESH_SEC = _env_float("AGY_MCP_QUOTA_REFRESH_SEC", DEFAULT_QUOTA_REFRESH_SEC)
