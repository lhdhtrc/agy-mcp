#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 agy-mcp contributors
"""兼容入口：既有客户端配置写的是 `python3 .../agy_mcp.py`，保持可用。

实现在 core/ 包里；本文件与 main.py 等价，只做转发。
"""

import sys

from core.server import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
