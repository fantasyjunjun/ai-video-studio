"""依赖装配：配置 → 知识库 → LLM 供应商 → 提示词引擎。

全部懒加载并缓存；切换供应商只改 providers.yaml 的 active。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

from .config import ProvidersConfig, load_providers_config
from .db.session import SessionLocal
from .kb.loader import KnowledgeBase
from .pipeline.engine import PromptEngine
from .providers.base import LLMProvider, MediaProvider
from .providers.media_registry import build_image_providers, build_video_providers
from .providers.registry import build_llm_providers, get_llm
from .services.batch import BatchOrchestrator
from .services.image_host import (ImageHost, StaticDirImageHost, UPLOAD_BACKENDS,
                                  UploadImageHost)
from .services.post import PostProduction
from .services.produce import ProduceOrchestrator
from .services.queue import RenderQueue
from .services.reverse import ReverseEngine

BASE = Path(__file__).resolve().parents[1]  # backend/


@lru_cache(maxsize=1)
def get_providers_config() -> ProvidersConfig:
    return load_providers_config(BASE / "providers.yaml")


@lru_cache(maxsize=1)
def get_kb() -> KnowledgeBase:
    cfg = get_providers_config()
    rel = cfg.active_llm_config().knowledge_base or "kb"
    return KnowledgeBase.load(BASE / rel)


@lru_cache(maxsize=1)
def get_llm_nodes() -> Dict[str, LLMProvider]:
    return build_llm_providers(get_providers_config())


def get_active_llm() -> LLMProvider:
    return get_llm(get_providers_config(), get_llm_nodes())


@lru_cache(maxsize=1)
def get_prompt_engine() -> PromptEngine:
    return PromptEngine(get_active_llm(), get_kb())


# ---------------- 媒体层（P2） ----------------

@lru_cache(maxsize=1)
def get_media_nodes() -> Tuple[Dict[str, MediaProvider], Dict[str, MediaProvider]]:
    """(图供应商池, 视频供应商池)。切换只改 providers.yaml 的 active。"""
    cfg = get_providers_config()
    return build_image_providers(cfg), build_video_providers(cfg)


# ---------------- 后期（P3） ----------------

@lru_cache(maxsize=1)
def get_post() -> PostProduction:
    """后期流水线编排器。工作目录落在 backend/data/post/p{项目号}/。"""
    return PostProduction(SessionLocal, BASE / "data" / "post")


@lru_cache(maxsize=1)
def get_image_host() -> Optional[ImageHost]:
    """临时图床：把本地参考图变成平台能取的公网 URL。

    按环境变量择一，**静态目录优先**（显式配了就听你的）：

    1. **静态目录图床** —— `AVS_IMAGE_HOST_ROOT`（静态目录）+ `AVS_IMAGE_HOST_URL`（对外地址）
       两个都设置才启用。适合"某个目录已经被暴露成公网 URL"（本地 nginx / http.server /
       sites 发布）。注意 `AVS_IMAGE_HOST_URL` 必须指向一份**包含了即将发布的那些文件**的
       快照，否则拿到的 URL 是 404。
    2. **上传型图床** —— `AVS_IMAGE_HOST_UPLOAD`（默认 `uguu`）把参考图直接 POST 给临时文件
       托管服务，当场拿到公网 URL。**本应用默认走这条**，因为发布参考图的目录名含 job id
       （`p{pid}/j{jid}`，出片时才生成），静态快照不可能预先包含它。
       设 `AVS_IMAGE_HOST_UPLOAD=off` 可关闭（关闭后带参考图的出片会被明确拒绝）。

    两者都没启用时返回 None，出片请求会带着"怎么配"的提示被拒 —— 宁可拒绝也不静默
    退化成纯文生视频（那样产品图与模特图根本没进画面，用户却以为做了图生视频）。
    """
    import os

    root = os.environ.get("AVS_IMAGE_HOST_ROOT")
    url = os.environ.get("AVS_IMAGE_HOST_URL")
    if root and url:
        return StaticDirImageHost(root, url)

    kind = (os.environ.get("AVS_IMAGE_HOST_UPLOAD") or "uguu").strip().lower()
    if kind in {"off", "none", "no", "0", "false", "disabled"}:
        return None
    entry = UPLOAD_BACKENDS.get(kind) or UPLOAD_BACKENDS["uguu"]
    make_backend, extract = entry
    return UploadImageHost(make_backend(), extract)


@lru_cache(maxsize=1)
def get_reverse_engine() -> ReverseEngine:
    """参考片反推：本地量测 + LLM 三档判定。

    看图调用的视觉模型由设置页的 `reverse_vision_model` 控制（留空=当前 active LLM 模型），
    复用当前 active LLM 的 base_url 与同一把密钥，只覆盖模型名——不需要（也不应该）
    单独建一条 provider 条目（那条会查不到凭据库里的密钥）。
    """
    return ReverseEngine(get_active_llm(), get_kb(), BASE / "data" / "reverse")


# ---------------- 合规（P-4） ----------------

@lru_cache(maxsize=1)
def get_compliance_rules():
    """广告法词表：(rules, source_desc)。

    内置词表 + `backend/compliance.yaml` 的覆盖/追加（文件不存在则纯内置）。
    改完 yaml 调 `POST /api/compliance/reload` 即可生效，不用重启 ——
    与 providers.yaml 的 reload 语义保持一致。
    """
    from .pipeline.compliance import load_rules

    return load_rules(BASE / "compliance.yaml")


@lru_cache(maxsize=1)
def get_batch() -> BatchOrchestrator:
    """批量变体编排器（P5）。

    依赖已缓存的渲染队列与后期编排器：它自己不认识任何厂商，
    也不重复实现"出片 / 拼接 / 念白 / 混音"里的任何一步。
    """
    return BatchOrchestrator(
        SessionLocal,
        get_render_queue(),
        get_post(),
        BASE / "data" / "batch",
        get_providers_config(),
    )


@lru_cache(maxsize=1)
def get_produce() -> ProduceOrchestrator:
    """一键出片编排器：脚本 → 落镜 → 逐镜出片 → 15s 成片。

    与 `get_batch` 一样只做**串联**：它不认识任何厂商，也不重复实现
    "出片 / 拼接 / 念白 / 混音"里的任何一步 —— 那几步分别在
    `services/render_core.py`、`services/shot_import.py`、`services/compose.py`。
    工作目录落在 backend/data/produce/。
    """
    return ProduceOrchestrator(
        SessionLocal,
        get_render_queue(),
        get_post(),
        get_providers_config(),
        get_media_nodes,
        BASE / "data" / "produce",
    )


@lru_cache(maxsize=1)
def get_render_queue() -> RenderQueue:
    img, vid = get_media_nodes()
    cfg = get_providers_config()
    vcfg = cfg.active_video_config()
    return RenderQueue(
        SessionLocal,
        img,
        vid,
        BASE / "data" / "renders",
        max_concurrency=vcfg.max_concurrency,
        poll_interval=float(vcfg.poll_interval),
        poll_timeout=float(vcfg.timeout),
        image_host=get_image_host(),
        budget_cfg=cfg,
    )
