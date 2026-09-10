"""给 Antigravity CLI（agy）装的定制：目前只有一个 PreToolUse 钩子。

这里的脚本不是 MCP 服务器的一部分——它由 agy 自己在工具调用前触发
（见 `~/.gemini/config/hooks.json`），用来拒掉 agy 自带的浏览器工具。
"""
