"""供应商注册表：按 providers.yaml 的 active 字段实例化，并装配降级链。

降级链用两遍装配实现：
  第一遍造节点，第二遍回填 fallback 引用；
  用 seen 集合检测成环（A→B→A）并直接报错，避免运行时无限递归。
"""

from __future__ import annotations

from typing import Dict, Optional

from ..config import LLMProviderConfig, ProvidersConfig
from .base import LLMProvider
from .llm_openai_compat import OpenAICompatibleLLM


def _instantiate(cfg: LLMProviderConfig, fallback: Optional[LLMProvider]) -> LLMProvider:
    """按 type 分派适配器。新增供应商 = 在这里加一个分支。"""
    t = (cfg.type or "openai_compatible").lower()

    if t in ("openai_compatible", "openai", "ollama", "relay"):
        return OpenAICompatibleLLM(cfg, fallback)

    if t == "anthropic":
        # P1 实现；此处显式报错，避免静默降级到错误适配器
        raise NotImplementedError(
            f"LLM type='anthropic' 尚未实现（P1）。若中转站提供 OpenAI 兼容端点，"
            f"可直接把 {cfg.id} 的 type 改为 openai_compatible。"
        )

    raise NotImplementedError(
        f"未知的 LLM type: {cfg.type}。请实现 Provider 适配器后在 registry 注册。"
    )


def build_llm_providers(cfg: ProvidersConfig) -> Dict[str, LLMProvider]:
    """构造全部已登记的 LLM 供应商节点（含降级链）。"""
    specs: Dict[str, LLMProviderConfig] = {c.id: c for c in cfg.llm.list}
    nodes: Dict[str, LLMProvider] = {}

    def make(pid: str, seen: frozenset = frozenset()) -> LLMProvider:
        if pid in nodes:
            return nodes[pid]
        if pid in seen:
            raise ValueError(f"fallback 链成环: {pid}")
        spec = specs.get(pid)
        if spec is None:
            raise KeyError(f"fallback 指向了未登记的供应商: {pid}")

        fallback = (
            make(spec.fallback_provider, seen | {pid})
            if spec.fallback_provider
            else None
        )
        node = _instantiate(spec, fallback)
        nodes[pid] = node
        return node

    for c in cfg.llm.list:
        make(c.id)

    return nodes


def get_llm(
    cfg: ProvidersConfig,
    nodes: Optional[Dict[str, LLMProvider]] = None,
    active: Optional[str] = None,
) -> LLMProvider:
    """取当前生效的 LLM 供应商（默认取 config 的 active）。"""
    nodes = nodes if nodes is not None else build_llm_providers(cfg)
    pid = active or cfg.llm.active
    if pid not in nodes:
        raise KeyError(f"llm.active 指向了未构造的供应商: {pid}")
    return nodes[pid]
