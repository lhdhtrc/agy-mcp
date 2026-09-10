#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 agy-mcp contributors
"""agy-mcp 正式入口：只做转发，实现在 core/ 包里。"""

import sys

from core.server import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
