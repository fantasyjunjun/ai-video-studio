"""ComfyUI 文生图 / 图生图适配器（submit → poll → download）。

ComfyUI 原生 HTTP API 三件套：
    POST /prompt          {"prompt": <API-format graph>}   → {"prompt_id": ...}
    GET  /history/{id}    {"<id>": {"status": {...}, "outputs": {"9": {"images": [...]}}}}
    GET  /view?filename=&subfolder=&type=output             → 二进制图片

**关键设计：注入点可配。**
不同工作流的"正向提示词挂在哪号节点"完全不同，因此不硬编码节点号，
而是把注入点写成配置里的 dotted path：

```yaml
image:
  - id: comfyui-sdxl
    type: comfyui
    workflow: workflows/t2i_sdxl.json
    inject:
      positive: "6.inputs.text"      # 正向提示词
      negative: "7.inputs.text"      # 负向提示词
      width:     "5.inputs.width"
      height:    "5.inputs.height"
      seed:      "3.inputs.seed"
      ref_image: "12.inputs.image"   # 图生图/参考图（可选）
```

换工作流 = 改 YAML，代码零改动 —— 与整套"供应商可插拔"哲学一致。
参考图可选走 `/upload/image` 上传后回填节点（img2img / 换装变体场景）。
"""

from __future__ import annotations

import json
import mimetypes
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from ..config import ImageProviderConfig
from . import http_safety
from .base import JobHandle, JobStatus, MediaProvider
from .video_autodl import looks_like_placeholder


def _set_by_path(obj: Dict[str, Any], dotted: str, value: Any) -> None:
    """按 dotted path 写入值，中间层缺失时按需建 dict。

    工作流节点数字可能导出为 str 或 int，两种都兼容查找。
    """
    parts = dotted.split(".")
    cur: Any = obj
    for part in parts[:-1]:
        nxt = None
        if isinstance(cur, dict):
            if part in cur:
                nxt = cur[part]
            elif part.isdigit():
                for k, v in cur.items():
                    if str(k) == part:
                        nxt = v
                        part = k
                        break
        if nxt is None:
            nxt = {}
            cur[part] = nxt
        cur = nxt

    last = parts[-1]
    if isinstance(cur, dict) and last.isdigit() and last not in cur:
        for k in cur.keys():
            if str(k) == last:
                last = k
                break
    cur[last] = value


