"""通用 OpenAI 兼容 LLM 适配器。

**一个适配器覆盖三种场景**，无需为中转站写专用代码：
  1. OpenAI 官方直连
  2. **任意中转站 / 聚合站**（几乎都实现 /v1/chat/completions）
  3. 本地 ollama（同样暴露 OpenAI 兼容接口）

针对中转站的可配能力（全部来自 config）：
  - base_url        任意端点，不必是 api.openai.com
  - key_rotation    多 key 轮询 + 故障转移（应对限流 / 单 key 余额耗尽）
  - extra_headers   渠道标识、专属鉴权头
  - model           模型名由中转站决定，本适配器不硬编码
  - timeout/retries 超时 + 指数退避重试（中转站延迟高、偶发 502）
  - fallback        全部失败后降级到另一个登记的供应商（如本地 ollama）
"""

from __future__ import annotations

import base64
import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from ..config import LLMProviderConfig
from .base import LLMProvider, LLMResult, mask_secret
from .http_safety import default_headers


# ---------------------------------------------------------------------------
# SSL 加固：中转站 / LB 常在没发 TLS close_notify 时就掐连接，Python 的 ssl 会因此抛
# `[SSL: UNEXPECTED_EOF_WHILE_READING]`。给它挂一个带 `OP_IGNORE_UNEXPECTED_EOF`
# 的上下文（Python 3.7+ 标准解法），把"对端粗暴关连接"当成正常结束而非异常 ——
# 只要响应体读全了就不报错；若服务器真在中途断了，read() 返回的是截断内容，
# 下游 JSON 解析会失败并被 `complete()` 的退避重试兜住。
#
# 同时强制 `Connection: close`：每条请求走新连接，避免复用被服务器关掉的
# keep-alive 连接（urllib 默认 keep-alive，连续多次调用时复用死连接也会撞 SSL EOF）。
# ---------------------------------------------------------------------------
def _build_opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    opt = getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
    if opt:
        ctx.options |= opt
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


_OPENER = _build_opener()


def _explain_llm_error(raw: str, cfg: Any) -> RuntimeError:
    """把文本模型侧的原始错误翻成**能行动**的中文提示（R-42）。

    与图/视频侧的 `_explain_media_error` 同源同思路，独立实现而不是抽公共模块 ——
    两边要匹配的关键词与给出的下一步并不一样（文本侧多一条"模型名可能不对"），
    真要合并时再抽，避免现在就把两条独立链路绑在一起。

    为什么需要：便携版首用最典型的失败就是"没配 key / 配错地址"，
    而服务端回的是 `401 invalid_api_key` 或 `insufficient_user_quota`，
    **完全不提"你还没在设置里填密钥"**，用户只会反复重试或去充值。
    原始串一律保留在末尾，便于排障对照。
    """
    low = raw.lower()
    pid = getattr(cfg, "id", "?")
    base = getattr(cfg, "base_url", "") or ""
    tail = f"\n原始错误：{raw[:400]}"

    if ("insufficient_user_quota" in low or "用户额度不足" in raw
            or "insufficient_quota" in low or "billing_hard_limit_reached" in low
            or "exceeded your current quota" in low):
        return RuntimeError(
            f"文本模型供应商账号额度不足（{pid}，{base}）。\n"
            f"请二选一：\n"
            f"  ① 到该服务商后台给账号充值；\n"
            f"  ② 在「设置」里换成自己有余额的供应商（换完需重启软件）。\n"
            f"如果这台电脑是从别人那里拿到的软件包，多半是第②种情况 —— "
            f"包里带着原开发者的服务商配置，你的密钥并没有跟着包走。" + tail)

    if ("401" in raw or "403" in raw or "invalid_api_key" in low
            or "unauthorized" in low or "invalid authorization" in low
            or "authentication" in low or "密钥" in raw or "鉴权" in raw):
        return RuntimeError(
            f"文本模型鉴权失败（{pid}）：密钥无效或没配对。\n"
            f"请到「设置」页的「文本大模型」里检查：\n"
            f"  · 地址：{base or '（未配置）'}\n"
            f"  · 密钥：是否已填写、是否属于这个服务商\n"
            f"  · 模型名：{getattr(cfg, 'model', '') or '（未配置）'}"
            f" —— 密钥对但模型名不对时，部分中转站也回 401/403\n"
            f"保存后需重启软件才会生效。" + tail)

    if "404" in raw or "model_not_found" in low or "does not exist" in low:
        return RuntimeError(
            f"文本模型地址或模型名不存在（{pid}）。\n"
            f"请核对：\n"
            f"  · 地址是否多了/少了 `/v1`：{base or '（未配置）'}\n"
            f"  · 模型名是否是该服务商实际支持的：{getattr(cfg, 'model', '') or '（未配置）'}"
            f"（不同中转站命名不同，需按对方文档填）" + tail)

    if "429" in raw or "rate limit" in low or "too many requests" in low:
        return RuntimeError(
            f"被限流（{pid}）：请求太频繁。稍等几十秒再试，或换一把 key。" + tail)

    return RuntimeError(raw)


