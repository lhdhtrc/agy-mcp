"""提示词拼装：把文件路径、diff、禁网声明与交接摘要组装成最终提示词。

这里全是纯函数——输入字符串、输出字符串，不碰进程、网络与磁盘，因此最容易测试与复用。
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, Tuple


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
    """交接摘要用的提示词；可用 AGY_MCP_HANDOFF_PROMPT 覆盖。"""
    return os.environ.get("AGY_MCP_HANDOFF_PROMPT") or HANDOFF_PROMPT


def seed_prompt(prompt: str, digest: Optional[str]) -> str:
    """交接：在新会话里先喂上一段旧会话的摘要。"""
    if not digest:
        return prompt
    return (
        "前情提要（上一段 Antigravity 会话的交接摘要）：\n"
        f"{digest.strip()}\n\n"
        "以上是背景。请在此基础上继续完成下面的任务：\n\n"
        f"{prompt}"
    )


def attach_files(prompt: str, files: Any) -> str:
    """把文件路径放到提示词前面，让 agent 自己去读（比粘贴全文便宜）。"""
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


def attach_no_web(prompt: str) -> str:
    """别让 agent 把一轮耗在浏览器工具上（这里的驱动常常不可用）。"""
    return (
        "Do not browse the web or use any browser tool for this task; work only from the "
        "material given below and your own knowledge, and say so if something is unknown.\n\n"
        f"{prompt}"
    )


def attach_diff(prompt: str, diff_text: str, source: str) -> str:
    return (
        f"Here is the current working tree state of `{source}` for context:\n\n"
        f"{diff_text}\n\n"
        f"Now complete this task:\n\n{prompt}"
    )
