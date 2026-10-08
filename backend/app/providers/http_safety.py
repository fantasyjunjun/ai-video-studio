"""计费安全的 HTTP 层（P-2）。

出片是**"提交即计费"的非幂等**操作：一次误重试就是一次真金白银的重复下单。
所以把"能不能重试"的判断集中在这里，而不是散落在各适配器：

  - **可重试请求（`billable=False`）** —— 轮询 / 下载结果 / 上传参考图。
    这些都是幂等或无害的：429、5xx、超时、网络错误一律按指数退避重试若干次，不产生费用。
  - **计费请求（`billable=True`）** —— 提交生成任务（POST /prompt、POST /{workflow}）。
      * HTTP 429 → **可重试**：429 表示服务端限流、请求未被处理，没建任务也没计费；
      * 超时 / 网络断开 / 5xx → **绝不自动重试**，抛 `BillingRiskError`。
        请求可能已被服务端受理并计费，盲目重试会重复下单、重复扣费；
      * **连接被重置 / 响应体被截断**（`ConnectionResetError`、`IncompleteRead`…）→ 同上，
        抛 `BillingRiskError`。这类异常在 `resp.read()` 时是**裸 OSError**，
        不是 `URLError` 的子类，**必须单独接住**，否则会被误判成"明确失败"，
        钱可能已花掉却没人去对账（实测事故：A5 报 `ConnectionResetError [WinError 10054]`）；
      * 其它 4xx → 抛 `RuntimeError`（明确的客户端错误，服务端未受理）。

`BillingRiskError` 的语义就是给上层一个"结果未知"的信号：**别重试，去平台核对**。
调用方（渲染队列）会据此把台账标成 `unknown` 并让用户人工对账。
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Optional

# 出站请求的默认 User-Agent。别用 urllib 默认的 `Python-urllib/x.y`
# —— 它被 Cloudflare 等 WAF 的 bot 规则直接拉黑，详见 ensure_user_agent 的说明。
DEFAULT_USER_AGENT = "ai-video-studio/0.7 (OpenAI-compatible client)"

# 与 llm_openai_compat 同款 SSL 加固：中转站 / LB 常在没发 TLS close_notify 时就掐连接，
# Python 会抛 `[SSL: UNEXPECTED_EOF_WHILE_READING]`。挂 `OP_IGNORE_UNEXPECTED_EOF` 把这种
# 粗暴关连接当成正常结束；图床上传/下载同样走裸 urlopen，可能撞同一类问题。
_OPENER_HTTPS = None


def _https_opener() -> urllib.request.OpenerDirector:
    global _OPENER_HTTPS
    if _OPENER_HTTPS is None:
        ctx = ssl.create_default_context()
        opt = getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
        if opt:
            ctx.options |= opt
        _OPENER_HTTPS = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx))
    return _OPENER_HTTPS


def ensure_user_agent(req: urllib.request.Request) -> None:
    """给 Request 补上默认 User-Agent（已经设过的话就不动）。

    **为什么要有这个东西**：`urllib` 的默认 UA 是 `Python-urllib/3.x`。
    这个字符串被很多 CDN / WAF（尤其是 Cloudflare 的 bot 规则）直接拉黑 ——
    实测某 OpenAI 兼容站点对 `Python-urllib/3.12` 直接返回
    `HTTP 403 error code: 1010`，而同样的请求换成任何别的 UA（包括**完全不带**）
    就能正常打到鉴权层拿到 401。

    症状特别容易误判：报错里**既看不出 IP 被封、也和 API key 无关**（key 根本没被读到），
    人只会反复怀疑自己的令牌配置错了。

    所以统一发一个中性的产品 UA。用户可以在 providers.yaml 的 `extra_headers`
    里写自己的 User-Agent 来覆盖（本函数只在**缺失**时才补）。
    """

    if not DEFAULT_USER_AGENT:
        return
    try:
        if not req.get_header("User-agent"):
            req.add_header("User-Agent", DEFAULT_USER_AGENT)
    except Exception:  # noqa: BLE001 - UA 兜不了也不该让主流程挂掉
        pass


def default_headers(extra: Optional[dict] = None) -> dict:
    """给 dict 形态的 headers 补上同样的默认 UA（给不走 Request 的调用点用）。"""
    h = dict(extra or {})
    h.setdefault("User-Agent", DEFAULT_USER_AGENT)
    return h


class BillingRiskError(RuntimeError):
    """计费请求在"结果未知"的失败后抛出。

    "结果未知" = 请求已发出，但服务端是否受理、是否已计费无从判断
    （超时 / 连接断开 / 5xx）。正确处理是**不要重试**：
    先到平台按时间或任务列表核对，确认没有重复任务再决定下一步。
    """

    def __init__(self, message: str, *, provider: str = "",
                 endpoint: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.endpoint = endpoint
        self.detail = detail


def _read_error_body(err: urllib.error.HTTPError) -> str:
    try:
        return err.read().decode("utf-8", "ignore")[:300]
    except Exception:  # noqa: BLE001 - 读不到就算了，别让排障代码再抛异常
        return ""


def _retry_after_seconds(err: urllib.error.HTTPError, fallback: float) -> float:
    """优先尊重服务端的 Retry-After（秒），封顶 30s，避免被要求睡到天荒地老。"""
    try:
        raw = err.headers.get("Retry-After") if err.headers else None
    except Exception:  # noqa: BLE001
        raw = None
    if raw:
        try:
            return max(0.0, min(float(raw), 30.0))
        except (TypeError, ValueError):
            pass
    return fallback


def _backoff(base: float, attempt: int) -> float:
    return base * (2 ** (attempt - 1))


def request_bytes(
    req: urllib.request.Request,
    *,
    billable: bool,
    timeout: float,
    max_retries: int = 2,
    retry_base: float = 0.8,
    provider: str = "",
    endpoint: Optional[str] = None,
    sleep=time.sleep,
) -> bytes:
    """发一个请求并返回响应体。

    :param billable: True = 该请求会导致计费（提交生成任务），失败时**不自动重试**。
    :param max_retries: 可重试请求的最大重试次数（不含首次）。
    :param sleep: 注入点，便于测试替换成假 sleep（避免测试真的等）。
    """
    url = endpoint or req.full_url
    # 兜底：就算调用点忘了设 UA，这里也补上。缺 UA 的代价是被 WAF 静默拦掉，
    # 而报错信息（403 + 一串 HTML）完全看不出原因。
    ensure_user_agent(req)
    last_detail = ""
    for attempt in range(1, max_retries + 2):
        try:
            # 用带 SSL 加固的 opener（忽略对端未发 close_notify 的粗暴关连接），
            # 避免 `[SSL: UNEXPECTED_EOF_WHILE_READING]` 把可重试请求误判成明确失败。
            if url.startswith("https"):
                with _https_opener().open(req, timeout=timeout) as resp:  # noqa: S310
                    return resp.read()
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return resp.read()
        except urllib.error.HTTPError as e:
            code = int(getattr(e, "code", 0) or 0)
            last_detail = _read_error_body(e)
            if code == 429:
                if attempt <= max_retries:
                    sleep(_retry_after_seconds(e, _backoff(retry_base, attempt)))
                    continue
                raise RuntimeError(
                    f"HTTP 429 限流，重试 {max_retries} 次仍失败: {last_detail}"
                ) from e
            if 500 <= code < 600:
                if not billable and attempt <= max_retries:
                    sleep(_backoff(retry_base, attempt))
                    continue
                if billable:
                    raise BillingRiskError(
                        f"提交请求收到 HTTP {code}，服务端可能已受理并计费；"
                        f"请勿盲目重试（会重复下单），请到平台核对任务列表。"
                        + (f" 详情: {last_detail}" if last_detail else ""),
                        provider=provider, endpoint=url, detail=last_detail,
                    ) from e
                raise RuntimeError(f"HTTP {code}: {last_detail}") from e
            # 其它 4xx：明确的客户端错误，服务端未受理，不重试
            raise RuntimeError(f"HTTP {code}: {last_detail}") from e
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:  # noqa: UP041
            reason = getattr(e, "reason", None) or e
            last_detail = str(reason)
            if not billable and attempt <= max_retries:
                sleep(_backoff(retry_base, attempt))
                continue
            if billable:
                raise BillingRiskError(
                    "提交请求超时/网络断开，服务端可能已受理并计费；"
                    "请勿盲目重试（会重复下单），请到平台核对任务是否已创建。",
                    provider=provider, endpoint=url, detail=last_detail,
                ) from e
            raise RuntimeError(f"网络错误/超时: {last_detail}") from e
        # 连接层异常（**读响应体时**才暴露的那一类）。
        # 必须单独接：`resp.read()` 抛的是**裸** `ConnectionResetError` /
        # `IncompleteRead`，它们不是 `URLError` 的子类，会被上面的分支漏过去。
        # 漏掉的后果很严重 —— 计费请求（出片提交）遇到它就变成"明确失败"，
        # 台账落成 `failed` 而不是 `unknown`，**钱可能已花掉却没人去对账**。
        # 实测事故：A5 出片报 `ConnectionResetError: [WinError 10054]`。
        # 注意顺序：`URLError` 本身就是 `OSError` 子类，所以这档必须排在它**后面**。
        except (ConnectionError, OSError, http.client.HTTPException) as e:
            last_detail = f"{type(e).__name__}: {e}"
            if not billable and attempt <= max_retries:
                sleep(_backoff(retry_base, attempt))
                continue
            if billable:
                raise BillingRiskError(
                    "提交请求收响应时连接被重置/截断，服务端可能已受理并计费；"
                    "请勿盲目重试（会重复下单），请到平台核对任务是否已创建。"
                    + f" 详情: {last_detail}",
                    provider=provider, endpoint=url, detail=last_detail,
                ) from e
            raise RuntimeError(f"连接中断: {last_detail}") from e

    # 正常流程不会走到这里（循环内要么 return 要么 raise）
    raise RuntimeError(f"请求失败: {last_detail}")


def request_json(req: urllib.request.Request, **kw: Any) -> Any:
    return json.loads(request_bytes(req, **kw).decode("utf-8"))
