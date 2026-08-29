"""定义经过 Pydantic 校验的 Session Header 与 Session Event 数据模型。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from python_agent.ids import SessionId


def utc_now() -> datetime:
    """生成带 UTC 时区信息的当前时间，保证跨机器回放时不会混淆本地时区。"""

    return datetime.now(timezone.utc)


class SessionHeader(BaseModel):
    """描述 Session 身份、创建来源和恢复所需的稳定元数据。"""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, gt=0, description="Session Header 格式版本")
    id: SessionId = Field(description="Session 和 Agent 共用的稳定 ID")
    created_at: datetime = Field(default_factory=utc_now, description="创建时间，使用 UTC")
    cwd: Path | None = Field(default=None, description="本次 Agent 可见的工作目录")
    parent_session_id: SessionId | None = Field(default=None, description="父 Agent Session ID")
    origin: Literal["user", "subagent"] = Field(
        default="user",
        description="Session 是用户 Agent 还是子 Agent 创建的",
    )
    delegation_depth: int = Field(default=0, ge=0, description="子 Agent 委派深度")
    agent_preset: str | None = Field(default=None, description="恢复时重新组装能力的 preset 名称")


class SessionEvent(BaseModel):
    """描述一条只追加事件；事件序号由 Session 保证连续，内容必须可 JSON 序列化。"""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0, description="Session 内从 0 开始连续递增的事件序号")
    time: datetime = Field(default_factory=utc_now, description="事件追加时间，使用 UTC")
    type: str = Field(min_length=1, description="事件类型，例如 user/message 或 tool/result")
    data: dict[str, Any] = Field(default_factory=dict, description="事件的 JSON 数据负载")
    source_event_seqs: list[int] | None = Field(
        default=None,
        description="可选的来源事件序号，用于关联流式片段或替换关系",
    )
    ignorable: bool = Field(default=False, description="是否允许投影器忽略该未知事件")

    @field_validator("data")
    @classmethod
    def validate_json_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        """在事件进入日志前拒绝 Path、对象实例等不可回放的值。"""

        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("session event data must be JSON serializable") from exc
        return value
