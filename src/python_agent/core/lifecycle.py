"""Agent 生命周期相关的公共模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CancelCause(BaseModel):
    """描述一次协作式取消的来源，便于日志和实时通知区分原因。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["user", "parent", "timeout", "shutdown", "disposed"] = Field(
        description="取消来源，用于区分用户取消、超时、父级传播和释放",
    )
    message: str | None = Field(default=None, description="可选的人类可读取消说明")


AgentStatus = Literal["idle", "running"]

# Driver 是否存活和最近一个任务是否完成是两个独立维度。AgentStatus 只描述前者；
# TaskStatus 用于让调用方区分“Driver 已回到 idle”与“任务确实生成了最终答案”。
TaskStatus = Literal["completed", "paused", "cancelled", "error"]

_PAUSED_FINISH_REASONS = frozenset(
    {
        "max_steps",
        "max_tokens",
        "length",
        "token_budget",
        "cost_budget",
        "wall_time",
        "crash_recovered",
    }
)


def task_status_for_finish_reason(reason: str) -> TaskStatus:
    """把 Turn 终止原因映射成调用方可见的最近任务状态。"""

    if reason in _PAUSED_FINISH_REASONS:
        return "paused"
    if reason == "aborted":
        return "cancelled"
    if reason == "error":
        return "error"
    return "completed"
