"""阶段五的 Turn 级 Token、费用与墙钟预算。

预算对象只保存一次 Turn 的运行统计，不写入模型消息副本。AgentLoop 在每个 Step 边界
查询限制，并把最终快照写进 ``turn/end``；Session Event 仍然是恢复和审计的事实源。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from python_agent.config import AgentPreset
from python_agent.errors import AgentLimitError
from python_agent.llm.types import Usage

LimitReason = Literal["token_budget", "cost_budget", "wall_time"]


@dataclass(frozen=True, slots=True)
class BudgetViolation:
    """一次已经达到或无法继续安全执行的预算限制。"""

    reason: LimitReason
    message: str


class BudgetExceededError(AgentLimitError):
    """在异步模型/工具操作中耗尽预算时携带结构化原因。"""

    def __init__(self, violation: BudgetViolation) -> None:
        """保存结构化限制，并沿用人类可读消息作为异常文本。"""

        super().__init__(violation.message)
        self.violation = violation


@dataclass(frozen=True, slots=True)
class TurnBudgetSnapshot:
    """可写入事件和实时通知的不可变预算快照。"""

    elapsed_seconds: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    cost_known: bool

    def event_data(self) -> dict[str, int | float | bool]:
        """转换成标准 JSON 标量，避免把内部 dataclass 放入 Session Event。"""

        return {
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 12),
            "cost_known": self.cost_known,
        }


class TurnBudget:
    """跟踪一个 Turn 内跨多个模型 Step 的累计资源消耗。"""

    def __init__(self, config: AgentPreset) -> None:
        """从不可变 preset 复制限制，并以 monotonic clock 记录开始时刻。"""

        self.max_tokens = config.max_turn_tokens
        self.max_cost_usd = config.max_turn_cost_usd
        self.max_seconds = config.max_turn_seconds
        self.input_rate = config.input_cost_per_million_tokens
        self.output_rate = config.output_cost_per_million_tokens
        self.started_at = time.monotonic()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.cost_usd = 0.0
        self.cost_known = False

    @property
    def elapsed_seconds(self) -> float:
        """返回不受系统时钟回拨影响的实际墙钟耗时。"""

        return max(0.0, time.monotonic() - self.started_at)

    @property
    def remaining_wall_seconds(self) -> float | None:
        """返回 Turn 剩余秒数；未配置墙钟预算时返回 None。"""

        if self.max_seconds is None:
            return None
        return max(0.0, self.max_seconds - self.elapsed_seconds)

    @property
    def remaining_tokens(self) -> int | None:
        """返回累计 Token 预算余量；未配置时返回 None。"""

        if self.max_tokens is None:
            return None
        return max(0, self.max_tokens - self.total_tokens)

    def snapshot(self) -> TurnBudgetSnapshot:
        """生成当前累计统计的不可变快照。"""

        return TurnBudgetSnapshot(
            elapsed_seconds=self.elapsed_seconds,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            cost_usd=self.cost_usd,
            cost_known=self.cost_known,
        )

    @staticmethod
    def _provider_cost(usage: Usage) -> float | None:
        """从 Provider 扩展 usage 中读取常见的美元费用字段。"""

        extras: dict[str, Any] = usage.model_extra or {}
        for key in ("cost_usd", "total_cost_usd", "total_cost", "cost"):
            value = extras.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return float(value)
        return None

    def _estimated_cost(self, usage: Usage) -> float | None:
        """优先采用 Provider 费用；缺失时按 preset 的输入/输出单价估算。"""

        provider_cost = self._provider_cost(usage)
        if provider_cost is not None:
            return provider_cost
        if self.input_rate is None and self.output_rate is None:
            return None
        input_rate = self.input_rate or 0.0
        output_rate = self.output_rate or 0.0
        return (
            usage.prompt_tokens * input_rate + usage.completion_tokens * output_rate
        ) / 1_000_000

    def record_usage(self, usage: Usage) -> BudgetViolation | None:
        """累计一次模型请求的 usage，并返回请求结束后触发的第一个限制。

        Provider 的 ``total_tokens`` 偶尔缺失，此时回退为 prompt + completion。费用预算
        被启用但 Provider 和 preset 都没有费用信息时选择停止，而不是把未知费用当作 0。
        """

        request_total = usage.total_tokens or usage.prompt_tokens + usage.completion_tokens
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += request_total

        request_cost = self._estimated_cost(usage)
        if request_cost is not None:
            self.cost_usd += request_cost
            self.cost_known = True
        elif self.max_cost_usd is not None:
            return BudgetViolation(
                "cost_budget",
                "已配置费用预算，但 Provider 未返回费用且 preset 未配置 Token 单价",
            )
        return self.check_boundary()

    def check_boundary(self) -> BudgetViolation | None:
        """在 Step/工具边界检查是否还能继续执行。"""

        if self.max_seconds is not None and self.elapsed_seconds >= self.max_seconds:
            return BudgetViolation(
                "wall_time",
                f"Turn 墙钟预算已耗尽：{self.elapsed_seconds:.3f}s / {self.max_seconds:.3f}s",
            )
        if self.max_tokens is not None and self.total_tokens >= self.max_tokens:
            return BudgetViolation(
                "token_budget",
                f"Turn Token 预算已耗尽：{self.total_tokens} / {self.max_tokens}",
            )
        if self.max_cost_usd is not None and self.cost_known and self.cost_usd >= self.max_cost_usd:
            return BudgetViolation(
                "cost_budget",
                f"Turn 费用预算已耗尽：${self.cost_usd:.8f} / ${self.max_cost_usd:.8f}",
            )
        return None


__all__ = [
    "BudgetExceededError",
    "BudgetViolation",
    "LimitReason",
    "TurnBudget",
    "TurnBudgetSnapshot",
]
