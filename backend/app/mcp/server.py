"""MCP 协议层（stdio + JSON-RPC 2.0）。

**两条铁律，违反任何一条客户端都会静默连不上：**

1. **stdout 只能出现 JSON-RPC 消息**。任何 `print` / 库的 banner / 警告写到
   stdout 都会污染协议帧。本模块所有日志一律走 stderr。
2. **stdout 必须是 UTF-8**。Windows 默认 stdout 编码是 GBK（取决于 locale），
   中文工具返回值会直接抛 UnicodeEncodeError —— 所以 `main()` 里显式
   `reconfigure(encoding="utf-8")`，且写入用 `errors="replace"` 兜底。

协议面刻意收窄：只实现 `initialize` / `tools/list` / `tools/call` / `ping`
与若干空列表。没有 resources、没有 prompts、没有 sampling —— 不需要的
capability 不声明，客户端就不会来问。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, IO, List, Optional

from .client import StudioClient, StudioError
from .tools import GROUP_ORDER, SPEND_ENV, TOOLS, TOOLS_BY_NAME, allow_spend

SERVER_NAME = "ai-video-studio"
SERVER_VERSION = "0.7.0"
SERVER_TITLE = "AI Video Studio（本地电商视频工作室）"

# 支持协商的协议版本，按新→旧。客户端报什么就在表里回什么，报不认识的回最新。
SUPPORTED_PROTOCOLS: List[str] = ["2025-06-18", "2025-03-26", "2024-11-05"]
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[0]

# 单次工具结果最大字符数（超了截断并提示，避免把 agent 的上下文冲爆）
MAX_RESULT_CHARS = 40000


# ---------------------------------------------------------------- 输出


def _write(stream: IO[str], obj: Dict[str, Any]) -> None:
    """写一帧 JSON-RPC。紧凑分隔符 + 单行 + 立即 flush（stdio 是交互式的）。"""
    stream.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def _log(stream: Optional[IO[str]], msg: str) -> None:
    if stream is None:
        return
    try:
        stream.write(f"[mcp] {msg}\n")
        stream.flush()
    except Exception:  # noqa: BLE001 - 日志写不出去也不能中断服务
        pass


def _err(mid: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _dump(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = (text[:MAX_RESULT_CHARS]
                + f"\n…（输出超过 {MAX_RESULT_CHARS} 字符已截断；"
                  "请用更精确的过滤参数，或缩小查询范围后重试）")
    return text


def _tool_ok(value: Any) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": _dump(value)}], "isError": False}


def _tool_error(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---------------------------------------------------------------- 方法实现


def _instructions() -> str:
    lines = [
        "本地 AI 视频工作室（电商带货短视频）。后端是单端口 REST 服务，",
        "本 MCP 只是它的薄壳 —— 所有工具都直接转发到后端，不在这里做二次编排。",
        "",
        "**流水线顺序**：create_project → generate_storyboard → (list_shots 复核)",
        "→ render_shot → wait_for_job → run_final → verify_delivery。",
        "质检用 qc_project，文案合规用 compliance_scan，花钱前先 get_budget。",
        "",
        "**安全边界**：",
        f"· 计费工具（render_shot / generate_storyboard / reverse_video）默认**被拒绝**，",
        f"  需要客户端环境变量 {SPEND_ENV}=1 才放行；未授权时不会向任何供应商发请求。",
        "· 没有删除类工具：你删不掉项目和资产。",
        "· 出片是异步的。render_shot 立刻回 job_id，用 wait_for_job 跟进，别硬等。",
        "· render_shot 传 dry_run=true 可免费试算费用，正式出片前建议先试算一次。",
        "",
        "工具分组：" + " / ".join(
            f"{g}({sum(1 for t in TOOLS if t.group == g)})" for g in GROUP_ORDER),
    ]
    return "\n".join(lines)


def _r_initialize(params: Dict[str, Any], client: StudioClient) -> Dict[str, Any]:
    want = params.get("protocolVersion")
    ver = want if want in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
    return {
        "protocolVersion": ver,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                       "title": SERVER_TITLE},
        "instructions": _instructions(),
    }


def _r_ping(params: Dict[str, Any], client: StudioClient) -> Dict[str, Any]:
    return {}


def _r_tools_list(params: Dict[str, Any], client: StudioClient) -> Dict[str, Any]:
    return {"tools": [t.spec() for t in TOOLS]}


def _r_tools_call(params: Dict[str, Any], client: StudioClient) -> Dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return _tool_error("arguments 必须是对象")

    tool = TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
    if tool is None:
        return _tool_error(f"未知工具 {name!r}。可用工具见 tools/list。")

    # ---- 花费门控：未授权时**在本地拒绝**，绝不发出请求 ----
    if tool.spend and not allow_spend():
        return _tool_error(
            f"未授权花费，已拒绝：{name} 会调用付费接口，但当前 MCP 会话没有花钱权限。\n"
            f"· 要放开：在 MCP 客户端配置的 env 里加 {SPEND_ENV}=1，然后重启客户端\n"
            f"· 只想看费用：改用 render_shot 的 dry_run=true（免费试算）\n"
            f"（本次拒绝在本地完成，没有向任何供应商发出请求）")

    try:
        value = tool.handler(client, args)
    except StudioError as e:
        return _tool_error(e.describe())
    except Exception as e:  # noqa: BLE001 - 工具异常不能把服务打挂
        return _tool_error(f"{type(e).__name__}: {e}")
    return _tool_ok(value)


def _r_resources_list(params, client) -> Dict[str, Any]:
    return {"resources": []}


def _r_resource_templates(params, client) -> Dict[str, Any]:
    return {"resourceTemplates": []}


def _r_prompts_list(params, client) -> Dict[str, Any]:
    return {"prompts": []}


def _r_set_level(params, client) -> Dict[str, Any]:
    return {}


_METHODS: Dict[str, Callable[[Dict[str, Any], StudioClient], Dict[str, Any]]] = {
    "initialize": _r_initialize,
    "ping": _r_ping,
    "tools/list": _r_tools_list,
    "tools/call": _r_tools_call,
    # 客户端会试探性问这些；回空列表比回 -32601 更友好（少了报错噪声）
    "resources/list": _r_resources_list,
    "resources/templates/list": _r_resource_templates,
    "prompts/list": _r_prompts_list,
    "logging/setLevel": _r_set_level,
}

# 通知（无 id，永不回响应）
_KNOWN_NOTIFICATIONS = {
    "notifications/initialized", "notifications/cancelled",
    "notifications/progress", "notifications/roots/list_changed",
}


# ---------------------------------------------------------------- 主处理


def handle(msg: Any, client: StudioClient,
           *, log_stream: Optional[IO[str]] = None) -> Optional[Dict[str, Any]]:
    """处理一条消息。返回 None 表示"这是通知，不回响应"。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "Invalid Request：消息必须是 JSON 对象")

    has_id = msg.get("id") is not None
    method = msg.get("method")
    mid = msg.get("id")

    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
        if not has_id:
            return None
        return _err(mid, -32600, "Invalid Request：需要 jsonrpc=\"2.0\" 与字符串 method")

    if not has_id:
        # 通知：只落日志
        if method not in _KNOWN_NOTIFICATIONS:
            _log(log_stream, f"忽略未知通知 {method}")
        return None

    fn = _METHODS.get(method)
    if fn is None:
        if method.startswith("notifications/"):
            return None
        return _err(mid, -32601, f"未知方法 {method}")
    params = msg.get("params")
    try:
        result = fn(params if isinstance(params, dict) else {}, client)
    except Exception as e:  # noqa: BLE001
        return _err(mid, -32603, f"内部错误：{type(e).__name__}: {e}")
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin: IO[str], stdout: IO[str], client: StudioClient,
          *, log_stream: Optional[IO[str]] = None) -> int:
    """主循环：按行读 JSON-RPC，按行写响应。EOF 即退出。"""
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            # -32700：协议层解析失败。id 未知，规范允许用 null
            _write(stdout, _err(None, -32700, f"JSON 解析失败：{e.msg}"))
            continue
        resp = handle(msg, client, log_stream=log_stream)
        if resp is not None:
            _write(stdout, resp)
    return 0


def _setup_stdio() -> None:
    """把 stdout/stderr 切成 UTF-8 + LF。Windows 上不做这一步，中文返回值必炸。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (AttributeError, ValueError):
            pass  # 非 TextIOWrapper（如已被重定向）就跳过


def main(argv: Optional[List[str]] = None) -> int:
    _setup_stdio()
    client = StudioClient()
    _log(sys.stderr, f"启动 {SERVER_NAME} {SERVER_VERSION}；"
                     f"base_url={client.base_url}；"
                     f"allow_spend={'1' if allow_spend() else '0'}；"
                     f"工具数={len(TOOLS)}")
    try:
        return serve(sys.stdin, sys.stdout, client, log_stream=sys.stderr)
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()
