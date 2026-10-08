"""MCP 接入信息 API（P-7）。

只读端点，给软件「设置」页用：把客户端配置该填的东西**算好**返回，
用户直接复制即可 —— 手抄解释器路径和脚本路径是最容易出错的一步。

注意 `sys.executable` 取的是**后端进程自己的解释器**，也就是当前这套
依赖真正装着的那个 Python（托管 venv）。这正是 MCP server 该用的解释器，
所以不用让用户自己找。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter

from ..mcp.tools import GROUP_ORDER, SPEND_ENV, TOOLS, allow_spend

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

_NOTE = (
    "把 config_json 粘进客户端的 mcpServers（Claude Desktop 在 "
    "claude_desktop_config.json，Cursor 在 mcp.json），重启客户端即可。"
    "计费工具默认被拒绝：要允许出片，把 env 里的 "
    f"{SPEND_ENV} 改成 \"1\"。MCP 只转发到后端，不自己实现流程；"
    "后端必须单独启动并保持运行。"
)


@router.get("/info")
def mcp_info() -> Dict[str, Any]:
    entry = (BACKEND_DIR / "mcp_server.py").resolve()
    py = sys.executable or "python"
    config = {"mcpServers": {"ai-video-studio": {
        "command": py,
        "args": [str(entry)],
        "env": {"STUDIO_BASE_URL": DEFAULT_BASE_URL, SPEND_ENV: "0"},
    }}}
    return {
        "server": "ai-video-studio",
        "entry": str(entry),
        "entry_exists": entry.exists(),
        "python": py,
        "base_url": DEFAULT_BASE_URL,
        "spend_env": SPEND_ENV,
        "allow_spend": allow_spend(),
        "tool_count": len(TOOLS),
        "by_group": {g: [t.name for t in TOOLS if t.group == g]
                     for g in GROUP_ORDER},
        "spend_tools": [t.name for t in TOOLS if t.spend],
        "config_json": json.dumps(config, ensure_ascii=False, indent=2),
        "note": _NOTE,
    }
