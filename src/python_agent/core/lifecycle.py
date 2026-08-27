"""Agent 生命周期相关的公共模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class CancelCause(BaseModel):
    """描述一次协作式取消的来源，便于日志和实时通知区分原因。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["user", "parent", "timeout", "shutdown", "disposed"]
    message: str | None = None


AgentStatus = Literal["idle", "running"]
