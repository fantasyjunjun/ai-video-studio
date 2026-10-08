"""Provider 抽象接口。

业务代码只依赖这里的接口，永不依赖具体厂商。
新增供应商 = 实现接口 + 在 providers.yaml 登记，无需改业务代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def mask_secret(value: Optional[str]) -> str:
    """日志 / 异常中只暴露密钥后四位。

    这是**全局唯一**的脱敏实现（LLM / 图 / 视频三个插槽共用），
    避免各适配器各写一份导致"某处漏脱敏"。
    """
    if not value:
        return "<no-key>"
    return f"…{value[-4:]}" if len(value) > 4 else "****"


@dataclass
class LLMResult:
    text: str
    model: str
    usage: Dict[str, Any] = field(default_factory=dict)
    # 上游结束原因：stop（正常）/ length（**被 max_tokens 截断**）/ content_filter …
    # 反推逐镜写长提示词时极易撞 length —— JSON 会在数组中途被腰斩，
    # 只有拿到这个字段才能把"模型没写完"和"模型写坏了"区分开，并给出可执行的建议。
    finish_reason: str = ""
    # 流式专用：是否收到了 `[DONE]` 或带 finish_reason 的结束帧。
    # False = 连接被中途掐断（中继/网关侧超时或断流），此时文本一定是残缺的。
    stream_complete: bool = True


class LLMProvider(ABC):
    """文本大模型：提示词引擎 / 反推 / 文案生成都走它。"""

    @abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kw: Any,
    ) -> LLMResult:
        raise NotImplementedError

    def complete_vision(
        self,
        system: str,
        user: str,
        images: List[Any],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kw: Any,
    ) -> LLMResult:
        """多模态补全：`images` 是 `[(bytes, mime), …]`（如商品图自动解读）。

        **默认不支持** —— 纯文本供应商（本地 ollama 文本模型等）不覆写；
        支持视觉的适配器（OpenAI 兼容层）覆写它。调用方必须 try/except
        `NotImplementedError` 并自己兜底，绝不因看不了图阻断主流程。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持视觉输入")

    def complete_vision_stream(
        self,
        system: str,
        user: str,
        images: List[Any],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        **kw: Any,
    ) -> LLMResult:
        """流式多看图补全（SSE）。

        与 `complete_vision` 返回同样的 `LLMResult`（文本是**累积**后的完整内容），
        但底层走 `stream=True` 逐 token 读取。

        **为什么要有它（R-32b-D）**：前置 Cloudflare 对中转站有 100s 硬超时（HTTP 524），
        在"上游没返回任何字节"时掐断。普通阻塞调用要等整个响应体读完才返回，一旦
        生成 > 100s 必撞 524。流式调用让 token 持续流动，Cloudflare 视作"连接活跃"
        不再发 524——这是从机制上兜住 524 的最后一环（A 缩图 + B 减 token + C 快模型
        负责把首 token 压进 100s；D 负责"即使总时长超过 100s 也不被掐"）。

        默认实现直接 raise `NotImplementedError`：只有显式支持流式 + 视觉的适配器才覆写
        （OpenAI 兼容层）。不支持的视觉供应商若没覆写，调用方会收到 NotImplementedError
        并降级为纯数据判定（与 `complete_vision` 的契约一致）。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持流式视觉输入")


@dataclass
class JobHandle:
    """提交生成任务后拿到的句柄。"""

    provider_id: str
    job_id: str
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JobStatus:
    state: str  # queued | running | succeeded | failed
    progress: float = 0.0
    message: str = ""
    result_url: Optional[str] = None


class MediaProvider(ABC):
    """文生图 / 图生视频共用接口（submit → poll → download）。"""

    def preflight(self) -> Optional[str]:
        """本地可判的"这个供应商现在能不能跑"。

        返回 `None` = 静态检查通过；返回字符串 = 一句可执行的中文原因。

        **为什么要有它**：有些配置错误在本地就能确定地判出来（工作流文件不在、
        base_url 还是占位值），而它们的失败现场却发生在**队列深处** —— 用户看到的是
        「任务失败：FileNotFoundError」，既不说明是哪个供应商，也不说可以怎么办。
        有了它，提交前就能拒掉并给人话，而不是排一个注定失败的队。

        **默认不检查**：网络/额度这类问题只有真发请求才知道，在这里探测等于
        每次提交前多打一次外部接口。子类按自己的确定性前提覆写。
        """
        return None

    @abstractmethod
    def submit(
        self,
        prompt: str,
        ref_images: Optional[List[str]] = None,
        **kw: Any,
    ) -> JobHandle:
        raise NotImplementedError

    @abstractmethod
    def poll(self, handle: JobHandle) -> JobStatus:
        raise NotImplementedError

    @abstractmethod
    def download(self, handle: JobHandle, dest: str | Path) -> Path:
        raise NotImplementedError
