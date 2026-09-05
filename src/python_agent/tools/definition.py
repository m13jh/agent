"""定义工具协议，以及基于普通 Python 函数的便捷实现。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from python_agent.tools.capabilities import (
    FILESYSTEM_WORKSPACE_READ,
    FILESYSTEM_WORKSPACE_WRITE,
    NETWORK_INTERNET,
    PROCESS_EXECUTE,
)
from python_agent.tools.types import ToolContext

ToolBody = Callable[[dict[str, Any], ToolContext], Any | Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolCapabilities:
    """工具明确声明的安全属性和调度属性。

    默认值有意采用 fail-closed：没有声明能力的扩展会被视为可能破坏数据、可访问外部世界
    且需要审批的操作。内置工具和明确的只读应用工具必须主动选择更安全的值。
    """

    read_only: bool = False
    destructive: bool = True
    open_world: bool = True
    concurrency_safe: bool = False
    requires_approval: bool = True
    interrupt_behavior: Literal["cancel", "block"] = "cancel"
    required_capabilities: frozenset[str] = frozenset()
    requires_network: bool = False

    def __post_init__(self) -> None:
        """把旧布尔声明物化为命名 Capability，确保每个工具都有显式快照。"""

        declared = set(self.required_capabilities)
        if not declared:
            declared.add(
                FILESYSTEM_WORKSPACE_READ if self.read_only else FILESYSTEM_WORKSPACE_WRITE
            )
            if self.open_world:
                declared.add(PROCESS_EXECUTE)
        if self.requires_network:
            declared.add(NETWORK_INTERNET)
        object.__setattr__(self, "required_capabilities", frozenset(declared))

    def declared_capabilities(self) -> frozenset[str]:
        """返回工具真正声明的命名 Capability 集合。

        旧版工具只填写布尔安全属性，因此这里保留一个确定的兼容映射；新工具可以用
        ``required_capabilities`` 明确声明更细粒度的能力。未声明的工具仍沿用危险默认值。
        """

        return self.required_capabilities

    @property
    def capability_names(self) -> frozenset[str]:
        """``required_capabilities`` 的可读别名。"""

        return self.required_capabilities


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
    capabilities: ToolCapabilities

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """声明给定参数是否允许未来的并发调度器重叠执行。"""

        ...

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        """执行工具主体并返回可 JSON 化的业务结果。"""

        ...


@dataclass(slots=True)
class FunctionTool:
    """把一个同步或异步 Python 函数包装成符合 ToolDefinition 的工具。

    只读扩展应显式提供 ``capabilities``。默认能力有意保持危险，未分类函数不能通过只读
    Agent 执行。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    body: ToolBody
    timeout_seconds: float | None = None
    concurrency_safe: bool = False
    capabilities: ToolCapabilities = ToolCapabilities()

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """返回注册时声明的并发能力；阶段 1—3默认由 Runtime 串行调度。"""

        del arguments
        return self.concurrency_safe and self.capabilities.concurrency_safe

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
