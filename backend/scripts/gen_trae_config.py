#!/usr/bin/env python
"""为 Trae Agent 生成 trae_config.yaml —— 把 ai-video-studio 的 MCP 工具接给它当手。

## 为什么用脚本生成，而不是手写一份 yaml

**1. 单一真相（最重要）。** Trae Agent 需要一个 OpenAI 兼容 endpoint + 一份 API key。
ai-video-studio 的 `providers.yaml` 里已经有 endpoint 和模型，密钥则存在加密的
secret_store（OS 凭据库 / 本地加密文件）里。手抄一份 yaml = 密钥出现第二份拷贝，
而且你在软件里换了模型以后 agent 不会跟着变。本脚本每次都从那份真相重新生成，
所以永远同步 —— 生成的 yaml 是**可再生产物**，不是资产。

**2. `provider` 不能写 `openai`。** 这是最容易踩的坑：

- `provider: openai` → `OpenAIClient`，用的是 OpenAI **Responses API**（`/v1/responses`）。
  绝大多数中转站 / 聚合站只实现 `/v1/chat/completions`，结果就是 404/400，
  而且报错信息非常含糊，会浪费你半天时间。
- `provider: doubao` → `DoubaoClient(OpenAICompatibleClient)`，用 **chat/completions**，
  并且它的 `create_client()` **直接使用配置里的 base_url，不会硬编码覆盖成火山方舟**。

  所以「接任意 OpenAI 兼容接口」这件事，靠的是 `provider: doubao`。
  名字叫 doubao，实质是通用的 OpenAI-compatible client。

**3. MCP 接线。** ai-video-studio 已有 stdio MCP server（`backend/mcp_server.py`），
暴露 18 个工具（建项目 / 写分镜 / lint / 渲染 / QC / 混流交付 …）。
本脚本负责把它挂进 Trae Agent 的 `mcp_servers` 并填好环境变量。

## 用法

    # 生成到 trae-agent 仓库目录（默认，且该路径已被上游 .gitignore 忽略）
    python scripts/gen_trae_config.py

    # 先看内容不落盘
    python scripts/gen_trae_config.py --dry-run

    # 换个供应商（用 providers.yaml 里登记过的 id）
    python scripts/gen_trae_config.py --provider-id relay-a

用例参见 `docs/TRAE-AGENT.md`。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

BACKEND = Path(__file__).resolve().parents[1]

# 允许从任意工作目录启动：`import app.xxx` 才能解析
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import yaml  # noqa: E402
from app.config import load_providers_config  # noqa: E402

# trae-agent 源码根目录（同时也是 trae-cli 的默认工作点：它默认找 ./trae_config.yaml）
DEFAULT_TRAE_HOME = Path.home() / ".workbuddy" / "third_party" / "trae-agent" / "trae-agent-main"

DEFAULT_MODEL_PROVIDER_NAME = "studio_llm"
DEFAULT_MODEL_NAME = "trae_agent_model"
SERVER_KEY = "ai-video-studio"

ALL_TOOLS = ["bash", "str_replace_based_edit_tool", "sequentialthinking", "task_done"]


# ---------------------------------------------------------------- 密钥脱敏


def mask(key: str) -> str:
    """密钥预览：只留头尾各 4 位，看得出是哪一份又不泄露。"""
    if not key:
        return "(空)"
    if len(key) <= 10:
        return key[:2] + "*" * (len(key) - 2)
    return f"{key[:4]}…{key[-4:]} (len={len(key)})"


def _fallback_keys() -> List[str]:
    """providers.yaml 没配出 key 时的兜底来源。

    顺序刻意排在 secret_store 之后：软件里配过的优先，
    环境变量只是没配的时候拿来应急。
    """
    out: List[str] = []
    for name in ("MODEL_API_KEY", "OPENAI_API_KEY"):
        v = os.environ.get(name)
        if v:
            out.append(v)
    return out


def build_config(
    *,
    api_key: str,
    base_url: str,
    model: str,
    temperature: float,
    max_tokens: int,
    tools: List[str],
    max_steps: int,
    mcp_python: str,
    mcp_script: str,
    backend_url: str,
    allow_spend: bool,
    max_retries: int = 10,
) -> Dict[str, Any]:
    """拼出完整的 trae_config.yaml 内容（dict 形态）。

    字段含义与坑位见文件顶部说明；`allow_mcp_servers` 是必须的 ——
    不在这个白名单里的 server 不会被启用，且**没有任何报错**。

    `max_retries` 按用途给不同的值（**别一刀切**）：
      - 手动跑流水线的 agent（默认 10）：任务链长、每步便宜，重试划算；
      - 网页版「生成视频提示词」（传 1）：单次请求要吐数千 token，中转站一次
        抖动就是几分钟。10 次重试只会把"卡住"从 5 分钟拖成 15 分钟以上，
        而用户全程看不到任何反馈 —— 快速失败比慢速重试有用地多。
        详见 docs/TRAE-AGENT.md「生文任务的步数与超时预算」。
    """
    return {
        "agents": {
            "trae_agent": {
                # Lakeview 会额外配一个模型专门做「步骤摘要」。
                # 中转站未必支持它要求的调用形态，且每次交互多一次付费请求，
                # 默认关掉；想要摘要时改这里并补一个 lakeview_model。
                "enable_lakeview": False,
                "model": DEFAULT_MODEL_NAME,
                "max_steps": max_steps,
                "tools": tools,
            }
        },
        "allow_mcp_servers": [SERVER_KEY],
        "mcp_servers": {
            SERVER_KEY: {
                "command": mcp_python,
                "args": [mcp_script],
                "cwd": str(BACKEND),
                "env": {
                    "STUDIO_BASE_URL": backend_url,
                    # 花钱动作（渲染/最终混流）默认禁止。想让 agent 真出片时
                    # 显式加 --allow-spend，别默认打开。
                    "STUDIO_MCP_ALLOW_SPEND": "1" if allow_spend else "0",
                    # MCP server 走 stdio，中文工具返回值要求 UTF-8；
                    # Windows 默认 GBK 会在写中文时炸。
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
            }
        },
        "model_providers": {
            DEFAULT_MODEL_PROVIDER_NAME: {
                "api_key": api_key,
                # 见顶部说明：doubao = 通用 OpenAI 兼容 client（chat/completions）
                "provider": "doubao",
                "base_url": base_url,
            }
        },
        "models": {
            DEFAULT_MODEL_NAME: {
                "model_provider": DEFAULT_MODEL_PROVIDER_NAME,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": 1,
                "top_k": 0,
                "max_retries": max_retries,
                "parallel_tool_calls": True,
            }
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 Trae Agent 的 trae_config.yaml")
    ap.add_argument("--out", default=str(DEFAULT_TRAE_HOME / "trae_config.yaml"),
                    help="输出路径（默认落到 trae-agent 仓库根，该路径已被上游 .gitignore 忽略）")
    ap.add_argument("--providers", default=str(BACKEND / "providers.yaml"))
    ap.add_argument("--provider-id", default="", help="指定 providers.yaml 里的供应商 id（默认用 llm.active）")
    ap.add_argument("--max-steps", type=int, default=40, help="agent 单任务最大步数（防止失控跑钱）")
    ap.add_argument("--tools", default=",".join(ALL_TOOLS), help="启用哪些工具，逗号分隔")
    ap.add_argument("--mcp-python", default=sys.executable,
                    help="运行 mcp_server.py 的解释器（必须装有 httpx，默认用当前解释器）")
    ap.add_argument("--backend-url", default="http://127.0.0.1:8000")
    ap.add_argument("--allow-spend", action="store_true",
                    help="允许 agent 触发花钱动作（渲染 / 最终混流）。默认关闭")
    ap.add_argument("--api-key", default="",
                    help="直接提供 API key（覆盖自动获取）。用于临时验证接线 / CI，"
                         "正常在本机软件里用 Secret Store 存就行")
    ap.add_argument("--skip-backend-check", action="store_true",
                    help="不检查后端是否在运行")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    args = ap.parse_args()

    cfg = load_providers_config(args.providers)
    if args.provider_id:
        prov = cfg.llm_by_id(args.provider_id)
        if prov is None:
            print(f"[x] providers.yaml 里没有 id={args.provider_id} 的文本大模型供应商", file=sys.stderr)
            print(f"    已登记的 id: {', '.join(c.id for c in cfg.llm.list)}", file=sys.stderr)
            return 2
    else:
        prov = cfg.active_llm_config()

    if args.api_key:
        keys = [args.api_key]
    else:
        keys = prov.resolve_keys() or _fallback_keys()
    if not keys:
        print("[x] 没取到任何 API key。二选一：", file=sys.stderr)
        print(f"    1) 在软件「设置 → 供应商」里给 `{prov.id}` 填密钥（推荐，落在加密存储里）", file=sys.stderr)
        print("    2) 设置环境变量 OPENAI_API_KEY 或 MODEL_API_KEY", file=sys.stderr)
        return 2

    tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    unknown = [t for t in tools if t not in ALL_TOOLS]
    if unknown:
        print(f"[x] 未知工具名: {unknown}；可用：{ALL_TOOLS}", file=sys.stderr)
        return 2

    doc = build_config(
        api_key=keys[0],
        base_url=prov.base_url,
        model=prov.model,
        temperature=prov.temperature,
        max_tokens=prov.max_tokens,
        tools=tools,
        max_steps=args.max_steps,
        mcp_python=args.mcp_python,
        mcp_script=str(BACKEND / "mcp_server.py"),
        backend_url=args.backend_url,
        allow_spend=args.allow_spend,
    )

    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False)

    out = Path(args.out)
    if args.dry_run:
        safe = text.replace(keys[0], "«REDACTED»") if keys[0] else text
        print(safe)
        print("--- 未落盘（--dry-run）---")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    print(f"[✓] 已生成 {out}")
    print(f"    文本大模型供应商 : {prov.id} ({prov.model})")
    print(f"    endpoint        : {prov.base_url}")
    print(f"    key             : {mask(keys[0])}" + (f"（另有 {len(keys)-1} 份轮换）" if len(keys) > 1 else ""))
    print(f"    工具            : {', '.join(tools)}")
    print(f"    最大步数        : {args.max_steps}")
    print(f"    花钱动作        : {'允许' if args.allow_spend else '禁止（默认）'}")
    print(f"    MCP server      : {SERVER_KEY} → {args.backend_url}")
    # MCP server 跑在一个子进程里，用的解释器未必是当前这个。
    # 那个解释器缺 httpx 时，症状是 agent 那边"连不上 MCP server"，
    # 而不是一句清楚的 ImportError —— 花两秒钟提前确认能省很多排查。
    try:
        import subprocess

        probe = subprocess.run(
            [args.mcp_python, "-c", "import httpx, sys; print(httpx.__version__, file=sys.stderr)"],
            capture_output=True, timeout=20,
        )
        ver = probe.stderr.decode(errors="replace").strip().split()[-1] if probe.stderr else ""
        if probe.returncode == 0:
            print(f"    MCP 解释器       : 就绪 (httpx {ver})")
        else:
            print(f"    [!] 指定的 MCP 解释器缺少 httpx：{args.mcp_python}")
            print("        用 --mcp-python 指向一个装有 httpx 的解释器（后端 requirements.txt 里有）")
    except Exception as e:  # noqa: BLE001
        print(f"    [!] 无法验证 MCP 解释器：{type(e).__name__}: {e}")

    # MCP server 只是转发层，真正的业务在后端。后端没起来时 agent 的每一次工具调用
    # 都会失败，而且错误信息很容易被误读成"agent 不会用工具" —— 这里提前点破。
    if not args.skip_backend_check:
        try:
            import httpx

            r = httpx.get(args.backend_url.rstrip("/") + "/api/stats", timeout=3.0)
            print(f"    后端状态        : 在线 ({r.text[:120]})")
        except Exception as e:  # noqa: BLE001
            print(f"    [!] 后端未在线（{type(e).__name__}）—— 现在 agent 调 MCP 工具会全部失败")
            print("        启动命令：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000")
    print()
    print("下一步（在 trae-agent 仓库目录下执行，或加 --config-file 指定）：")
    print(f'    trae-cli run "列出所有项目，并告诉我每个项目有几个分镜" '
          f'--config-file "{out}" --working-dir "C:/Users/Administrator/WorkBuddy/图生视频skill/ai-video-studio"')
    print()
    print("提醒：该 yaml 含明文密钥，已落在上述 trae-agent 仓库目录（上游 .gitignore 已忽略）。")
    print("      密钥失效或换供应商后，重跑本脚本即可，不要手改 yaml。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
