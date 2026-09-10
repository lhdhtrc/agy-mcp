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
