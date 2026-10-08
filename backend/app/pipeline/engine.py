"""提示词引擎：知识库注入 → LLM 生成 → lint 门禁 → 不达标回灌重生成。

设计要点：
  - **领域知识来自 KnowledgeBase**，不来自模型自身 —— 换任何 LLM 都被同一套铁律约束；
  - **lint 门禁是硬闸门**：低于 min_score 的提示词不返回给调用方，
    而是把扣分点回灌给 LLM 重生成（最多 max_rounds 轮）；
  - LLM 通过 LLMProvider 接口注入，测试可用假实现，不依赖网络。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..kb.loader import KnowledgeBase
from ..providers.base import LLMProvider
from .lint import DEFAULT_MIN_SCORE, LintReport, lint_prompt

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


@dataclass
class ShotSpec:
    """待生成的镜头规格。"""

    code: str
    duration_sec: float
    brief: str  # 这一镜要干什么（中文，给 LLM 看）


@dataclass
class Brief:
    """一次分镜生成的全部输入。"""

    project_name: str
    product_notes: str  # 只应包含 A/B 级事实
    talent_anchor: str  # 模特锚点（跨镜逐字复用）
    shots: List[ShotSpec]
    language: str = "pt-BR"
    extra: str = ""  # 用户额外要求（如"喷空中+转圈入金色汽雾"）


@dataclass
class GeneratedShot:
    code: str
    duration_sec: float
    target_frames: int
    prompt_en: str
    prompt_zh: str
    lint: Optional[LintReport] = None
    rounds: int = 1

    @property
    def lint_score(self) -> int:
        return self.lint.total if self.lint else 0

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "duration_sec": self.duration_sec,
            "target_frames": self.target_frames,
            "prompt_en": self.prompt_en,
            "prompt_zh": self.prompt_zh,
            "lint_score": self.lint_score,
            "lint": self.lint.to_dict() if self.lint else None,
            "rounds": self.rounds,
        }


@dataclass
class Storyboard:
    shots: List[GeneratedShot] = field(default_factory=list)
    rounds: int = 0

    @property
    def total_frames(self) -> int:
        return sum(s.target_frames for s in self.shots)

    @property
    def min_score(self) -> int:
        return min((s.lint_score for s in self.shots), default=0)

    def to_dict(self) -> dict:
        return {
            "rounds": self.rounds,
            "total_frames": self.total_frames,
            "min_score": self.min_score,
            "shots": [s.to_dict() for s in self.shots],
        }


class PromptEngine:
    def __init__(
        self,
        llm: LLMProvider,
        kb: KnowledgeBase,
        *,
        min_score: int = DEFAULT_MIN_SCORE,
        max_rounds: int = 3,
        fps: int = 24,
        kb_tags: Optional[List[str]] = None,
    ) -> None:
        self.llm = llm
        self.kb = kb
        self.min_score = min_score
        self.max_rounds = max_rounds
        self.fps = fps
        self.kb_tags = kb_tags or ["always", "prompt", "template", "gate"]

    # ---------- system prompt ----------
    def build_system_prompt(self) -> str:
        kb_text = self.kb.render(tags=self.kb_tags)
        return (
            "你是一个电商香水/美妆短视频的提示词工程师。\n"
            "你必须严格遵守下面的【领域知识库】里的铁律与模板来写提示词——"
            "这些规则来自真实出片踩坑，优先级高于你自己的偏好。\n\n"
            "输出要求：\n"
            "1. 只输出 JSON，不要任何解释文字；\n"
            "2. JSON 结构：{\"shots\": [{\"code\": str, \"prompt_en\": str, \"prompt_zh\": str}, ...]}；\n"
            "3. prompt_en 必须严格使用 v2 模板的 [SHOT]/[HERO]/[MOTION]/[CAMERA]/[LIGHT]/"
            "[PHYSICS]/[TEXTURE]/[REF]/[NEG] 分层结构；\n"
            "4. prompt_zh 是 prompt_en 的中文直译，供人工核对。\n\n"
            "【领域知识库】\n"
            f"{kb_text}\n"
        )

    # ---------- user prompt ----------
    def _build_user_prompt(self, brief: Brief, targets: List[ShotSpec]) -> str:
        lines = [
            f"项目：{brief.project_name}",
            f"投放语言：{brief.language}",
            f"产品香调事实（只允许用这些，不得推断补充）：{brief.product_notes}",
            f"模特锚点（必须在每条提示词的 [REF] 中逐字复用，且 ≤25 词）：{brief.talent_anchor}",
        ]
        if brief.extra:
            lines.append(f"额外要求：{brief.extra}")
        lines.append("")
        lines.append("本次需要生成的镜头：")
        for s in targets:
            lines.append(f"- {s.code}（{s.duration_sec:g}s）：{s.brief}")
        lines.append("")
        lines.append("逐镜生成，注意每镜的节拍数必须符合镜长预算。")
        return "\n".join(lines)

    def _build_repair_prompt(self, brief: Brief, bad: List[GeneratedShot]) -> str:
        lines = [
            "上一版提示词未通过 lint 门禁。请**只重写下面这些镜头**，"
            "严格按每条的具体扣分点修改，其余镜头保持不动。",
            "",
        ]
        for s in bad:
            assert s.lint is not None
            lines.append(f"### {s.code}（{s.duration_sec:g}s，当前 {s.lint_score}/14）")
            lines.append("扣分点：")
            for i in s.lint.issues:
                lines.append(f"  - {i}")
            for n in s.lint.notes:
                lines.append(f"  · {n}")
            lines.append("原文：")
            lines.append("```text")
            lines.append(s.prompt_en)
            lines.append("```")
            lines.append("")
        lines.append(f"模特锚点（[REF] 必须逐字复用且 ≤25 词）：{brief.talent_anchor}")
        lines.append("只输出 JSON：{\"shots\": [{\"code\":…, \"prompt_en\":…, \"prompt_zh\":…}]}")
        return "\n".join(lines)

    # ---------- 主流程 ----------
    def generate_storyboard(self, brief: Brief) -> Storyboard:
        system = self.build_system_prompt()
        targets = list(brief.shots)

        parsed = self._call_and_parse(system, self._build_user_prompt(brief, targets))
        shots = self._merge(parsed, brief, rounds=1)

        rounds = 1
        while rounds < self.max_rounds:
            bad = [s for s in shots if s.lint_score < self.min_score]
            if not bad:
                break
            rounds += 1
            repair = self._build_repair_prompt(brief, bad)
            parsed = self._call_and_parse(system, repair)
            fixed = self._merge(parsed, brief, rounds=rounds)
            # 只替换本次重写的镜头，其余保留上一版
            by_code = {s.code: s for s in fixed}
            shots = [by_code.get(s.code, s) for s in shots]

        return Storyboard(shots=shots, rounds=rounds)

    # ---------- 内部 ----------
    def _call_and_parse(self, system: str, user: str) -> Dict[str, Any]:
        raw = self.llm.complete(system, user).text
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> Dict[str, Any]:
        text = raw.strip()
        m = _JSON_BLOCK.search(text)
        if m:
            text = m.group(1)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 兜底：截取第一个 { 到最后一个 }
            i, j = text.find("{"), text.rfind("}")
            if i >= 0 and j > i:
                try:
                    return json.loads(text[i:j + 1])
                except json.JSONDecodeError:
                    pass
            raise ValueError(f"LLM 输出不是合法 JSON：{raw[:300]}")
        if "shots" not in data:
            raise ValueError("LLM 输出缺少 shots 字段")
        return data

    def _merge(self, data: Dict[str, Any], brief: Brief, rounds: int) -> List[GeneratedShot]:
        specs = {s.code: s for s in brief.shots}
        out: List[GeneratedShot] = []
        for item in data.get("shots", []):
            code = str(item.get("code", "")).strip()
            spec = specs.get(code)
            if spec is None:
                continue  # 忽略未在 brief 中声明的镜头
            prompt_en = str(item.get("prompt_en", "")).strip()
            shot = GeneratedShot(
                code=code,
                duration_sec=spec.duration_sec,
                target_frames=int(round(spec.duration_sec * self.fps)),
                prompt_en=prompt_en,
                prompt_zh=str(item.get("prompt_zh", "")).strip(),
                rounds=rounds,
            )
            shot.lint = lint_prompt(prompt_en, spec.duration_sec, name=code)
            out.append(shot)
        return out
