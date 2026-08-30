"""进程内子 Agent 的公开规格、状态与结果类型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from python_agent.core.lifecycle import AgentStatus
from python_agent.ids import SessionId
from python_agent.session.events import utc_now

SUBAGENT_TOOL_NAMES = frozenset(
    {"spawn_agent", "subagent_followup", "subagent_interrupt", "list_subagents"}
)


class SubagentSpec(BaseModel):
    """父 Agent 创建子 Agent 时可以收窄或定制的能力规格。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1, description="子任务简短说明，用于列表和审计")
    provider: str | None = Field(default=None, description="可选模型 Provider；默认继承父级")
    model: str | None = Field(default=None, description="可选模型名；默认继承父级")
    persona: str | None = Field(default=None, description="追加到系统提示词的子 Agent 角色说明")
    allowed_tools: frozenset[str] | None = Field(
        default=None,
        description="允许工具集合；必须是父级当前授权工具的子集",
    )
    max_steps: int | None = Field(
        default=None,
        gt=0,
        description="子 Agent 每 Turn 最大 Step；不能超过父级",
    )


class SubagentInfo(BaseModel):
    """供 API、工具结果和 UI 展示的直接子 Agent 快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: SessionId
    parent_id: SessionId
    description: str
    delegation_depth: int = Field(ge=1)
    status: AgentStatus
    created_at: datetime = Field(default_factory=utc_now)
    last_answer: str | None = None
    finish_reason: str | None = None


class SubagentSettled(BaseModel):
    """子 Agent 一批已排队 Turn 收敛后发送给父级的结果通知。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    child_id: SessionId
    parent_id: SessionId
    answer: str
    finish_reason: str


__all__ = ["SUBAGENT_TOOL_NAMES", "SubagentInfo", "SubagentSettled", "SubagentSpec"]
