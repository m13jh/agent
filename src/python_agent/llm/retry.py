"""模型请求错误的可注入重试决策接口与默认策略。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from python_agent.errors import ModelError
from python_agent.llm.types import ModelRequest


@dataclass(frozen=True, slots=True)
class ModelRetryContext:
    """一次失败提交给重试策略的只读上下文。"""

    request: ModelRequest
    error: Exception
    attempt: int
    max_retries: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """策略对当前错误的处理决定。"""

    retry: bool
    delay_seconds: float = 0.0
    reason: str = ""


@runtime_checkable
class ModelRetryPolicy(Protocol):
    """允许应用按错误、模型和尝试次数替换默认重试规则。"""

    async def decide(self, context: ModelRetryContext) -> RetryDecision:
        """返回是否重试以及重试前等待的秒数。"""

        ...


class DefaultModelRetryPolicy:
    """对传输类错误有限指数退避，对永久错误 fail fast。"""

    _permanent_markers = (
        "api key",
        "authentication",
        "unauthorized",
        "forbidden",
        "insufficient balance",
        "quota",
        "status 400",
        "status 401",
        "status 403",
    )
    _malformed_markers = (
        "invalid deepseek response",
        "invalid deepseek stream",
        "invalid deepseek stream tool",
        "malformed",
    )

    def __init__(self, base_delay_seconds: float = 0.5, *, max_delay_seconds: float = 30.0) -> None:
        """设置指数退避基数和单次等待上限。"""

        self.base_delay_seconds = max(0.0, base_delay_seconds)
        self.max_delay_seconds = max(0.0, max_delay_seconds)

    async def decide(self, context: ModelRetryContext) -> RetryDecision:
        """根据标准异常类型和安全错误标记生成默认决定。"""

        if context.attempt > context.max_retries:
            return RetryDecision(False, reason="已达到模型请求最大重试次数")
        if not isinstance(context.error, ModelError):
            return RetryDecision(False, reason="不是可重试的模型边界错误")

        message = str(context.error).lower()
        if any(marker in message for marker in self._permanent_markers):
            return RetryDecision(False, reason="认证、配额或请求参数错误不应重试")
        if any(marker in message for marker in self._malformed_markers) and context.attempt > 1:
            return RetryDecision(False, reason="畸形响应只允许重新请求一次")

        delay = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** max(0, context.attempt - 1)),
        )
        return RetryDecision(True, delay_seconds=delay, reason="模型传输或响应错误，有限重试")


__all__ = [
    "DefaultModelRetryPolicy",
    "ModelRetryContext",
    "ModelRetryPolicy",
    "RetryDecision",
]
