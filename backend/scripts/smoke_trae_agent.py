#!/usr/bin/env python
"""验证 Trae Agent ↔ ai-video-studio 的 MCP 接线是否真的通。

这个脚本**不需要任何 API key**，因为它只验证「配置和管道」，不跑大模型。
它回答三个问题：

1. 生成的 trae_config.yaml 能不能被 trae-agent 正确解析？（schema 对不对）
2. `allow_mcp_servers` 白名单有没有漏？（漏了不会报错，server 只是静默不生效 —— 这是最容易白找半天的坑）
3. trae-agent 能不能真的拉起我们的 mcp_server.py、列出工具、并调用一个只读工具？

因为 trae_agent 只装在它自己的 venv 里，**必须用那个解释器跑**：

    C:/Users/Administrator/.workbuddy/binaries/python/envs/trae312/Scripts/python.exe ^
        scripts/smoke_trae_agent.py

退出码 0 = 全绿；非 0 = 有断言失败（脚本会把失败点全部列出来，不中途停）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, List, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_TRAE_HOME = Path.home() / ".workbuddy" / "third_party" / "trae-agent" / "trae-agent-main"

SERVER_KEY = "ai-video-studio"

# 这几个工具是整个 video pipeline 的骨架，少一个说明 MCP 层没接全
ESSENTIAL_TOOLS = [
    "create_project",
    "list_projects",
    "generate_storyboard",
    "list_shots",
    "update_shot",
    "lint_prompt",
    "render_shot",
    "get_job",
    "compliance_scan",
    "qc_project",
    "get_budget",
    "list_assets",
]


class Checker:
    """收集结果而不是遇错即停：一次跑完能看到全貌。"""

    def __init__(self) -> None:
        self.rows: List[Tuple[bool, str, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.rows.append((True, name, detail))
        print(f"  [PASS] {name}" + (f"  ·  {detail}" if detail else ""))

    def fail(self, name: str, detail: str = "") -> None:
        self.rows.append((False, name, detail))
        print(f"  [FAIL] {name}" + (f"  ·  {detail}" if detail else ""))

    def check(self, cond: bool, name: str, detail_ok: str = "", detail_bad: str = "") -> bool:
        if cond:
            self.ok(name, detail_ok)
        else:
            self.fail(name, detail_bad)
        return cond

    @property
    def failed(self) -> int:
        return sum(1 for ok, _, _ in self.rows if not ok)

    @property
    def passed(self) -> int:
        return sum(1 for ok, _, _ in self.rows if ok)


async def call_readonly_tool(client: Any, name: str, args: dict) -> Any:
    """调一个只读 MCP 工具并返回解析后的结果。"""
    out = await client.call_tool(name, args)
    blob = getattr(out, "content", None)
    text = ""
    if blob:
        item = blob[0]
        text = getattr(item, "text", None) or (item.get("text") if isinstance(item, dict) else "")
    if getattr(out, "isError", False):
        raise RuntimeError(f"tool reported error: {text[:400]}")
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 - 纯文本返回值原样返回即可
        return text


async def main_async(config_path: Path, call_tools: bool) -> int:
    ck = Checker()

    print("\n=== 1. 配置文件能被 trae-agent 解析吗 ===")
    try:
        sys.path.insert(0, str(DEFAULT_TRAE_HOME))
        from trae_agent.utils.config import Config  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        ck.fail("导入 trae_agent", f"{type(e).__name__}: {e}")
        ck.fail("提示", "本脚本必须用装着 trae-agent 的解释器运行，详见文件头注释")
        return 2

    if not ck.check(config_path.exists(),
                    "trae_config.yaml 存在",
                    str(config_path),
                    f"找不到 {config_path}（先跑 scripts/gen_trae_config.py）"):
        return 2

    try:
        cfg = Config.create(config_file=str(config_path))
        ck.ok("Config.create 解析", "schema 合法")
    except Exception as e:  # noqa: BLE001
        ck.fail("Config.create 解析", f"{type(e).__name__}: {e}")
        return 2

    ck.check(bool(cfg.model_providers), "model_providers 已配置",
             f"{len(cfg.model_providers or {})} 个")
    ck.check(bool(cfg.models), "models 已配置", f"{len(cfg.models or {})} 个")
    ck.check(cfg.trae_agent is not None, "agents.trae_agent 已配置")

    provider_cfg = (cfg.model_providers or {}).get("studio_llm")
    if provider_cfg is not None:
        ck.check(provider_cfg.provider != "openai",
                 "provider 没踩 Responses API 的坑",
                 f"provider={provider_cfg.provider}",
                 f"provider=openai 会走 /v1/responses，中转站普遍不支持")

    print("\n=== 2. MCP server 白名单 ===")
    # 注意字段名：yaml 里写 `mcp_servers`，但解析到对象上叫 `mcp_servers_config`。
    # 用错名字不会报错，只会拿到空 dict —— 所以这里显式 sys 兜一下。
    mcp_servers = getattr(cfg.trae_agent, "mcp_servers_config", None) or {}
    allow = getattr(cfg.trae_agent, "allow_mcp_servers", None) or []
    ck.check(SERVER_KEY in mcp_servers, "mcp_servers 里有 ai-video-studio", str(list(mcp_servers)))
    ck.check(SERVER_KEY in allow,
             f"allow_mcp_servers 白名单包含 {SERVER_KEY}",
             str(allow),
             f"白名单={allow} —— 不在名单里的 server 会静默不生效，没有任何报错")
    if SERVER_KEY not in mcp_servers:
        return 2

    # 花钱护栏：这不算"通过/失败"，因为它可以是人 deliberately 打开的，
    # 但默认必须是关的 —— 所以开着的时候要显式喊出来。
    env = getattr(mcp_servers[SERVER_KEY], "env", None) or {}
    if env.get("STUDIO_MCP_ALLOW_SPEND") == "1":
        print("  [WARN] STUDIO_MCP_ALLOW_SPEND=1 —— agent 可以直接触发渲染/混流并开始计费")
    elif env.get("STUDIO_MCP_ALLOW_SPEND") == "0":
        ck.ok("花钱护栏已关闭", "agent 触发渲染/混流会被后端拒绝")
    else:
        ck.fail("花钱护栏未设置", "env 里没有 STUDIO_MCP_ALLOW_SPEND，行为取决于后端默认值")

    print("\n=== 3. 真的能拉起 mcp_server.py 吗 ===")
    from trae_agent.utils.mcp_client import MCPClient  # noqa: PLC0415

    client = MCPClient()
    tools: List[Any] = []
    try:
        await client.connect_and_discover(
            SERVER_KEY, mcp_servers[SERVER_KEY], tools, None
        )
        names = [t.get_name() for t in tools]
        ck.ok("连接并发现工具", f"{len(names)} 个工具")
        missing = [t for t in ESSENTIAL_TOOLS if t not in names]
        ck.check(not missing, "关键工具齐全",
                 f"{len(ESSENTIAL_TOOLS)}/{len(ESSENTIAL_TOOLS)}",
                 f"缺少: {missing}")
    except Exception as e:  # noqa: BLE001
        ck.fail("连接并发现工具", f"{type(e).__name__}: {e}")
        ck.fail("排查提示",
                "确认 ai-video-studio 后端在跑（http://127.0.0.1:8000/api/stats），"
                "以及 yaml 里 mcp_servers 的 command 指向一个装有 httpx 的解释器")
        return 2

    if call_tools:
        print("\n=== 4. 调一个只读工具（需要后端在线）===")
        for tool, args in (("list_projects", {}), ("get_budget", {})):
            try:
                res = await call_readonly_tool(client, tool, args)
                brief = res if isinstance(res, (int, str)) else json.dumps(res, ensure_ascii=False)[:160]
                ck.ok(f"调用 {tool}", str(brief))
            except Exception as e:  # noqa: BLE001
                ck.fail(f"调用 {tool}", f"{type(e).__name__}: {str(e)[:300]}")

    # 显式收 strio 子进程。不收的话 anyio 会在 loop 关闭时抛一段
    # "Attempted to exit cancel scope in a different task" —— 那不是失败，
    # 纯粹是清理时机造成的噪音，但会让人误以为脚本没跑干净。
    try:
        await client.exit_stack.aclose()
    except Exception:  # noqa: BLE001
        pass

    print(f"\n{'=' * 60}")
    print(f"通过 {ck.passed} 项，失败 {ck.failed} 项")
    if ck.failed:
        print("\n失败项：")
        for ok, name, detail in ck.rows:
            if not ok:
                print(f"  - {name}" + (f"：{detail}" if detail else ""))
    return 0 if ck.failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="验证 Trae Agent 的 MCP 接线")
    ap.add_argument("--config", default=str(DEFAULT_TRAE_HOME / "trae_config.yaml"))
    ap.add_argument("--no-call", action="store_true", help="只做连通检查，不真的调工具")
    ap.add_argument("--trae-home", default=str(DEFAULT_TRAE_HOME), help="trae-agent 源码根目录")
    args = ap.parse_args()

    return asyncio.run(main_async(Path(args.config), call_tools=not args.no_call))


if __name__ == "__main__":
    raise SystemExit(main())
