"""串行执行工具，并把所有失败规范化为模型可见的 ToolResult。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from python_agent.errors import ToolError, ToolValidationError
from python_agent.ids import CallId
from python_agent.llm.types import ToolCall
from python_agent.tools.definition import ToolDefinition
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.types import ToolContext


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: CallId
    name: str
    content: Any = None
    is_error: bool = False
    concludes_turn: bool = False

    def event_data(self) -> dict[str, Any]:
        return {
            "call_id": str(self.call_id),
            "name": self.name,
            "content": self.content,
            "is_error": self.is_error,
            "concludes_turn": self.concludes_turn,
        }


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def validate_arguments(tool: ToolDefinition, arguments: dict[str, Any]) -> None:
    """校验阶段 1 工具使用的精简 JSON Schema 子集。

    这里故意只覆盖 object、required、properties、additionalProperties 和基础 type，
    足以保护当前内置工具的外部参数边界；复杂校验和 Pydantic 参数模型留给后续工具
    策略阶段扩展。
    """

    schema = tool.parameters
    if schema.get("type", "object") != "object":
        raise ToolValidationError(f"tool {tool.name} parameters must describe an object")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ToolValidationError(f"tool {tool.name} has an invalid parameter schema")
    missing = [name for name in required if name not in arguments]
    if missing:
        raise ToolValidationError(
            f"tool {tool.name} is missing required arguments: {', '.join(missing)}"
        )
    additional = schema.get("additionalProperties", True)
    if additional is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ToolValidationError(
                f"tool {tool.name} received unknown arguments: {', '.join(unknown)}"
            )
    for name, value in arguments.items():
        spec = properties.get(name)
        if (
            isinstance(spec, dict)
            and isinstance(spec.get("type"), str)
            and not _matches_type(value, spec["type"])
        ):
            raise ToolValidationError(f"argument {name!r} for {tool.name} must be {spec['type']}")


def _safe_content(value: Any, max_chars: int) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        value = str(value)
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"\n[tool output truncated at {max_chars} characters]"
    return value


class ToolRuntime:
    """一次只运行一个模型工具调用，并保持模型给出的调用顺序。

    工具主体抛出的普通异常不会直接击穿 Agent Loop，而是转成 ``is_error=True`` 的结果，
    让下一次模型请求能够看到错误并自行修正。只有协作式取消会继续向上抛出。
    """

    def __init__(self, registry: ToolRegistry, *, max_result_chars: int = 12000) -> None:
        self.registry = registry
        self.max_result_chars = max_result_chars

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        tool = self.registry.maybe_get(call.name)
        if tool is None:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"tool is not registered: {call.name}",
                is_error=True,
            )
        try:
            validate_arguments(tool, call.arguments)
            if context.cancel_event.is_set():
                raise asyncio.CancelledError
            result = tool.execute(call.arguments, context)
            if tool.timeout_seconds is not None:
                result = await asyncio.wait_for(result, timeout=tool.timeout_seconds)
            else:
                result = await result
            if isinstance(result, ToolResult):
                return result.model_copy(update={"call_id": call.id, "name": call.name})
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=_safe_content(result, self.max_result_chars),
            )
        except asyncio.CancelledError:
            raise
        except (ToolError, TimeoutError, asyncio.TimeoutError) as exc:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"{type(exc).__name__}: {exc}",
                is_error=True,
            )
        except Exception as exc:  # 工具失败要交给模型，允许模型根据错误结果自我修正。
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"{type(exc).__name__}: {exc}",
                is_error=True,
            )
