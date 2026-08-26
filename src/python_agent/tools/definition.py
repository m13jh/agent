"""定义工具协议，以及基于普通 Python 函数的便捷实现。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from python_agent.tools.types import ToolContext

ToolBody = Callable[[dict[str, Any], ToolContext], Any | Awaitable[Any]]


@runtime_checkable
class ToolDefinition(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    timeout_seconds: float | None

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool: ...

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any: ...


@dataclass(slots=True)
class FunctionTool:
    name: str
    description: str
    parameters: dict[str, Any]
    body: ToolBody
    timeout_seconds: float | None = None
    concurrency_safe: bool = False

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        return self.concurrency_safe

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        result = self.body(arguments, context)
        if inspect.isawaitable(result):
            return await result
        return result

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
