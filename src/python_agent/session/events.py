"""定义经过 Pydantic 校验的 Session Header 与 Session Event 数据模型。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from python_agent.ids import SessionId


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SessionHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, gt=0)
    id: SessionId
    created_at: datetime = Field(default_factory=utc_now)
    cwd: Path | None = None
    parent_session_id: SessionId | None = None
    origin: Literal["user", "subagent"] = "user"
    delegation_depth: int = Field(default=0, ge=0)
    agent_preset: str | None = None


class SessionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    time: datetime = Field(default_factory=utc_now)
    type: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    source_event_seqs: list[int] | None = None
    ignorable: bool = False

    @field_validator("data")
    @classmethod
    def validate_json_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("session event data must be JSON serializable") from exc
        return value
