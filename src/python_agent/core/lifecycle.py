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
