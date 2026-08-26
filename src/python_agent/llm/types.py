"""定义与具体 Provider 无关的模型边界类型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from python_agent.ids import CallId


class Usage(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: CallId
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    max_tokens: int = Field(default=2048, gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class AssistantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"
    usage: Usage = Field(default_factory=Usage)


class ModelChunk(BaseModel):
    """一次流式模型响应中的增量片段。

    文本内容会在到达时立即交给 CLI；工具调用通常会被 Provider 拆成多个片段，
    因此适配器负责在流结束前把它们重新拼成完整的 ToolCall，再交给 Agent Loop。
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: Literal["stop", "tool_calls", "length", "error"] | None = None
    usage: Usage | None = None
    done: bool = False


def tool_call_data(call: ToolCall) -> dict[str, Any]:
    """把标准化 ToolCall 转换成可直接写入事件日志的字典。"""

    return {"id": str(call.id), "name": call.name, "arguments": call.arguments}
