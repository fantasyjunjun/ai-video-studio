"""MCP 薄壳：把工作室的 REST API 暴露成外部 agent 可调用的工具。

**设计红线（P-7）：本包不实现任何业务流程。**

排期、重试、计费台账、预算熔断、队列调度、合规词表、质检判据 —— 全部复用
后端那一份实现。本包只做三件事：

1. 把「工具入参」翻译成 HTTP 请求（参数整形）；
2. 把 HTTP 响应翻译回 MCP 的 content（结果摘要 + 截断）；
3. 在协议层做版本协商与错误归一。

**为什么不做第二份实现**：一旦 MCP 层自己写一遍"生成分镜要先查产品香调事实"这类
规则，两条路径就会分叉 —— 网页上手能跑、agent 调却漏规则，且改一处忘另一处。
薄壳的代价是每次工具调用多一跳 HTTP，收益是业务流程永远只有一份真相。
"""

from .client import DEFAULT_BASE_URL, StudioClient, StudioError

__all__ = ["StudioClient", "StudioError", "DEFAULT_BASE_URL"]
