"""工作室后端的 HTTP 转发层。

只干一件事：把 HTTP 的各种失败形态（连不上 / 超时 / 4xx / 5xx）**归一成
`StudioError`**，并附上「人话提示」。agent 看到的错误信息质量，直接决定它
能不能自己纠错 —— 所以 `hint` 不是装饰，是本模块的主要产出。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 30.0

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}


def base_url_from_env() -> str:
    """后端地址。默认本机 8000（单端口托管形态）。"""
    return (os.environ.get("STUDIO_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def is_local_url(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "") in _LOCAL_HOSTS
    except ValueError:
        return False


def _detail_to_text(detail: Any) -> str:
    """FastAPI 的 `detail` 可能是 str，也可能是 pydantic 的校验错误数组。"""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for it in detail:
            if isinstance(it, dict):
                loc = ".".join(str(x) for x in (it.get("loc") or [])
                               if x not in ("body", "query", "path"))
                msg = str(it.get("msg") or "")
                parts.append(f"{loc}: {msg}" if loc else msg)
            else:
                parts.append(str(it))
        return "; ".join(p for p in parts if p) or "参数校验失败"
    if detail is None:
        return "后端返回了错误但没有说明"
    return json.dumps(detail, ensure_ascii=False)


def _status_hint(status: int) -> str:
    if status == 402:
        return ("这是花费上限熔断（P-5）。先用 get_budget 看余额与各级上限，"
                "或在软件「设置 → 花费上限」里调整；也可以先 dry_run 试算。")
    if status == 404:
        return "对象不存在。先用 list_projects / list_shots 拿到真实 id。"
    if status == 422:
        return "参数不合法。对照工具的 inputSchema 检查字段名与类型。"
    if status == 400:
        return "后端拒绝了这次请求，理由见上。"
    if status in (502, 503, 504):
        return ("这是网关 / 代理类错误，不是后端主动返回的。"
                "若地址指向本机：多半是系统代理（HTTP_PROXY）拦下了发往 127.0.0.1 的请求"
                "—— 本客户端对 localhost 已关闭 trust_env，若仍如此请检查是否有"
                "NO_PROXY 缺失或上游代理故障。若指向远程：确认后端真的在跑。")
    if status >= 500:
        return "后端内部错误。看后端进程的 stderr 日志定位（本页看不到）。"
    return ""


class StudioError(Exception):
    """工作室调用失败。`status == 0` 表示连不上（后端多半没启动）。"""

    def __init__(self, message: str, *, status: int = 0, path: str = "",
                 detail: Any = None, hint: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.path = path
        self.detail = detail
        self.hint = hint

    def describe(self) -> str:
        head = f"[{self.status or 'conn'}] {self.path}: {self}" if self.path else str(self)
        return f"{head}\n→ {self.hint}" if self.hint else head


class StudioClient:
    """转发到 `http://127.0.0.1:8000` 的瘦客户端。"""

    def __init__(self, base_url: Optional[str] = None, *,
                 timeout: float = DEFAULT_TIMEOUT,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        self.base_url = (base_url or base_url_from_env()).rstrip("/")
        self.timeout = float(timeout)
        self.local = is_local_url(self.base_url)
        # **本机后端绝不能走系统代理。**
        # 国内环境几乎必配 HTTP_PROXY（为了访问中转站），而 httpx 默认 trust_env=True
        # 会把 `127.0.0.1:8000` 也塞给代理 —— 代理连不上就回 502，于是"后端没启动"
        # 会被误报成"网关错误"，排查方向完全跑偏（实测复现过）。
        # 对 localhost 关掉 env 代理；远程地址仍尊重系统代理。
        self._client = httpx.Client(transport=transport, timeout=timeout,
                                    follow_redirects=True,
                                    trust_env=not self.local)

    # ------------------------------------------------------------ 基础请求
    def request(self, method: str, path: str, *,
                json_body: Optional[Dict[str, Any]] = None,
                params: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> Any:
        url = path if path.startswith("http") else self.base_url + path
        eff_timeout = float(timeout or self.timeout)
        try:
            r = self._client.request(method.upper(), url, json=json_body,
                                     params=params, timeout=eff_timeout)
        except httpx.TimeoutException as e:
            raise StudioError(
                f"请求超时（{eff_timeout:.0f}s）", path=path,
                hint=("后端可能正在跑重活（出片 / 后期混音 / 质检解帧）。"
                      "出片是异步的，请用 render_shot 拿到 job_id 后走 get_job / "
                      "wait_for_job 跟进，不要靠长超时硬等。"),
            ) from e
        except httpx.TransportError as e:
            raise StudioError(
                f"连不上工作室后端（{type(e).__name__}）", path=path,
                hint=(f"确认后端在跑：backend 目录下执行 "
                      f"`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`；"
                      f"当前 STUDIO_BASE_URL={self.base_url}。"),
            ) from e

        if r.status_code >= 400:
            detail = _read_detail(r)
            raise StudioError(_detail_to_text(detail), status=r.status_code,
                              path=path, detail=detail,
                              hint=_status_hint(r.status_code))

        if not r.content:
            return {"ok": True, "status": r.status_code}
        ctype = (r.headers.get("content-type") or "").lower()
        if "json" in ctype:
            return r.json()
        text = r.text
        return text if len(text) <= 4000 else text[:4000] + "…（已截断）"

    def get(self, path: str, *, params=None, timeout=None) -> Any:
        return self.request("GET", path, params=params, timeout=timeout)

    def post(self, path: str, *, json_body=None, params=None, timeout=None) -> Any:
        return self.request("POST", path, json_body=json_body, params=params,
                            timeout=timeout)

    def patch(self, path: str, *, json_body=None, timeout=None) -> Any:
        return self.request("PATCH", path, json_body=json_body, timeout=timeout)

    def close(self) -> None:
        self._client.close()


def _read_detail(r: httpx.Response) -> Any:
    try:
        body = r.json()
    except Exception:  # noqa: BLE001 - 非 JSON 错误体（如网关 HTML）
        return (r.text or "")[:500] or f"HTTP {r.status_code}"
    if isinstance(body, dict) and "detail" in body:
        return body["detail"]
    return body
