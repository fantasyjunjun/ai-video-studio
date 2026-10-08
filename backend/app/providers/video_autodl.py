"""AutoDL ComfyUI 图生视频适配器（submit → poll → download）。

忠实移植自技能包里已验证的 `scripts/autodl_video.py`，继承其全部经验：

  - **三级令牌解析**：环境变量 → ~/.autodl/token → ~/.autodl/config.json；令牌永不落库/进日志。
  - **negative_prompt 保护**：`minimax_h3_lightx2v_v5_15s` 等工作流未定义该入参，
    传入会被服务端直接拒绝 → 忽略并把 warning 回传（反向约束须改写进正向提示词）。
  - **分辨率别名**：配置里写易读的 `480p_vertical`，平台要的是 `480p竖`，映射在适配器内完成。
  - **高峰 / 空闲计价**：8–24 点为高峰时段，出片前给出费用预估，出片后如实记账。
  - **结果 URL 多键提取**：结果项可能是 str，也可能是 dict（键名因工作流而异）。
  - **duration 范围校验**：该工作流 1–15 秒，越界直接拒绝（避免白白花钱打到服务端才报错）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import VideoProviderConfig
from . import http_safety
from .base import JobHandle, JobStatus, MediaProvider, mask_secret

# 平台默认接口前缀（可被 config.api_base 覆盖，指向私有实例 / 中转转发）
DEFAULT_API_BASE = "https://autodl.art/api/v1/comfyui/comfyui_workflow"

# 易读别名 → 平台档位（反向也可）
RESOLUTION_ALIASES: Dict[str, str] = {
    "480p_vertical": "480p竖", "480p_horizontal": "480p横",
    "768p_vertical": "768p竖", "768p_horizontal": "768p横",
}
_REVERSE_ALIASES = {v: v for v in RESOLUTION_ALIASES.values()}

RESULT_KEYS = ("url", "video_url", "image_url", "file", "download_url", "result", "path")

# 配置里的占位值。看到这些就当作"用户还没填"，回落到默认地址，
# 而不是真去打 `https://your-autodl-endpoint` 报一个莫名其妙的 DNS 错误。
PLACEHOLDER_HINTS = ("your-", "example.", "changeme", "replace-me", "<", "todo")


def looks_like_placeholder(value: Optional[str]) -> bool:
    if not value:
        return True
    low = str(value).lower()
    return any(h in low for h in PLACEHOLDER_HINTS)


class AutoDLVideo(MediaProvider):
    def __init__(self, cfg: VideoProviderConfig) -> None:
        self.cfg = cfg
        self.last_warnings: List[str] = []
        # 优先级：显式 api_base > base_url > 官方默认；占位值等同于未配置
        candidate = cfg.api_base or cfg.base_url
        if looks_like_placeholder(candidate):
            if candidate:
                self.last_warnings.append(
                    f"base_url='{candidate}' 看起来是占位值，已回落到官方默认地址"
                )
            candidate = None
        self._api_base = str(candidate or DEFAULT_API_BASE).rstrip("/")

    # ---------- 令牌 ----------
    def resolve_token(self) -> str:
        """三级解析：env(token_env) → ~/.autodl/token → ~/.autodl/config.json。"""
        tok = self.cfg.resolve_token()
        if tok:
            return tok

        home_cfg = Path.home() / ".autodl" / "config.json"
        home_tok = Path.home() / ".autodl" / "token"
        if home_tok.exists():
            t = home_tok.read_text(encoding="utf-8").strip()
            if t:
                return t
        if home_cfg.exists():
            try:
                t = (json.loads(home_cfg.read_text(encoding="utf-8")) or {}).get("token")
                if t:
                    return str(t)
            except json.JSONDecodeError:
                pass

        env_name = self.cfg.token_env or "AUTODL_TOKEN"
        raise RuntimeError(
            f"未找到 AutoDL 令牌。请设置环境变量 {env_name}，或写入 ~/.autodl/token"
            f"（令牌绝不落库、不进日志、不进分发包）"
        )

    # ---------- 参数规范化 ----------
    @staticmethod
    def map_resolution(resolution: Optional[str]) -> Optional[str]:
        """配置里的 `480p_vertical` → 平台要的 `480p竖`；已是平台写法则原样返回。"""
        if not resolution:
            return resolution
        r = str(resolution)
        return RESOLUTION_ALIASES.get(r, r)

    def check_duration(self, duration: Optional[int]) -> int:
        if duration is None:
            return int(self.cfg.default_duration)
        d = int(duration)
        if not (self.cfg.min_duration <= d <= self.cfg.max_duration):
            raise ValueError(
                f"duration={d} 超出工作流允许范围 "
                f"[{self.cfg.min_duration}, {self.cfg.max_duration}] 秒"
            )
        return d

    def build_body(
        self,
        prompt: str,
        *,
        duration: Optional[int] = None,
        resolution: Optional[str] = None,
        seed: Optional[int] = None,
        ref_images: Optional[List[str]] = None,
        negative_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """构造请求体。纯函数（除 warning 记录），便于离线单测。"""
        body: Dict[str, Any] = {"prompt": prompt}
        body["duration"] = self.check_duration(duration)

        res = self.map_resolution(resolution or self.cfg.default_resolution)
        if res:
            body["resolution"] = res

        if seed is not None:
            body["seed"] = int(seed)

        wf = self.cfg.workflow or ""
        if negative_prompt:
            if wf in (self.cfg.no_negative_prompt_workflows or []):
                self.last_warnings.append(
                    f"工作流 {wf} 未定义 negative_prompt，已忽略；"
                    f"负面约束请改写进正向提示词（祈使句写法）"
                )
            else:
                body["negative_prompt"] = negative_prompt

        for i, url in enumerate(ref_images or []):
            body[f"ref_image_{i}"] = url

        return body

    # ---------- 计费 ----------
    def estimate_cost(self, duration: int, resolution: Optional[str]) -> Tuple[float, str]:
        """返回 (金额, 说明)。沿用平台现价；8–24 点为高峰时段。"""
        res = self.map_resolution(resolution or self.cfg.default_resolution)
        prices = (self.cfg.price_map or {}).get(str(res))
        if not prices:
            rate = float(self.cfg.cost_per_sec)
            amt = round(rate * duration, 4)
            return amt, f"{duration}s × ￥{rate}/s = ￥{amt:.2f}（{res} 不在价目表，按 cost_per_sec 兜底）"
        peak = 8 <= time.localtime().tm_hour < 24
        rate = float(prices[0] if peak else prices[1])
        amt = round(rate * duration, 4)
        return amt, (
            f"{duration}s × ￥{rate}/s = ￥{amt:.2f}"
            f"（{'高峰' if peak else '空闲'}时段，以平台结算为准）"
        )

    # ---------- HTTP（抽成方法，便于离线测试时替换） ----------
    def _request(self, url: str, token: str, method: str = "GET",
                 body: Optional[Dict[str, Any]] = None,
                 timeout: int = 60) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", token)
        req.add_header("Content-Type", "application/json")
        for k, v in (self.cfg.extra_headers or {}).items():
            req.add_header(k, v)
        # POST = 提交生成任务（提交即计费）→ billable，失败绝不盲目重试；
        # GET = 轮询/查询 → 幂等，可安全重试。规则集中在 http_safety，别散落各处。
        raw = http_safety.request_bytes(
            req, billable=(method.upper() != "GET"), timeout=timeout,
            provider=self.cfg.id, endpoint=url,
        )
        return json.loads(raw.decode("utf-8"))

    # ---------- MediaProvider 接口 ----------
    def preflight(self) -> Optional[str]:
        """本地就能判定的前提：有令牌 + 有工作流 ID。

        `workflow` 这边是**平台侧的工作流 ID**（如 `minimax_h3_lightx2v_v5_15s`），
        不是本地文件，所以只判空、不判存在性。

        令牌**必须走 `self.resolve_token()`**，不能只看 `cfg.resolve_token()` ——
        后者少一层：`~/.autodl/token` / `~/.autodl/config.json` 的兜底在 provider 这一层。
        只看 cfg 会把"令牌写在 home 文件里"的正常环境误判成没令牌。
        """
        if not self.cfg.workflow:
            return "未配置 workflow（AutoDL 平台侧的工作流 ID）"
        try:
            self.resolve_token()
        except RuntimeError as e:
            return str(e)
        return None

    def submit(self, prompt: str, ref_images: Optional[List[str]] = None, **kw: Any) -> JobHandle:
        token = self.resolve_token()
        wf = self.cfg.workflow
        if not wf:
            raise ValueError(f"视频供应商 {self.cfg.id} 未配置 workflow")

        duration = self.check_duration(kw.get("duration"))
        body = self.build_body(
            prompt,
            duration=duration,
            resolution=kw.get("resolution"),
            seed=kw.get("seed"),
            ref_images=ref_images,
            negative_prompt=kw.get("negative_prompt"),
        )

        resp = self._request(
            f"{self._api_base}/{wf}", token, method="POST", body=body,
            timeout=self.cfg.timeout,
        )
        if resp.get("code") != "Success":
            raise RuntimeError(f"AutoDL 提交失败: {str(resp)[:300]}")

        task_id = str((resp.get("data") or {})["task_id"])
        amount, desc = self.estimate_cost(duration, body.get("resolution"))
        return JobHandle(
            provider_id=self.cfg.id,
            job_id=task_id,
            raw={
                "token": token,
                "body": body,
                "est_cost": amount,
                "est_desc": desc,
                "warnings": list(self.last_warnings),
                "workflow": wf,
            },
        )

    def poll(self, handle: JobHandle) -> JobStatus:
        token = handle.raw.get("token") or self.resolve_token()
        resp = self._request(
            f"{self._api_base}/result/{handle.job_id}", token,
            timeout=self.cfg.timeout,
        )
        data = resp.get("data") or {}
        status = data.get("status")
        state = {
            "SUCCESS": "succeeded", "FAILED": "failed", "FAILED_FAILED": "failed",
        }.get(str(status).upper(), "running")
        if str(status).upper() in ("QUEUED", "PENDING", ""):
            state = "queued"

        if state == "succeeded":
            results = data.get("results") or []
            if results:
                handle.raw["results"] = results
            return JobStatus(
                state="succeeded",
                progress=1.0,
                message="done",
                result_url=self.extract_url(results[0]) if results else None,
            )
        if state == "failed":
            return JobStatus(state="failed", progress=1.0,
                             message=f"任务失败 task_id={handle.job_id}")
        return JobStatus(
            state=state,
            progress=0.0,
            message=f"{status}（已耗时 {data.get('duration', 0)}s）",
        )

    @staticmethod
    def extract_url(item: Any) -> Optional[str]:
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            for key in RESULT_KEYS:
                if item.get(key):
                    return str(item[key])
        return None

    def download(self, handle: JobHandle, dest: str | Path) -> Path:
        results = handle.raw.get("results") or []
        if not results:
            st = self.poll(handle)
            if st.state != "succeeded" or not st.result_url:
                raise RuntimeError(f"任务尚未成功，无法下载: {st.state} {st.message}")
            url = st.result_url
        else:
            url = self.extract_url(results[0])
            if not url:
                raise RuntimeError(f"无法解析结果 URL: {results[0]}")

        p = Path(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        suffix = Path(url.split("?")[0]).suffix
        if not suffix or len(suffix) > 5:
            suffix = ".mp4"
        p = p.with_suffix(suffix) if p.suffix != suffix else p

        req = urllib.request.Request(url)
        for k, v in (self.cfg.extra_headers or {}).items():
            req.add_header(k, v)
        # 下载结果不计费、且幂等 → 网络抖动可安全重试
        blob = http_safety.request_bytes(
            req, billable=False, timeout=self.cfg.timeout,
            provider=self.cfg.id, endpoint=url,
        )
        with open(p, "wb") as f:
            f.write(blob)

        if os.environ.get("_AVS_DEBUG_TOKEN"):  # 永不常规打印，仅排障开关下的最小展示
            print(f"[debug] token={mask_secret(handle.raw.get('token'))}")
        return p
