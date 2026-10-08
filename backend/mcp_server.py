#!/usr/bin/env python
"""MCP server 入口（stdio）。

把本文件路径填进客户端的 mcpServers 配置即可。
配置样例与说明见 `app/mcp/README.md`。

    python mcp_server.py

它只做协议与转发，不会启动后端 —— 后端要单独跑：
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许从任意工作目录启动：把 backend/ 放进 sys.path，`import app.mcp...` 才能解析
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.mcp.server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
