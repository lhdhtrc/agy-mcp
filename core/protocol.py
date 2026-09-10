"""协议层的纯函数：结果渲染（以及后续的解析、答案提取、进度标签）。

只放**纯函数**（不碰进程、网络、磁盘），因此可以脱离 CLI 直接测试；
worker / jobs / quota 都依赖这里，等全部搬完后它们的临时懒加载会改成正常导入。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from core.config import log  # noqa: F401


def text_result(
    text: str, is_error: bool = False, notes: Optional[List[str]] = None
) -> Dict[str, Any]:
    """把一段文本包装成 MCP 工具结果；notes 会作为额外的文本块附在后面。"""
    content = [{"type": "text", "text": text}]
    for note in notes or []:
        content.append({"type": "text", "text": f"[agy-mcp] {note}"})
    return {"content": content, "isError": is_error}


def join_streams(code: int, out: str, err: str) -> str:
    """把 stdout/stderr 合成一段可读文本；失败且没有输出时给出退出码。"""
    body = out.strip()
    err = err.strip()
    if not body and err:
        body = err
    elif err:
        body = f"{body}\n\n[stderr]\n{err}"
    if code != 0 and not body:
        body = f"agy exited with code {code}"
    return body


def parse_json_output(text: str) -> Optional[Dict[str, Any]]:
    """从 CLI 输出里取出 JSON 对象：先整体解析，失败再逐行从后往前找。"""
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


def summarize_delta(text: str, limit: int = 120) -> str:
    """把流式输出的一段文字压成单行预览（超长时保留结尾，便于看"正在写什么"）。"""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return "…" + collapsed[-limit:]


def parse_stream_line(line: str) -> Optional[Dict[str, Any]]:
    """解析流式输出的一行 NDJSON；不是合法 JSON 对象时返回 None。"""
    text = line.strip()
    if not text:
        return None
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def progress_from_event(event: Dict[str, Any]) -> Optional[Tuple[str, Optional[str]]]:
    """从一步事件里取出（标签, 文字预览）；不是步骤事件时返回 None。"""
    if event.get("event") == "init":
        return None
    update = event.get("step_update")
    update = update if isinstance(update, dict) else {}
    step_type = str(update.get("step_type") or event.get("step_type") or "step")
    state = str(update.get("state") or "")
    label = f"{step_type} {state}".strip()
    delta = update.get("text_delta")
    return label, (summarize_delta(delta) if isinstance(delta, str) else None)
