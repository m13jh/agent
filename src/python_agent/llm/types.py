"""定义与具体 Provider 无关的模型边界类型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from python_agent.ids import CallId


class Usage(BaseModel):
    """一次模型请求的 Token 使用量。

    Provider 返回的 usage 字段可能缺失或包含额外字段，因此标准字段默认从 0 开始，
    同时允许适配器保留额外统计信息。
    """

    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class ToolCall(BaseModel):
    """模型要求 Agent 执行一个工具时的标准化调用对象。"""

    model_config = ConfigDict(extra="forbid")

    id: CallId
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(BaseModel):
    """发送给模型适配器的完整请求快照，包含消息、工具 Schema 和采样参数。"""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    max_tokens: int = Field(default=2048, gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class AssistantResponse(BaseModel):
    """一个模型步骤结束后的完整响应。

    流式响应会先转换为多个 ModelChunk，只有全部片段收齐后才组装为此对象，
    因此 AgentLoop 不会执行半截工具参数。
    """

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
