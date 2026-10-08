"""媒体供应商注册表（图 / 视频）。

与 LLM 注册表不同，这里**不做降级**：生图/生视频失败就该如实失败，
静默换供应商会导致同一批镜头的画风不一致（这是比失败更糟的结果）。

已支持的 type：
  图像  comfyui / comfyui_api            自建 ComfyUI（工作流注入）
        openai_image / openai_compatible 任意 OpenAI 兼容中转站（/images/generations，P-3）
  视频  comfyui_autodl / autodl          AutoDL 托管工作流
        openai_video / openai_compatible 任意 OpenAI 兼容中转站（提交→轮询，P-3）

新增供应商 = 实现 MediaProvider 接口 + 在此登记 + 在 providers.yaml 写条目，业务代码无需改动。
"""

from __future__ import annotations

from typing import Dict, Optional

from ..config import ProvidersConfig
from .base import MediaProvider
from .image_comfyui import ComfyUIImage
from .openai_media import OPENAI_IMAGE_TYPES, OPENAI_VIDEO_TYPES, OpenAIImage, OpenAIVideo
from .video_autodl import AutoDLVideo

# type → 适配器类（大小写不敏感）；openai_* 的 type 集合与 openai_media 模块共用一份定义
IMAGE_TYPES = {
    "comfyui": ComfyUIImage,
    "comfyui_api": ComfyUIImage,
}
IMAGE_TYPES.update({t: OpenAIImage for t in OPENAI_IMAGE_TYPES})

VIDEO_TYPES = {
    "comfyui_autodl": AutoDLVideo,
    "autodl": AutoDLVideo,
}
VIDEO_TYPES.update({t: OpenAIVideo for t in OPENAI_VIDEO_TYPES})


def build_image_providers(cfg: ProvidersConfig) -> Dict[str, MediaProvider]:
    nodes: Dict[str, MediaProvider] = {}
    for c in cfg.image.list:
        t = (c.type or "comfyui").lower()
        cls = IMAGE_TYPES.get(t)
        if cls is None:
            raise NotImplementedError(
                f"未知的 image type: {c.type}。可用: {sorted(set(IMAGE_TYPES))}。"
                f"（新厂商按同一 MediaProvider 接口实现后在此登记即可，业务代码无需改动）"
            )
        nodes[c.id] = cls(c)
    return nodes


def build_video_providers(cfg: ProvidersConfig) -> Dict[str, MediaProvider]:
    nodes: Dict[str, MediaProvider] = {}
    for c in cfg.video.list:
        t = (c.type or "comfyui_autodl").lower()
        cls = VIDEO_TYPES.get(t)
        if cls is None:
            raise NotImplementedError(
                f"未知的 video type: {c.type}。可用: {sorted(set(VIDEO_TYPES))}。"
                f"（可灵 / Runway / 即梦 等按同一 MediaProvider 接口接入即可，业务代码无需改动）"
            )
        nodes[c.id] = cls(c)
    return nodes


def get_media(
    cfg: ProvidersConfig,
    slot: str,
    nodes: Optional[Dict[str, MediaProvider]] = None,
    active: Optional[str] = None,
) -> MediaProvider:
    """取当前生效的媒体供应商。slot ∈ {image, video}。"""
    if slot == "image":
        nodes = nodes if nodes is not None else build_image_providers(cfg)
        pid = active or cfg.image.active
    elif slot == "video":
        nodes = nodes if nodes is not None else build_video_providers(cfg)
        pid = active or cfg.video.active
    else:
        raise ValueError(f"未知插槽: {slot}")

    if pid not in nodes:
        raise KeyError(f"{slot}.active 指向了未构造的供应商: {pid}")
    return nodes[pid]
