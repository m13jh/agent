"""确定性 Echo 工具，主要用于 smoke test 和使用示例。"""

from __future__ import annotations

from typing import Any

from python_agent.tools.types import ToolContext


class EchoTool:
    name = "echo"
    description = "Return the supplied value unchanged."
    parameters = {
        "type": "object",
        "properties": {"value": {"description": "Value to echo"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = None

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        return True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        return arguments["value"]
