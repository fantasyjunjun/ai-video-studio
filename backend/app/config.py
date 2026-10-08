"""配置层：providers.yaml + 环境变量。

安全约定（硬性）：
  - 配置文件里**不落明文密钥**：内联密钥存 OS 凭据库 / 本地加密文件（P-1b），
    yaml 里只保留 api_key_env / key_rotation / token_env 这类**变量名**；
  - 密钥绝不落库、绝不进日志、绝不进分发包；API 只回 `api_key_set`/`token_set` 标记；
  - 异常信息里出现密钥时只保留后四位（见 providers/base.py::mask_secret）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator


def _env(name: str) -> Optional[str]:
    """读取环境变量，空字符串视为未设置。"""
    v = os.environ.get(name)
    return v or None


class BudgetConfig(BaseModel):
    """花费上限（P-5）。

    为什么放在这里而不是散在各调用点：出片是**非幂等的计费操作**，
    一旦"哪一步忘了查上限"，钱就出去了。所以判据集中成一条纯函数
    （`services/budget.py::check`），所有提交入口只调它。

    各级上限语义（0 = 不限）：
      - `per_task_cap_cny`  单次提交预估费用上限（最直接的"手滑保护"）
      - `project_cap_cny`   单项目累计上限
      - `global_cap_cny`    全局（按 period 统计）累计上限
      - `provider_cap_cny`  在**每个供应商配置里**单独给（见各 ProviderConfig）
    """

    enabled: bool = True
    # 统计窗口：total（有史以来）/ daily / monthly / rolling_24h（滚动 24 小时）
    period: str = "total"
    per_task_cap_cny: float = 0.0
    project_cap_cny: float = 0.0
    global_cap_cny: float = 0.0
    # 预估费用按倍数预留：平台报价常偏低（高峰/加时长/重试），1.2 表示留 20% 余量
    reserve_ratio: float = 1.0
    # 在途（submitting/submitted/running）与未知（unknown）台账也计入已花 ——
    # 宁可低估余额，不可高估；unknown 恰恰是"钱可能已经花了"的那一类。
    count_pending: bool = True
    on_exceed: str = "block"  # block（拒绝提交） | warn（放行但在回执里告警）

    model_config = {"extra": "allow"}

    @field_validator("period")
    @classmethod
    def _v_period(cls, v: str) -> str:
        if v not in ("total", "daily", "monthly", "rolling_24h"):
            raise ValueError(
                f"budget.period 只能是 total/daily/monthly/rolling_24h，收到 {v!r}"
            )
        return v

    @field_validator("on_exceed")
    @classmethod
    def _v_on_exceed(cls, v: str) -> str:
        if v not in ("block", "warn"):
            raise ValueError(f"budget.on_exceed 只能是 block/warn，收到 {v!r}")
        return v

    @field_validator("reserve_ratio")
    @classmethod
    def _v_ratio(cls, v: float) -> float:
        if float(v) <= 0:
            raise ValueError("budget.reserve_ratio 必须为正数")
        return float(v)

    def validate_choices(self) -> None:
        # pydantic 的 field_validator 已在构造时校验；这里保留一个显式入口，
        # 供"先构造后修改"的调用方（如 API 层 patch 合并）二次确认 ——
        # 拼错 period/on_exceed 会让熔断**静默失效**，比不设上限更危险。
        if self.period not in ("total", "daily", "monthly", "rolling_24h"):
            raise ValueError(
                f"budget.period 只能是 total/daily/monthly/rolling_24h，收到 {self.period!r}"
            )
        if self.on_exceed not in ("block", "warn"):
            raise ValueError(
                f"budget.on_exceed 只能是 block/warn，收到 {self.on_exceed!r}"
            )
        if float(self.reserve_ratio) <= 0:
            raise ValueError("budget.reserve_ratio 必须为正数")


class LLMProviderConfig(BaseModel):
    """文本大模型供应商配置。

    关键设计：type=openai_compatible 时，base_url 可指向
    OpenAI 官方 / **任意中转站聚合站** / 本地 ollama，
    三者共用同一个适配器，无需为中转站写专用代码 —— 这就是"任意中转站可配"的落点。
    """

    id: str
    type: str = "openai_compatible"
    base_url: str

    # 该供应商的累计花费上限（P-5，0 = 不限）
    spend_cap_cny: float = 0.0

    # 单 key
    api_key_env: Optional[str] = None
    # 内联密钥：桌面软件场景下直接填，落在本机应用数据目录的 providers.yaml，
    # 限定在软件本身；优先于 api_key_env 使用（仍可两者并存做多 key）。
    api_key: Optional[str] = None
    # 多 key 轮询 + 故障转移（env 变量名列表）
    key_rotation: List[str] = Field(default_factory=list)
    # 中转站常用：渠道标识 / 专属鉴权头
    extra_headers: Dict[str, str] = Field(default_factory=dict)

    model: str
    temperature: float = 0.7
    max_tokens: int = 4096

    timeout: int = 120
    retries: int = 3
    # 全部重试失败后降级到的另一个供应商 id（通常指向本地 ollama）
    fallback_provider: Optional[str] = None

    # 知识库目录（铁律/模板/lint/文案规范），提示词引擎用
    knowledge_base: Optional[str] = None

    model_config = {"extra": "allow"}  # 允许任意中转站的自定义字段透传

    def resolve_keys(self) -> List[str]:
        """运行时解析真实密钥（去重保序）。

        优先级：内联 api_key（迁移兼容） > OS 凭据库/本地加密文件（P-1b） > 环境变量。
        """
        keys: List[str] = []
        if self.api_key:
            keys.append(self.api_key)
        try:
            from .security import secret_store as _sec

            s = _sec.get_secret("llm", self.id)
            if s:
                keys.append(s)
        except Exception:
            pass
        if self.api_key_env:
            v = _env(self.api_key_env)
            if v:
                keys.append(v)
        for name in self.key_rotation:
            v = _env(name)
            if v:
                keys.append(v)
        seen, out = set(), []
        for k in keys:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out


class ImageProviderConfig(BaseModel):
    """文生图供应商配置。

    ComfyUI 的关键难点是"工作流 JSON 里哪个节点吃正向提示词"，
    所以把注入点做成可配的 `inject`（dotted path），换工作流只改配置。
    """

    model_config = {"extra": "allow"}  # 允许任意图生图服务的自定义字段透传

    id: str
    type: str = "comfyui"
    base_url: Optional[str] = None
    token_env: Optional[str] = None
    # 该供应商的累计花费上限（P-5，0 = 不限）
    spend_cap_cny: float = 0.0
    # 内联令牌：桌面软件场景下直接填，落在本机应用数据目录，限定在软件本身
    token: Optional[str] = None
    extra_headers: Dict[str, str] = Field(default_factory=dict)
    timeout: int = 300
    retries: int = 3
    default_resolution: Optional[str] = None

    # ComfyUI 专用
    workflow: Optional[str] = None          # 工作流 JSON（API format）路径
    # 注入点：{"positive": "6.inputs.text", "width": "5.inputs.width", "seed": "3.inputs.seed"}
    inject: Dict[str, str] = Field(default_factory=dict)
    view_path: str = "/view"
    history_path: str = "/history"

    # ---------- 通用 OpenAI 兼容图像接口（P-3）----------
    # type: openai_image / openai_compatible
    # 事实标准：POST {base}/images/generations → {data:[{url|b64_json}]}，**同步返回**。
    # 少数异步中转站会回 task id，此时按 poll_path 轮询（默认同路径 /{id}）。
    model: Optional[str] = None             # 如 dall-e-3 / flux / seedream（按中转站命名填）
    n: int = 1
    default_size: Optional[str] = None      # 如 1024x1024 / 768x1344
    response_format: Optional[str] = None   # url | b64_json；留空 = 不传，听服务端默认
    image_path: str = "/images/generations"
    edit_path: str = "/images/edits"
    poll_path: str = "/images/generations/{id}"
    # 参考图（图生图 / 换装）字段名与传输方式；`url` 直传（默认），`base64` 取字节转 data URI
    ref_image_field: str = "image"
    ref_image_mode: str = "url"
    body_extra: Dict[str, Any] = Field(default_factory=dict)  # 任意中转站自定义请求字段
    # 响应提取（dotted path，支持 "data.0.url"）；留空则用内置常见键名探测
    extract: Dict[str, str] = Field(default_factory=dict)
    key_rotation: List[str] = Field(default_factory=list)     # 多 key 轮换（env 变量名）

    def resolve_all_tokens(self) -> List[str]:
        """解析全部可用令牌（去重保序）。

        优先级：内联 token（迁移兼容） > OS 凭据库/本地加密文件（P-1b）
                > token_env > key_rotation 里的各环境变量。
        """
        toks: List[str] = []
        if self.token:
            toks.append(self.token)
        try:
            from .security import secret_store as _sec

            s = _sec.get_secret("image", self.id)
            if s:
                toks.append(s)
        except Exception:
            pass
        if self.token_env:
            v = _env(self.token_env)
            if v:
                toks.append(v)
        for name in self.key_rotation:
            v = _env(name)
            if v:
                toks.append(v)
        seen, out = set(), []
        for k in toks:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out

    def resolve_token(self) -> Optional[str]:
        toks = self.resolve_all_tokens()
        return toks[0] if toks else None


class VideoProviderConfig(BaseModel):
    """图生视频供应商配置。"""

    model_config = {"extra": "allow"}  # 允许任意图生视频服务的自定义字段透传

    id: str
    type: str = "comfyui_autodl"
    base_url: Optional[str] = None
    token_env: Optional[str] = None
    # 该供应商的累计花费上限（P-5，0 = 不限）
    spend_cap_cny: float = 0.0
    # 内联令牌：桌面软件场景下直接填，落在本机应用数据目录，限定在软件本身
    token: Optional[str] = None
    extra_headers: Dict[str, str] = Field(default_factory=dict)
    workflow: Optional[str] = None
    default_duration: int = 5
    default_resolution: str = "480p_vertical"
    cost_per_sec: float = 0.03
    timeout: int = 900
    max_concurrency: int = 3

    # AutoDL 专用
    api_base: Optional[str] = None          # 覆盖默认接口前缀
    min_duration: int = 1
    max_duration: int = 15
    poll_interval: int = 5
    # 价目表：{档位: [高峰单价, 空闲单价]}，忠实沿用现平台报价，可按 Hussar 换
    price_map: Dict[str, List[float]] = Field(
        default_factory=lambda: {
            "480p竖": [0.030, 0.020], "480p横": [0.030, 0.020],
            "768p竖": [0.040, 0.030], "768p横": [0.040, 0.030],
        }
    )
    # 这些工作流未定义 negative_prompt，传入会被服务端直接拒绝
    no_negative_prompt_workflows: List[str] = Field(
        default_factory=lambda: ["minimax_h3_lightx2v_v5_15s"]
    )

    # ---------- 通用 OpenAI 兼容视频接口（P-3）----------
    # type: openai_video / openai_compatible
    # 视频领域**没有** OpenAI 官方标准：路径、请求字段名、响应结构、状态词表各家不同。
    # 因此四处全部可配，默认值按最通行的约定给；不匹配时改配置，代码零改动。
    model: Optional[str] = None             # 如 kling-v1 / sora-2 / veo-3（按中转站命名填）
    submit_path: str = "/videos/generations"
    poll_path: str = "/videos/generations/{id}"   # {id} 会替换成提交返回的 task id
    # 请求字段名映射：内部语义 → 该中转站实际字段名（未映射的用内部名）
    field_map: Dict[str, str] = Field(default_factory=dict)
    # 响应提取（dotted path，支持 "data.0.url"）；留空则用内置常见键名探测
    extract: Dict[str, str] = Field(default_factory=dict)
    # 状态词表：内部状态 → 该中转站的状态字符串列表（覆盖内置默认）
    status_map: Dict[str, List[str]] = Field(default_factory=dict)
    body_extra: Dict[str, Any] = Field(default_factory=dict)
    ref_image_field: str = "image"
    ref_image_mode: str = "url"             # url | base64
    supports_negative_prompt: bool = True
    key_rotation: List[str] = Field(default_factory=list)

    def resolve_all_tokens(self) -> List[str]:
        """解析全部可用令牌（去重保序）：内联 > 凭据库 > token_env > key_rotation。"""
        toks: List[str] = []
        if self.token:
            toks.append(self.token)
        try:
            from .security import secret_store as _sec

            s = _sec.get_secret("video", self.id)
            if s:
                toks.append(s)
        except Exception:
            pass
        if self.token_env:
            v = _env(self.token_env)
            if v:
                toks.append(v)
        for name in self.key_rotation:
            v = _env(name)
            if v:
                toks.append(v)
        seen, out = set(), []
        for k in toks:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out

    def resolve_token(self) -> Optional[str]:
        toks = self.resolve_all_tokens()
        return toks[0] if toks else None


class LLMSlot(BaseModel):
    active: str
    list: List[LLMProviderConfig]


class ImageSlot(BaseModel):
    active: str
    list: List[ImageProviderConfig]


class VideoSlot(BaseModel):
    active: str
    list: List[VideoProviderConfig]


class ProvidersConfig(BaseModel):
    llm: LLMSlot
    image: ImageSlot
    video: VideoSlot
    # 花费上限（P-5）。缺省即"不限"，老配置文件不改也能跑。
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    def llm_by_id(self, pid: str) -> Optional[LLMProviderConfig]:
        for c in self.llm.list:
            if c.id == pid:
                return c
        return None

    def active_llm_config(self) -> LLMProviderConfig:
        c = self.llm_by_id(self.llm.active)
        if c is None:
            raise KeyError(f"llm.active 指向了未登记的供应商: {self.llm.active}")
        return c

    def image_by_id(self, pid: str) -> Optional[ImageProviderConfig]:
        return next((c for c in self.image.list if c.id == pid), None)

    def active_image_config(self) -> ImageProviderConfig:
        c = self.image_by_id(self.image.active)
        if c is None:
            raise KeyError(f"image.active 指向了未登记的供应商: {self.image.active}")
        return c

    def video_by_id(self, pid: str) -> Optional[VideoProviderConfig]:
        return next((c for c in self.video.list if c.id == pid), None)

    def active_video_config(self) -> VideoProviderConfig:
        c = self.video_by_id(self.video.active)
        if c is None:
            raise KeyError(f"video.active 指向了未登记的供应商: {self.video.active}")
        return c


def load_providers_config(path: str | Path) -> ProvidersConfig:
    """从 providers.yaml 加载配置（顶层键为 providers）。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if "providers" not in data:
        raise ValueError("providers.yaml 缺少顶层 `providers:` 键")
    cfg = ProvidersConfig(**data["providers"])
    # 预算字段拼错（period=day / on_exceed=reject）会让熔断**静默失效**，
    # 而"以为设了上限其实没生效"比不设更危险 —— 所以加载即报错。
    cfg.budget.validate_choices()
    return cfg
