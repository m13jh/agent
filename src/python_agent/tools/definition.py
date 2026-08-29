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
    """所有工具必须实现的结构化契约。

    Registry 只依赖名称、说明、参数 Schema、超时和 execute，不关心工具内部是普通函数、
    文件系统操作还是子进程。这样权限、超时和结果处理可以放在 Runtime 中统一执行。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    timeout_seconds: float | None

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """声明给定参数是否允许未来的并发调度器重叠执行。"""

        ...

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        """执行工具主体并返回可 JSON 化的业务结果。"""

        ...


@dataclass(slots=True)
class FunctionTool:
    """把一个同步或异步 Python 函数包装成符合 ToolDefinition 的工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    body: ToolBody
    timeout_seconds: float | None = None
    concurrency_safe: bool = False

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """返回注册时声明的并发能力；阶段 1—3默认由 Runtime 串行调度。"""

        return self.concurrency_safe

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        """执行 body，并兼容 body 返回普通值或 Awaitable 的两种写法。"""

        result = self.body(arguments, context)
        if inspect.isawaitable(result):
            return await result
        return result

    def schema(self) -> dict[str, Any]:
        """生成 OpenAI function tool Schema，供模型请求使用。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
