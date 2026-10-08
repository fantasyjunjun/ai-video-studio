"""通用 OpenAI 兼容媒体适配器（P-3）。

把"任意中转站可配"从 **LLM** 扩到 **生图 / 生视频**：

  - LLM 层早就是 `type: openai_compatible`（换 base_url 就能接任意中转站）；
  - 图/视频插槽此前只有 ComfyUI / AutoDL 两个专属适配器，
    想接"某个 OpenAI 兼容的中转站出图/出片"只能等有人写新适配器。
  - P-3 打通这条路：**同一套 base_url + 多 key 轮换 + extra_headers 内核**。

难点：OpenAI 没有"视频生成"的官方标准
--------------------------------------
图像有事实标准：`POST {base}/images/generations`
    body {model, prompt, n, size, response_format} → {data:[{url|b64_json}]}，**同步返回**。
多数中转站（one-api / new-api 一脉）照抄它，所以图适配器可以直接按约定写。

视频领域则**没有统一规范** —— 路径、请求字段名、响应结构、状态词表各家不同。
因此视频适配器把这几处**全部做成可配**：

    submit_path / poll_path     提交与轮询路径（{id} 占位）
    field_map                   请求字段名（prompt / ref_image / duration / resolution / seed / negative_prompt）
    extract                     响应提取（task_id / status / result_url / progress / message）
    status_map                  状态词表（queued / running / succeeded / failed）
    body_extra                  任意自定义请求字段

默认值按最通行的约定给；不匹配时改配置即可，**代码零改动** ——
与 ComfyUI 适配器"注入点可配"是同一个思路：把差异留在配置里。

计费安全
--------
提交（POST）= 计费、非幂等 → `http_safety(billable=True)`，超时抛 `BillingRiskError` 不自动重试；
轮询 / 下载 = 幂等 → `billable=False`，可安全重试。
多 key 轮换**只在 429 / 401 / 403**（明确"未受理 / 鉴权失败"）时换下一把 key；
`BillingRiskError`（结果未知）**绝不换 key 重试** —— 换把 key 重发同样可能重复下单。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import ImageProviderConfig, VideoProviderConfig
from . import http_safety
from .base import JobHandle, JobStatus, MediaProvider
from .http_safety import BillingRiskError


# ------------------------------------------------------------------ 取值工具

def dig(obj: Any, path: Optional[str], default: Any = None) -> Any:
    """按 dotted path 取值，支持 dict 键与 list 数字下标（"data.0.url"）。"""
    if not path:
        return default
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
                continue
            return default
        if isinstance(cur, (list, tuple)):
            if part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
                cur = cur[int(part)]
                continue
            return default
        return default
    return cur


def pick_first(obj: Any, paths: List[str], default: Any = None) -> Any:
    """按顺序试若干 dotted path，返回第一个"非空"的值。

    "非空" = 不是 None / "" / [] —— 中转站常回 `{"url": null, "video_url": "..."}`。
    """
    for p in paths:
        v = dig(obj, p)
        if v is not None and v != "" and v != []:
            return v
    return default


def _ref_to_data_uri(ref: str, sender: Callable[[str], bytes]) -> str:
    """把参考图（URL 或本地路径）转成 data URI（`ref_image_mode: base64` 时用）。"""
    r = str(ref)
    if r.startswith("data:"):
        return r
    if r.startswith(("http://", "https://")):
        blob = sender(r)
        mime = mimetypes.guess_type(r.split("?")[0])[0] or "image/png"
    else:
        p = Path(r)
        if not p.exists():
            raise FileNotFoundError(f"参考图不存在: {p}")
        blob = p.read_bytes()
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


# 内置"常见键名"探测表 —— extract 未配置时按这些顺序试。
# 覆盖 one-api / new-api / 各家中转站及若干原生厂商的常见形状。
_TASK_ID_PATHS = [
    "id", "task_id", "request_id", "data.id", "data.task_id", "data.request_id",
    "output.task_id", "output.id", "result.task_id",
]
_STATUS_PATHS = [
    "status", "state", "task_status", "data.status", "data.state", "data.task_status",
    "output.status", "output.state",
]
_URL_PATHS = [
    "data.0.url", "data.url", "url", "video_url", "data.0.video_url", "data.video_url",
    "output.url", "output.video_url", "data.output.url", "result.url",
    "results.0.url", "videos.0.url", "image_url", "data.0.image_url",
]
_B64_PATHS = ["data.0.b64_json", "b64_json", "data.b64_json", "data.0.b64"]

# 内部状态 → 常见外部状态字符串（可被 config.status_map 覆盖）
DEFAULT_STATUS_MAP: Dict[str, List[str]] = {
    "queued": ["queued", "pending", "submitted", "not_started", "waiting", "created", "in_queue"],
    "running": ["running", "processing", "in_progress", "generating", "started", "executing"],
    "succeeded": ["succeeded", "success", "completed", "complete", "done", "finished", "ok"],
    "failed": ["failed", "failure", "error", "cancelled", "canceled", "rejected", "timeout"],
}

# 这些 type 值会分发到本模块（注册表与连接测试共用同一份定义，避免两处漂移）
OPENAI_IMAGE_TYPES = {"openai_image", "openai_compatible", "openai", "images_generations"}
OPENAI_VIDEO_TYPES = {"openai_video", "openai_compatible", "openai", "videos_generations"}


def _explain_media_error(err: Exception, cfg: Any) -> RuntimeError:
    """把中转站/服务商的原始错误翻成**能行动**的中文提示（R-42）。

    为什么必须有这一步：便携版最容易踩的坑是「配置从开发者机器原样分发、
    密钥却不跟着走」——对方机器上 `providers.yaml` 仍指着开发者的中转站，
    但凭据库里没有对应 key，于是服务商返回的是
    `HTTP 403 insufficient_user_quota（剩余额度 $0）`。
    这条原文有两个害处：
      ① 完全没提"你还没配密钥"，用户会去**充值**（钱花了仍然不通）；
      ② 把"分发包带着别人的服务商配置"这件事彻底藏起来。

    所以这里按错误码分流，明确告诉用户下一步该做什么。原始串一律保留在末尾，
    便于排障时对照。
    """
    raw = str(err)
    low = raw.lower()
    pid = getattr(cfg, "id", "?")
    base = getattr(cfg, "base_url", "") or ""
    # ⚠️ 配置对象里**没有** kind/slot 字段（只有适配器基类有 `slot`），
    # 所以这里只能给出中性文案；出图/出片的区分由调用方补。
    tail = f"\n原始错误：{raw[:400]}"

    # 额度不足（服务商侧的账号/密钥问题，不是代码问题）
    if ("insufficient_user_quota" in low or "用户额度不足" in raw
            or "insufficient_quota" in low or "exceeded your current quota" in low
            or "billing_hard_limit_reached" in low):
        return RuntimeError(
            f"供应商账号额度不足（{pid}，{base}），已拒绝本次请求。\n"
            f"请二选一：\n"
            f"  ① 到该服务商后台给账号充值（当前余额为 0）；\n"
            f"  ② 在「设置」里换成自己有余额的供应商（换完需重启软件）。\n"
            f"如果这台电脑是**第一次使用、从别人拿到这个包**，那多半是第②种情况 —— "
            f"包里带着原开发者的服务商配置，而你的密钥并没有跟着包走，请先在「设置」里配自己的。\n"
            f"配置位置：{base or '未配置'}"
            + tail)

    # 鉴权失败
    if ("401" in raw or "403" in raw) and (
            "invalid_api_key" in low or "unauthorized" in low
            or "invalid authorization" in low or "authentication" in low
            or "invalid_api" in low or "密钥" in raw or "鉴权" in raw):
        return RuntimeError(
            f"密钥无效或未配置（{pid}）。\n"
            f"请到「设置」里重新填写该供应商的 API key / token，保存后重启软件。\n"
            f"配置位置：{base or '未配置'}" + tail)

    # 限流
    if "429" in raw or "rate limit" in low or "too many requests" in low:
        return RuntimeError(
            f"被限流（{pid}）：请求太频繁，稍等几十秒再试，或换一把 key。\n"
            f"配置位置：{base or '未配置'}" + tail)

    return err if isinstance(err, RuntimeError) else RuntimeError(raw + tail)


class _OpenAIMediaBase(MediaProvider):
    """OpenAI 兼容媒体适配器公共部分（鉴权 / 请求 / 多 key 轮换 / 下载）。"""

    slot = "image"

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self._base = (cfg.base_url or "").rstrip("/")

    # ---------- 令牌 ----------
    def _tokens(self) -> List[Optional[str]]:
        toks = list(self.cfg.resolve_all_tokens())
        return toks or [None]  # 允许无鉴权的本地中转

    def _check_configured(self) -> None:
        """公共前置检查（仅对**公网**供应商生效）。

        与 LLM 侧的 `_require_key` 不同，这里**不能**简单地"没 key 就报错"：
        `comfyui` 这类**本地** ComfyUI 本来就不要鉴权（`http://127.0.0.1:8188`），
        一律拦会把合法的本地用法打死。所以只对**公网地址**要求密钥。

        缺失时报的是可行动提示，而不是让用户去啃 `401invalid_api_key`。
        """
        base = (getattr(self.cfg, "base_url", "") or "").lower()
        is_local = any(h in base for h in ("127.0.0.1", "localhost", "::1", "0.0.0.0"))
        if is_local or self._tokens() != [None]:
            return
        # 用**实例上的** `slot`（配置对象里没有 kind/slot 字段，只有适配器基类有）
        slot_name = "出图" if getattr(self, "slot", "") == "image" else "出片"
        raise RuntimeError(
            f"未配置 API 密钥，无法{slot_name}（供应商 `{getattr(self.cfg, 'id', '?')}`）。\n"
            f"请到「设置」页填写：\n"
            f"  · 供应商地址：{getattr(self.cfg, 'base_url', '') or '（未配置）'}\n"
            f"  · 对应的 API key（保存后存进系统凭据库，不会写进配置文件）\n"
            f"填完保存后**重启软件**再试 —— 配置是启动时读一次的。\n"
            f"（如果用自建本地 ComfyUI 则不需要密钥，但地址要能连上。）"
        )

    # ---------- HTTP ----------
    def _headers(self, token: Optional[str]) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if token:
            h["Authorization"] = f"Bearer {token}"
        for k, v in (self.cfg.extra_headers or {}).items():
            h[k] = v
        return h

    def _send_json(self, url: str, method: str, body: Any, token: Optional[str],
                   *, billable: bool, timeout: Optional[float] = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in self._headers(token).items():
            req.add_header(k, v)
        raw = http_safety.request_bytes(
            req, billable=billable, timeout=timeout or self.cfg.timeout,
            provider=self.cfg.id, endpoint=url,
        )
        return json.loads(raw.decode("utf-8"))

    def _send_bytes(self, url: str, token: Optional[str] = None,
                    *, timeout: Optional[float] = None) -> bytes:
        req = urllib.request.Request(url, method="GET")
        for k, v in self._headers(token).items():
            req.add_header(k, v)
        # 下载 / 取参考图字节：不计费且幂等 → 网络抖动可安全重试
        return http_safety.request_bytes(
            req, billable=False, timeout=timeout or self.cfg.timeout,
            provider=self.cfg.id, endpoint=url,
        )

    def _with_keys(self, fn: Callable[[Optional[str]], Any]) -> Tuple[Any, Optional[str]]:
        """按 key 列表依次尝试；仅在 **429/401/403** 时换下一把 key。

        返回 (结果, 实际使用的 key)。`BillingRiskError` 直接上抛，不换 key 重试。
        最后一把 key 仍失败时，把错误翻译成可行动的中文提示（见 `_explain_media_error`）。
        """
        toks = self._tokens()
        last: Optional[Exception] = None
        for i, tok in enumerate(toks):
            try:
                return fn(tok), tok
            except BillingRiskError:
                raise  # 结果未知：换 key 重发同样可能重复计费，绝不重试
            except RuntimeError as e:
                msg = str(e)
                auth_or_ratelimit = ("429" in msg) or ("401" in msg) or ("403" in msg)
                if auth_or_ratelimit and i < len(toks) - 1:
                    last = e
                    continue
                raise _explain_media_error(e, self.cfg) from e
        raise last if last else RuntimeError("没有可用的密钥")

    # ---------- 状态映射 ----------
    def _status_map(self) -> Dict[str, List[str]]:
        smap = {k: list(v) for k, v in DEFAULT_STATUS_MAP.items()}
        for k, vs in (getattr(self.cfg, "status_map", None) or {}).items():
            smap[str(k).lower()] = [str(x).lower() for x in vs]
        return smap

    def _map_status(self, data: Any) -> str:
        raw: Any = None
        ex = (getattr(self.cfg, "extract", None) or {}).get("status")
        if ex:
            raw = dig(data, ex)
        if raw in (None, ""):
            raw = pick_first(data, _STATUS_PATHS)
        s = str(raw or "").strip().lower()
        if not s:
            return "queued"
        smap = self._status_map()
        # 先判终态，避免中转站把 "completed" 同时列进 running 之类造成误判
        for state in ("succeeded", "failed", "running", "queued"):
            if s in smap.get(state, []):
                return state
        return "running"

    def _extract_paths(self, key: str, defaults: List[str]) -> List[str]:
        ex = (getattr(self.cfg, "extract", None) or {}).get(key)
        return ([ex] if ex else []) + defaults

    # ---------- 下载（图/视频共用） ----------
    def _download_bytes(self, handle: JobHandle, dest: str | Path,
                        default_suffix: str) -> Path:
        b64 = handle.raw.get("b64")
        if b64:
            blob = base64.b64decode(b64)
            p = Path(dest)
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.suffix:
                p = p.with_suffix(".png")
            with open(p, "wb") as f:
                f.write(blob)
            return p

        url = handle.raw.get("sync_url") or handle.raw.get("result_url")
        if not url:
            st = self.poll(handle)
            if st.state != "succeeded" or not st.result_url:
                raise RuntimeError(f"任务尚未成功，无法下载: {st.state} {st.message}")
            url = st.result_url

        p = Path(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        suffix = Path(str(url).split("?")[0]).suffix
        if not suffix or len(suffix) > 5:
            suffix = default_suffix
        if p.suffix != suffix:
            p = p.with_suffix(suffix)
        blob = self._send_bytes(str(url), handle.raw.get("token"))
        with open(p, "wb") as f:
            f.write(blob)
        return p


# ------------------------------------------------------------------ 图像

class OpenAIImage(_OpenAIMediaBase):
    """OpenAI 兼容文生图 / 图生图（`/images/generations`）。

    主体是**同步**接口：一次 POST 就拿到结果。少数异步中转站会回 task id，
    此时按 `poll_path`（默认同路径 `/{id}`）轮询，逻辑与视频一致。
    """

    slot = "image"

    def __init__(self, cfg: ImageProviderConfig) -> None:
        super().__init__(cfg)

    def _size(self, kw: Dict[str, Any]) -> Optional[str]:
        if kw.get("size"):
            return str(kw["size"])
        w, h = kw.get("width"), kw.get("height")
        if w and h:
            return f"{int(w)}x{int(h)}"
        # `resolution` 兼容：视频侧它是平台档位名（"480p_vertical"），图像侧调用方
        # （如主播出图）传的是 "768x1344" 这种像素对。档位名在这里会因解析不出
        # 两个整数而自然落空，不会把 "480p_vertical" 当成尺寸发出去。
        res = str(kw.get("resolution") or "").strip().lower()
        if "x" in res:
            a, b = (p.strip() for p in res.split("x", 1))
            if a.isdigit() and b.isdigit():
                return f"{int(a)}x{int(b)}"
        # 兜底用 `default_size`。也认 `default_resolution` —— 否则配了这一项的人
        # 会以为改了尺寸，实际被静默忽略（两个字段同义，只能有一个真相）。
        if self.cfg.default_size:
            return self.cfg.default_size
        dr = str(self.cfg.default_resolution or "").strip().lower()
        if "x" in dr:
            a, b = (p.strip() for p in dr.split("x", 1))
            if a.isdigit() and b.isdigit():
                return f"{int(a)}x{int(b)}"
        return None

    def build_body(self, prompt: str, ref_images: List[str], **kw: Any) -> Dict[str, Any]:
        """构造请求体。除"取参考图字节"外为纯函数，便于离线单测。"""
        cfg = self.cfg
        body: Dict[str, Any] = {"prompt": prompt}
        if cfg.model:
            body["model"] = cfg.model
        if int(cfg.n or 1) > 1:
            body["n"] = int(cfg.n)
        size = self._size(kw)
        if size:
            body["size"] = size
        if cfg.response_format:
            body["response_format"] = cfg.response_format
        if kw.get("negative"):
            body["negative_prompt"] = kw["negative"]
        if kw.get("seed") is not None:
            body["seed"] = int(kw["seed"])
        if ref_images:
            first = ref_images[0]
            body[cfg.ref_image_field] = (
                _ref_to_data_uri(first, self._send_bytes)
                if (cfg.ref_image_mode or "url") == "base64" else first
            )
        # 任意中转站的自定义字段：配置里给的显式覆盖前面的默认
        body.update(cfg.body_extra or {})
        return body

    def submit(self, prompt: str, ref_images: Optional[List[str]] = None,
               **kw: Any) -> JobHandle:
        if not self._base:
            raise ValueError(f"图像供应商 {self.cfg.id} 未配置 base_url")
        self._check_configured()
        refs = list(ref_images or [])
        body = self.build_body(prompt, refs, **kw)
        url = f"{self._base}{self.cfg.image_path}"
        resp, tok = self._with_keys(
            lambda t: self._send_json(url, "POST", body, t, billable=True)
        )

        handle = JobHandle(provider_id=self.cfg.id, job_id="", raw={"token": tok, "body": body})
        task_id = pick_first(resp, self._extract_paths("task_id", _TASK_ID_PATHS))
        out_url = pick_first(resp, self._extract_paths("result_url", _URL_PATHS))
        b64 = pick_first(resp, self._extract_paths("b64", _B64_PATHS))

        if out_url:
            handle.raw["sync_url"] = str(out_url)
        elif b64:
            handle.raw["b64"] = str(b64)
        elif task_id:
            handle.job_id = str(task_id)
            handle.raw["pending"] = True
        else:
            raise RuntimeError(
                f"提交响应里没有任务 id 也没有结果地址（检查 extract 配置）: {str(resp)[:300]}"
            )
        return handle

    def poll(self, handle: JobHandle) -> JobStatus:
        if handle.raw.get("sync_url"):
            return JobStatus(state="succeeded", progress=1.0, message="done",
                             result_url=handle.raw["sync_url"])
        if handle.raw.get("b64"):
            return JobStatus(state="succeeded", progress=1.0, message="done")

        path = (self.cfg.poll_path or "/images/generations/{id}").replace("{id}", handle.job_id)
        data = self._send_json(f"{self._base}{path}", "GET", None,
                              handle.raw.get("token"), billable=False)
        state = self._map_status(data)
        msg = str(pick_first(data, self._extract_paths("message", ["message", "msg", "data.message"]), "") or "")
        if state == "succeeded":
            out_url = pick_first(data, self._extract_paths("result_url", _URL_PATHS))
            b64 = pick_first(data, self._extract_paths("b64", _B64_PATHS))
            if out_url:
                handle.raw["result_url"] = str(out_url)
            elif b64:
                handle.raw["b64"] = str(b64)
            else:
                return JobStatus(state="running", progress=0.9,
                                 message=msg or "已成功但未找到结果地址")
            return JobStatus(state="succeeded", progress=1.0, message=msg or "done",
                             result_url=handle.raw.get("result_url"))
        if state == "failed":
            return JobStatus(state="failed", progress=1.0, message=msg or "任务失败")
        progress = float(dig(data, "progress") or dig(data, "data.progress") or 0.0) or 0.0
        if progress > 1:
            progress = progress / 100.0
        return JobStatus(state=state, progress=min(max(progress, 0.1), 0.9),
                         message=msg or state)

    def download(self, handle: JobHandle, dest: str | Path) -> Path:
        return self._download_bytes(handle, dest, ".png")


# ------------------------------------------------------------------ 视频

class OpenAIVideo(_OpenAIMediaBase):
    """OpenAI 兼容图生视频（提交 → 轮询 → 下载）。

    所有与厂商的差异（路径 / 字段名 / 响应结构 / 状态词表）都在配置里，见模块 docstring。
    """

    slot = "video"

    def __init__(self, cfg: VideoProviderConfig) -> None:
        super().__init__(cfg)

    def _fname(self, key: str, default: str) -> str:
        return (self.cfg.field_map or {}).get(key, default)

    def build_body(self, prompt: str, ref_images: List[str], *,
                   duration: Optional[int] = None,
                   resolution: Optional[str] = None,
                   seed: Optional[int] = None,
                   negative_prompt: Optional[str] = None,
                   **kw: Any) -> Dict[str, Any]:
        """构造请求体。纯函数（除"取参考图字节"），便于离线单测。"""
        cfg = self.cfg
        body: Dict[str, Any] = {self._fname("prompt", "prompt"): prompt}
        if cfg.model:
            body[self._fname("model", "model")] = cfg.model

        d = duration if duration is not None else cfg.default_duration
        if d is not None:
            body[self._fname("duration", "duration")] = int(d)

        res = resolution or cfg.default_resolution
        if res:
            body[self._fname("resolution", "resolution")] = res

        if seed is not None:
            body[self._fname("seed", "seed")] = int(seed)

        if negative_prompt and cfg.supports_negative_prompt:
            body[self._fname("negative_prompt", "negative_prompt")] = negative_prompt

        if ref_images:
            first = ref_images[0]
            body[self._fname("ref_image", cfg.ref_image_field)] = (
                _ref_to_data_uri(first, self._send_bytes)
                if (cfg.ref_image_mode or "url") == "base64" else first
            )

        body.update(cfg.body_extra or {})
        return body

    def estimate_cost(self, duration: Optional[int],
                      resolution: Optional[str]) -> Tuple[float, str]:
        """沿用平台现价口径：8–24 点为高峰时段；价目表缺档时按 cost_per_sec 兜底。"""
        cfg = self.cfg
        d = int(duration) if duration is not None else int(cfg.default_duration or 1)
        res = resolution or cfg.default_resolution
        prices = (cfg.price_map or {}).get(str(res))
        if not prices:
            rate = float(cfg.cost_per_sec)
            amt = round(rate * d, 4)
            return amt, f"{d}s × ￥{rate}/s = ￥{amt:.2f}（{res} 不在价目表，按 cost_per_sec 兜底）"
        peak = 8 <= time.localtime().tm_hour < 24
        rate = float(prices[0] if peak else prices[1])
        amt = round(rate * d, 4)
        return amt, (
            f"{d}s × ￥{rate}/s = ￥{amt:.2f}"
            f"（{'高峰' if peak else '空闲'}时段，以平台结算为准）"
        )

    def submit(self, prompt: str, ref_images: Optional[List[str]] = None,
               **kw: Any) -> JobHandle:
        if not self._base:
            raise ValueError(f"视频供应商 {self.cfg.id} 未配置 base_url")
        self._check_configured()
        refs = list(ref_images or [])
        body = self.build_body(
            prompt, refs,
            duration=kw.get("duration"), resolution=kw.get("resolution"),
            seed=kw.get("seed"), negative_prompt=kw.get("negative_prompt"),
        )
        url = f"{self._base}{self.cfg.submit_path}"
        resp, tok = self._with_keys(
            lambda t: self._send_json(url, "POST", body, t, billable=True)
        )

        task_id = pick_first(resp, self._extract_paths("task_id", _TASK_ID_PATHS))
        out_url = pick_first(resp, self._extract_paths("result_url", _URL_PATHS))
        handle = JobHandle(provider_id=self.cfg.id, job_id=str(task_id or ""),
                           raw={"token": tok, "body": body, "resp": resp})
        if out_url:
            handle.raw["sync_url"] = str(out_url)
        if not task_id and not out_url:
            raise RuntimeError(
                f"提交响应里没有任务 id 也没有结果地址（检查 extract 配置）: {str(resp)[:300]}"
            )
        return handle

    def poll(self, handle: JobHandle) -> JobStatus:
        if handle.raw.get("sync_url"):
            return JobStatus(state="succeeded", progress=1.0, message="done",
                             result_url=handle.raw["sync_url"])

        path = (self.cfg.poll_path or "/videos/generations/{id}").replace("{id}", handle.job_id)
        data = self._send_json(f"{self._base}{path}", "GET", None,
                              handle.raw.get("token"), billable=False)
        state = self._map_status(data)
        msg = str(pick_first(data, self._extract_paths("message",
                      ["message", "msg", "error", "data.message", "data.msg"]), "") or "")
        if state == "succeeded":
            out_url = pick_first(data, self._extract_paths("result_url", _URL_PATHS))
            if not out_url:
                return JobStatus(state="running", progress=0.9,
                                 message=msg or "已成功但未找到结果地址")
            handle.raw["result_url"] = str(out_url)
            return JobStatus(state="succeeded", progress=1.0, message=msg or "done",
                             result_url=str(out_url))
        if state == "failed":
            return JobStatus(state="failed", progress=1.0,
                             message=msg or f"任务失败 task_id={handle.job_id}")
        progress = float(dig(data, "progress") or dig(data, "data.progress") or 0.0) or 0.0
        if progress > 1:
            progress = progress / 100.0
        return JobStatus(state=state, progress=min(max(progress, 0.0), 0.9),
                         message=msg or state)

    def download(self, handle: JobHandle, dest: str | Path) -> Path:
        return self._download_bytes(handle, dest, ".mp4")
