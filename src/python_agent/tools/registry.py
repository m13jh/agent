"""按照稳定工具名管理 ToolDefinition 的注册表。"""

from __future__ import annotations

from collections.abc import Iterable

from python_agent.errors import ToolError, ToolNotFoundError
from python_agent.tools.definition import ToolDefinition


class ToolRegistry:
    def __init__(self, tools: Iterable[ToolDefinition] = ()) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolDefinition) -> None:
        if not tool.name or not tool.name.strip():
            raise ToolError("tool name cannot be empty")
        if tool.name in self._tools:
            raise ToolError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"tool is not registered: {name}") from exc

    def maybe_get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def schemas(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for name in self.names():
            tool = self._tools[name]
            schema = getattr(tool, "schema", None)
            if callable(schema):
                value = schema()
            else:
                value = {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            result.append(value)
        return result
