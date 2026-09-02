"""确定性 Echo 工具，主要用于 smoke test 和使用示例。"""

from __future__ import annotations

from typing import Any

from python_agent.tools.definition import ToolCapabilities
from python_agent.tools.types import ToolContext


class EchoTool:
    """返回输入值的只读工具，常用于验证工具注册和模型闭环。"""

    name = "echo"
    description = "Return the supplied value unchanged."
    parameters = {
        "type": "object",
        "properties": {"value": {"description": "Value to echo"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = None
    capabilities = ToolCapabilities(
        read_only=True,
        destructive=False,
        open_world=False,
        concurrency_safe=True,
        requires_approval=False,
    )

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """Echo 不读写外部状态，多个调用之间互不影响。"""

        return True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        """返回 value；context 保留在签名中以满足统一工具协议。"""

        return arguments["value"]