class OpenAICompatibleLLM(LLMProvider):
    def __init__(
        self,
        cfg: LLMProviderConfig,
        fallback: Optional[LLMProvider] = None,
    ) -> None:
        self.cfg = cfg
        self.fallback = fallback
        self._keys: List[str] = cfg.resolve_keys()
        self._cursor = 0

    # ---------- key 轮换 ----------
    def _next_key(self) -> Optional[str]:
        if not self._keys:
            return None
        key = self._keys[self._cursor % len(self._keys)]
        self._cursor += 1
        return key

    def _require_key(self) -> str:
        """取一把 key；**一把都没有就直接抛可读错误**（R-42）。

        为什么必须前置判：`_next_key()` 在无 key 时返回 None，而下游 `_call`
        会带着空 Authorization 照样发请求 → 服务端回 401 → 再经 `retries`
        次重试（每次还 `time.sleep` 指数退避，上限 8s）→ 最后抛出的是一串
        夹杂 `keyNone` 的晦涩原文。实测便携版首次使用正是这个症状：
        分镜脚本生成失败，但提示里**完全没提"你还没配 API 密钥"**，
        用户只知道"失败"，不知道该去设置页填什么。

        这里提前拦，并把该去哪儿填写清楚；原始异常串一律保留在末尾。
        """
        key = self._next_key()
        if key:
            return key
        cfg = self.cfg
        raise RuntimeError(
            f"未配置 API 密钥，无法调用文本模型（供应商 `{cfg.id}`）。\n"
            f"请到「设置」页的「文本大模型」里填写：\n"
            f"  · 中转站地址或 OpenAI 官方地址：{cfg.base_url or '（未配置）'}\n"
            f"  · 对应的 API key（保存后会存进系统凭据库，不会写进配置文件）\n"
            f"  · 模型名：{cfg.model or '（未配置）'}\n"
            f"填完保存后**重启软件**再试 —— 配置是启动时读一次的。\n"
            f"如果这台电脑是从别人那里拿到的软件包，那一定需要先配自己的密钥："
            f"密钥不会跟着安装包分发。"
        )

    # ---------- 主入口 ----------
    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kw: Any,
    ) -> LLMResult:
        temp = self.cfg.temperature if temperature is None else temperature
        mtok = self.cfg.max_tokens if max_tokens is None else max_tokens

        payload: Dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temp,
            "max_tokens": mtok,
        }
        payload.update(kw.pop("extra_body", {}) or {})

        attempts = max(1, int(self.cfg.retries))
        errors: List[str] = []

        for i in range(attempts):
            key = self._require_key()
            try:
                return self._call(key, payload)
            except Exception as e:  # noqa: BLE001 - 汇总所有失败后统一降级
                errors.append(f"attempt{i + 1}/key{mask_secret(key)}: {e}")
                # 指数退避，上限 8s
                time.sleep(min(2 ** i, 8))

        # 全部重试失败 → 降级
        if self.fallback is not None:
            return self.fallback.complete(
                system, user, temperature=temp, max_tokens=mtok, **kw
            )

        raise RuntimeError(
            f"LLM 调用失败（provider={self.cfg.id}, base_url={self.cfg.base_url}），"
            f"已重试 {attempts} 次： " + " | ".join(errors)
        )

    # ---------- 多模态（商品图自动解读等） ----------
    def complete_vision(
        self,
        system: str,
        user: str,
        images: List[Tuple[bytes, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
        **kw: Any,
    ) -> LLMResult:
        """OpenAI 兼容视觉：content parts + base64 data URL。

        **只尝试一次、不重试**（调用方自己兜底 —— 图片识别失败应回落默认值，
        而不是让保存请求在退避重试里干等几十秒）。`timeout` 可覆盖配置值。
        """
        temp = self.cfg.temperature if temperature is None else temperature
        mtok = self.cfg.max_tokens if max_tokens is None else max_tokens

        content: List[Dict[str, Any]] = [{"type": "text", "text": user}]
        for data, mime in images:
            b64 = base64.b64encode(data).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })

        payload: Dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": temp,
            "max_tokens": mtok,
        }
        payload.update(kw.pop("extra_body", {}) or {})

        key = self._require_key()
        return self._call(key, payload, timeout=timeout)

    # ---------- 流式多看图（R-32b-D：绕开 Cloudflare 524） ----------
    def complete_vision_stream(
        self,
        system: str,
        user: str,
        images: List[Tuple[bytes, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        **kw: Any,
    ) -> LLMResult:
        """流式多看图：SSE 逐 token 读取，token 持续流动 → Cloudflare 不触发 524。

        与 `complete_vision` 同接口，多一个 `model` 覆盖（反推可指定更快的视觉模型，
        复用同一 base_url 与密钥）。返回累积后的完整文本 + usage。
        """
        temp = self.cfg.temperature if temperature is None else temperature
        mtok = self.cfg.max_tokens if max_tokens is None else max_tokens

        content: List[Dict[str, Any]] = [{"type": "text", "text": user}]
        for data, mime in images:
            b64 = base64.b64encode(data).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })

        payload: Dict[str, Any] = {
            "model": model or self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": temp,
            "max_tokens": mtok,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        payload.update(kw.pop("extra_body", {}) or {})

        key = self._require_key()
        return self._call_stream(key, payload, timeout=timeout)

    # ---------- 单次 HTTP 调用 ----------
    def _call(self, key: Optional[str], payload: Dict[str, Any],
              timeout: Optional[float] = None) -> LLMResult:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"

        # 必须带 UA：urllib 默认的 `Python-urllib/x.y` 会被 Cloudflare 一类 WAF
        # 直接拉黑（返回 403 error code: 1010），而那个报错完全不像鉴权问题 ——
        # 实测某中转站对 Python-urllib 返回 403，换任何 UA 就正常到鉴权层。
        # extra_headers 放在最后，所以用户仍可在 providers.yaml 里覆盖成自己的 UA。
        headers: Dict[str, str] = default_headers({"Content-Type": "application/json"})
        if key:
            headers["Authorization"] = f"Bearer {key}"
        # 中转站渠道头 / 专属鉴权头
        headers.update(self.cfg.extra_headers or {})
        headers["Connection"] = "close"  # 每条请求走新连接，避免复用被掐的 keep-alive

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with _OPENER.open(req, timeout=timeout or self.cfg.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:  # noqa: BLE001
                detail = ""
            raise _explain_llm_error(f"HTTP {e.code}: {detail}", self.cfg)
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}")
        except TimeoutError:
            raise RuntimeError(f"请求超时（{self.cfg.timeout}s）")

        choices = body.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content", "") or ""

        return LLMResult(
            text=text,
            model=body.get("model", self.cfg.model),
            usage=body.get("usage", {}) or {},
            finish_reason=(choices[0].get("finish_reason") or ""),
        )

    # ---------- 流式 HTTP 调用（SSE） ----------
    def _call_stream(self, key: Optional[str], payload: Dict[str, Any],
                    timeout: Optional[float] = None) -> LLMResult:
        """SSE 流式读取 /chat/completions。

        - 响应是 `text/event-stream` → 逐行解析 `data:` 事件，累加 delta.content，
          收尾取 usage（带 `stream_options.include_usage` 时最后一帧带）与 finish_reason；
        - 响应是普通 JSON（中继忽略了 `stream`）→ 回退为整块读取（与 `_call` 同款），
          此时不享受"抗 524"的好处，但至少能正常出结果；
        - `timeout` 在这里是**空闲超时**：只要 token 持续到达就不触发；
          单段空闲超过它才抛"流式读取超时"。

        返回值带 `finish_reason`（`length` = 输出被 max_tokens 上限截断）与
        `stream_complete`（False = 没等到 `[DONE]`/finish_reason，连接被中途掐断）。
        这两个字段让调用方能区分"模型没写完"和"模型写坏了" —— 逐镜写长提示词时
        撞上限是常态，缺了它们只能笼统报一句"不是合法 JSON"。
        """
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers: Dict[str, str] = default_headers({"Content-Type": "application/json"})
        if key:
            headers["Authorization"] = f"Bearer {key}"
        headers.update(self.cfg.extra_headers or {})
        headers["Connection"] = "close"  # 每条请求走新连接，避免复用被掐的 keep-alive

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        to = float(timeout or self.cfg.timeout)

        try:
            resp = _OPENER.open(req, timeout=to)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}")
        except TimeoutError:
            raise RuntimeError(f"请求超时（首字节 {to:g}s 内未返回）")

        ctype = resp.headers.get("Content-Type", "") or ""
        if "text/event-stream" not in ctype:
            # 中继没按 SSE 回：整块读 JSON 兜底
            try:
                body = json.loads(resp.read().decode("utf-8"))
            except TimeoutError:
                raise RuntimeError(f"请求超时（非流式回退，{to:g}s）")
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"流式响应非 SSE 且非合法 JSON：{e}") from e
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
            choices = body.get("choices") or [{}]
            text = (choices[0].get("message") or {}).get("content", "") or ""
            return LLMResult(
                text=text,
                model=body.get("model", payload.get("model", self.cfg.model)),
                usage=body.get("usage", {}) or {},
                finish_reason=(choices[0].get("finish_reason") or ""),
            )

        chunks: List[str] = []
        usage: Dict[str, Any] = {}
        finish_reason = ""
        saw_done = False
        try:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                content = line[5:].strip()
                if content == "[DONE]":
                    saw_done = True
                    break
                try:
                    obj = json.loads(content)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        chunks.append(piece)
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish_reason = fr
                if obj.get("usage"):
                    usage = obj["usage"]
        except TimeoutError:
            raise RuntimeError(f"流式读取超时（{to:g}s 内无新数据）")
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

        return LLMResult(
            text="".join(chunks),
            model=payload.get("model", self.cfg.model),
            usage=usage or {},
            finish_reason=finish_reason,
            stream_complete=bool(saw_done or finish_reason),
        )
