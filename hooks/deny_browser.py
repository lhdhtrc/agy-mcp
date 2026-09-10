#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 agy-mcp contributors
"""agy 的 PreToolUse 钩子：硬拒它自带的浏览器工具。

为什么：agy 内置的浏览器驱动是 playwright-go，下载源是 Playwright 已经废弃的旧 CDN（全部 404），
装不上；不拦着它，一轮提问会先花上很多步在"装浏览器—失败—再试"上。本项目让 agy 复用
Codex 的 MCP 工具（`register_agy_mcp.py --share-codex-tools`）联网，所以这里直接把
浏览器一族工具拒掉，让它一开始就走正确的路。

契约（见 agy 自带文档 `builtin/skills/agy-customizations/docs/hooks.md`）：

* stdin 收到 `{"toolCall": {"name": ..., "args": ...}, "stepIdx": ..., ...}`；
* stdout 回 `{"decision": "deny"|"allow"|"ask"|"force_ask", "reason": "..."}`。

工具名是 `CORTEX_STEP_TYPE_*` 去掉前缀、转小写（例如 `read_browser_page`、
`capture_browser_screenshot`、`execute_browser_javascript`），所以注册在 hooks.json 里的
matcher 用 `(?i)(browser|playwright)` 就能覆盖全族。
"""

from __future__ import annotations

import json
import sys

REASON = (
    "agy-mcp: Antigravity 自带的浏览器工具已禁用（它的 playwright 驱动装不上）。"
    "需要联网或浏览器能力时，请改用共享进来的 Codex MCP 工具（例如 node_repl），"
    "或者先让 Codex 取好材料，再用 no_web=true 提问。"
)


def main() -> int:
    try:  # 钩子契约要求从 stdin 读输入；内容我们不看，读不到也不影响决定
        json.load(sys.stdin)
    except (ValueError, OSError):
        pass
    sys.stdout.write(json.dumps({"decision": "deny", "reason": REASON}, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
