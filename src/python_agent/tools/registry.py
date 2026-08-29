"""按照稳定工具名管理 ToolDefinition 的注册表。"""

from __future__ import annotations

from collections.abc import Iterable

from python_agent.errors import ToolError, ToolNotFoundError
from python_agent.tools.definition import ToolDefinition


class ToolRegistry:
    """维护工具名称到定义的映射，并提供稳定排序的模型 Schema。"""

    def __init__(self, tools: Iterable[ToolDefinition] = ()) -> None:
        """按传入顺序注册工具；Schema 输出时会重新按名称排序。"""

        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolDefinition) -> None:
        """校验工具名并登记定义；重复名不能覆盖已有工具。"""

        if not tool.name or not tool.name.strip():
            raise ToolError("tool name cannot be empty")
        if tool.name in self._tools:
            raise ToolError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        """获取必须存在的工具；缺失名称抛出 ToolNotFoundError。"""

        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"tool is not registered: {name}") from exc

    def maybe_get(self, name: str) -> ToolDefinition | None:
        """获取可选工具；不存在时返回 None，适合 Runtime 生成模型可见错误结果。"""

        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        """返回字典序稳定的工具名快照。"""

        return tuple(sorted(self._tools))

    def schemas(self) -> list[dict[str, object]]:
        """返回稳定排序的模型工具 Schema，不暴露 execute 等内部实现细节。"""

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
