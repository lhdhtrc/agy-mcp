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
SESSIONS_PATH = os.path.join(STATE_DIR, "sessions.json")
# Antigravity CLI 自己的状态（会话 id、workspace 索引）放在这里
AGY_CLI_HOME = os.environ.get("AGY_CLI_HOME") or os.path.join(
    os.path.expanduser("~"), ".gemini", "antigravity-cli"
)


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