class ComfyUIImage(MediaProvider):
    def __init__(self, cfg: ImageProviderConfig) -> None:
        self.cfg = cfg
        self._base = (cfg.base_url or "").rstrip("/")

    # ---------- 工作流 ----------
    def workflow_path(self) -> Optional[Path]:
        """工作流 JSON 的绝对路径（相对路径以 backend/ 为基准）；未配置返回 None。

        单独抽出来，是为了让 `preflight`（判断"能不能跑"）与 `load_workflow`
        （真去读）共用**同一份**路径推导。两处各写一遍，就会出现
        "检查说没问题、读的时候却找不到"这种自相矛盾。
        """
        wf = self.cfg.workflow
        if not wf:
            return None
        p = Path(wf)
        if not p.is_absolute():
            # 相对路径以 backend/ 为基准
            p = Path(__file__).resolve().parents[2] / wf
        return p

    def preflight(self) -> Optional[str]:
        """本地就能判定的前提：实例地址可用 + 工作流文件真实存在。

        这两条正是"配了 ComfyUI 却永远跑不起来"的全部常见原因，且都不需要联网。
        地址**是否真在跑**不在这里探测 —— 那要每次提交都打一次外部接口；
        留给真正的 `submit`。

        `inject` 里的注入点是否命中节点**不查**：工作流换成别的图仍可能跑通
        （`_set_by_path` 会按需建中间层），过早拦下会挡住合理配置。
        """
        if not self._base:
            return ("未配置 base_url。ComfyUI 没有公共默认地址，"
                    "必须填你自己的实例地址（如 http://127.0.0.1:8188）")
        if looks_like_placeholder(self.cfg.base_url):
            return (f"base_url='{self.cfg.base_url}' 是占位值，"
                    f"请改成真实可达的 ComfyUI 实例地址")
        p = self.workflow_path()
        if p is None:
            return "未配置 workflow（工作流 JSON 路径）"
        if not p.exists():
            return (f"工作流文件不存在：{p}。提示词注入点（inject）依赖这份"
                    f"「API 格式」的工作流导出文件 —— 请在 ComfyUI 里用"
                    f"「导出（API 格式）」导出后放到该路径")
        return None

    def load_workflow(self) -> Dict[str, Any]:
        p = self.workflow_path()
        if p is None:
            raise ValueError(f"图像供应商 {self.cfg.id} 未配置 workflow")
        if not p.exists():
            raise FileNotFoundError(f"工作流文件不存在: {p}")
        return json.loads(p.read_text(encoding="utf-8"))

    def build_graph(
        self,
        prompt: str,
        *,
        negative: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        seed: Optional[int] = None,
        ref_image_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把提示词与参数注入工作流副本。纯函数，可离线单测。"""
        graph = self.load_workflow()
        inj = self.cfg.inject or {}

        def path_of(key: str) -> Optional[str]:
            return inj.get(key)

        p_pos = path_of("positive")
        if not p_pos:
            raise ValueError(
                f"供应商 {self.cfg.id} 的 inject 缺少 `positive`（正向提示词注入点），"
                f"无法把提示词写进工作流"
            )
        _set_by_path(graph, p_pos, prompt)

        if negative is not None and path_of("negative"):
            _set_by_path(graph, path_of("negative"), negative)
        if width is not None and path_of("width"):
            _set_by_path(graph, path_of("width"), int(width))
        if height is not None and path_of("height"):
            _set_by_path(graph, path_of("height"), int(height))
        if seed is not None and path_of("seed"):
            _set_by_path(graph, path_of("seed"), int(seed))
        if ref_image_name and path_of("ref_image"):
            _set_by_path(graph, path_of("ref_image"), ref_image_name)

        return graph

    # ---------- HTTP（抽成方法，便于离线测试替换） ----------
    def _request(self, url: str, method: str = "GET",
                 body: Any = None, timeout: Optional[int] = None,
                 *, raw_bytes: bool = False) -> Any:
        from urllib.request import Request, urlopen

        timeout = timeout or self.cfg.timeout
        if raw_bytes:
            req = Request(url, method=method or "GET")
        else:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = Request(url, data=data, method=method or "GET")
            req.add_header("Content-Type", "application/json")
        tok = self.cfg.resolve_token()
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        for k, v in (self.cfg.extra_headers or {}).items():
            req.add_header(k, v)

        # POST /prompt = 提交生成（提交即计费）→ billable；
        # GET /history、/view 下载 → 幂等，可安全重试。
        raw = http_safety.request_bytes(
            req, billable=(method or "GET").upper() == "POST", timeout=timeout,
            provider=self.cfg.id, endpoint=url,
        )
        return raw if raw_bytes else json.loads(raw.decode("utf-8"))

    def upload_image(self, file_path: str | Path) -> str:
        """上传本地图为 ComfyUI 可直接引用的文件名（含子目录）。"""
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"参考图不存在: {p}")

        boundary = f"----avs{uuid.uuid4().hex}"
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        blob = p.read_bytes()

        body = b""
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="image"; filename="{p.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
        body += blob + b"\r\n"
        body += f"--{boundary}--\r\n".encode()

        from urllib.request import Request, urlopen

        req = Request(f"{self._base}/upload/image", data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        tok = self.cfg.resolve_token()
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        # 上传参考图不产生费用，网络抖动可安全重试 → billable=False
        raw = http_safety.request_bytes(
            req, billable=False, timeout=self.cfg.timeout, provider=self.cfg.id,
        )
        info = json.loads(raw.decode("utf-8"))

        return str(info.get("name") or info.get("filename") or p.name)

    # ---------- MediaProvider 接口 ----------
    def submit(self, prompt: str, ref_images: Optional[List[str]] = None, **kw: Any) -> JobHandle:
        if not self._base:
            raise ValueError(
                f"图像供应商 {self.cfg.id} 未配置 base_url"
                f"（ComfyUI 没有公共默认地址，必须填你自己的实例地址）"
            )
        if looks_like_placeholder(self.cfg.base_url):
            raise ValueError(
                f"图像供应商 {self.cfg.id} 的 base_url='{self.cfg.base_url}' 是占位值。"
                f"请改成真实可达的 ComfyUI 实例地址（如 http://127.0.0.1:8188）"
            )

        ref_name: Optional[str] = None
        if ref_images and (self.cfg.inject or {}).get("ref_image"):
            ref_name = self.upload_image(ref_images[0])

        graph = self.build_graph(
            prompt,
            negative=kw.get("negative"),
            width=kw.get("width"),
            height=kw.get("height"),
            seed=kw.get("seed"),
            ref_image_name=ref_name,
        )

        client_id = uuid.uuid4().hex
        resp = self._request(
            f"{self._base}/prompt", method="POST",
            body={"prompt": graph, "client_id": client_id},
        )
        pid = str(resp.get("prompt_id") or "")
        if not pid:
            raise RuntimeError(f"ComfyUI 提交失败（无 prompt_id）: {str(resp)[:300]}")

        return JobHandle(provider_id=self.cfg.id, job_id=pid,
                         raw={"graph": graph, "client_id": client_id})

    @staticmethod
    def extract_images(outputs: Dict[str, Any]) -> List[Dict[str, str]]:
        """从 /history 的 outputs 里找出全部图片项。"""
        found: List[Dict[str, str]] = []
        for node in (outputs or {}).values():
            for key in ("images", "image", "gifs"):
                v = node.get(key) if isinstance(node, dict) else None
                if isinstance(v, dict):
                    v = [v]
                if isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict) and it.get("filename"):
                            found.append({
                                "filename": str(it["filename"]),
                                "subfolder": str(it.get("subfolder") or ""),
                                "type": str(it.get("type") or "output"),
                            })
        return found

    def poll(self, handle: JobHandle) -> JobStatus:
        resp = self._request(
            f"{self._base}{self.cfg.history_path}/{handle.job_id}",
        )
        entry = (resp.get(handle.job_id) or {}) if isinstance(resp, dict) else None
        if entry is None:
            return JobStatus(state="running", progress=0.0, message="waiting for history")

        status_obj = entry.get("status") or {}
        status_str = str(status_obj.get("status_str") or "").lower()
        completed = status_obj.get("completed", False)

        if status_str in ("error", "failed") or status_obj.get("status", 0) == 4:
            msgs = [str(m[1]) for m in status_obj.get("messages", []) if len(m) > 1]
            return JobStatus(state="failed", progress=1.0,
                             message="; ".join(msgs[:2]) or "ComfyUI 任务失败")

        images = self.extract_images(entry.get("outputs") or {})
        if not images and not completed:
            return JobStatus(state="running", progress=0.5, message="executing")

        if images:
            handle.raw["images"] = images
            first = images[0]
            qs = urlencode({"filename": first["filename"],
                            "subfolder": first["subfolder"],
                            "type": first["type"]})
            return JobStatus(state="succeeded", progress=1.0, message="done",
                             result_url=f"{self._base}{self.cfg.view_path}?{qs}")

        return JobStatus(state="running", progress=0.9, message="no output yet")

    def download(self, handle: JobHandle, dest: str | Path) -> Path:
        images = handle.raw.get("images")
        if not images:
            st = self.poll(handle)
            if st.state != "succeeded" or not st.result_url:
                raise RuntimeError(f"任务尚未成功，无法下载: {st.state} {st.message}")
            url = st.result_url
        else:
            first = images[0]
            qs = urlencode({"filename": first["filename"],
                            "subfolder": first["subfolder"],
                            "type": first["type"]})
            url = f"{self._base}{self.cfg.view_path}?{qs}"

        p = Path(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        suffix = Path(images[0]["filename"]).suffix if images else ".png"
        if p.suffix != suffix:
            p = p.with_suffix(suffix)
        data = self._request(url, raw_bytes=True)
        with open(p, "wb") as f:
            f.write(data)
        return p
