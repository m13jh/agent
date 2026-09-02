"""把 SubagentManager 的受控操作暴露成模型可调用工具。"""

from __future__ import annotations

from typing import Any, Literal

from python_agent.core.agent import Agent
from python_agent.ids import SessionId
from python_agent.subagents.manager import SubagentManager
from python_agent.subagents.types import SubagentSpec
from python_agent.tools.definition import ToolCapabilities
from python_agent.tools.types import ToolContext


class SpawnAgentTool:
    """创建独立 child Session，并可选择等待首批工作完成。"""

    name = "spawn_agent"
    description = (
        "Start an in-process subagent with a restricted tool set. "
        "Returns a stable child session id; optionally wait for its result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Initial child task"},
            "description": {"type": "string", "description": "Short task label"},
            "provider": {"type": "string"},
            "model": {"type": "string"},
            "persona": {"type": "string"},
            "allowed_tools": {"type": "array", "items": {"type": "string"}},
            "max_steps": {"type": "integer", "minimum": 1},
            "wait": {
                "type": "boolean",
                "default": False,
                "description": "Wait for the child to become idle and include its answer",
            },
        },
        "required": ["prompt", "description"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 600.0
    capabilities = ToolCapabilities(
        read_only=True,
        destructive=False,
        open_world=False,
        concurrency_safe=False,
        requires_approval=False,
    )

    def __init__(self, manager: SubagentManager, parent: Agent) -> None:
        self.manager = manager
        self.parent = parent

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """创建 Agent 会改变 Manager 所有权图，必须作为 exclusive 工具。"""

        return False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """校验规格、启动 child，并返回 ID 或同步等待后的结果。"""

        del context
        spec = SubagentSpec(
            description=arguments["description"],
            provider=arguments.get("provider"),
            model=arguments.get("model"),
            persona=arguments.get("persona"),
            allowed_tools=arguments.get("allowed_tools"),
            max_steps=arguments.get("max_steps"),
        )
        delivery_mode: Literal["sync", "async"] = (
            "sync" if arguments.get("wait", False) else "async"
        )
        child_id = await self.manager.start(
            self.parent,
            arguments["prompt"],
            spec,
            delivery_mode=delivery_mode,
        )
        result: dict[str, Any] = {"child_id": str(child_id), "status": "running"}
        if arguments.get("wait", False):
            settled = await self.manager.wait(self.parent, child_id)
            result.update(
                {
                    "status": "idle",
                    "answer": settled.answer,
                    "finish_reason": settled.finish_reason,
                }
            )
        return result


class SubagentFollowupTool:
    """向直接 child 的 Durable Inbox 提交新 Turn。"""

    name = "subagent_followup"
    description = "Send a follow-up task to a direct child subagent."
    parameters = {
        "type": "object",
        "properties": {
            "child_id": {"type": "string"},
            "prompt": {"type": "string"},
            "wait": {"type": "boolean", "default": False},
        },
        "required": ["child_id", "prompt"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 600.0
    capabilities = ToolCapabilities(
        read_only=True,
        destructive=False,
        open_world=False,
        concurrency_safe=False,
        requires_approval=False,
    )

    def __init__(self, manager: SubagentManager, parent: Agent) -> None:
        self.manager = manager
        self.parent = parent

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """修改 child Inbox，按 exclusive 调度。"""

        return False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """提交 followup，并可等待 child 再次收敛。"""

        del context
        child_id = SessionId(arguments["child_id"])
        delivery_mode: Literal["sync", "async"] = (
            "sync" if arguments.get("wait", False) else "async"
        )
        message_id = await self.manager.followup(
            self.parent,
            child_id,
            arguments["prompt"],
            delivery_mode=delivery_mode,
        )
        result: dict[str, Any] = {
            "child_id": str(child_id),
            "message_id": str(message_id),
            "status": "running",
        }
        if arguments.get("wait", False):
            settled = await self.manager.wait(self.parent, child_id)
            result.update(
                {
                    "status": "idle",
                    "answer": settled.answer,
                    "finish_reason": settled.finish_reason,
                }
            )
        return result


class SubagentInterruptTool:
    """取消直接 child 的当前执行和待处理消息。"""

    name = "subagent_interrupt"
    description = "Interrupt a direct child subagent and clear its pending Inbox."
    parameters = {
        "type": "object",
        "properties": {"child_id": {"type": "string"}},
        "required": ["child_id"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 30.0
    capabilities = ToolCapabilities(
        read_only=True,
        destructive=False,
        open_world=False,
        concurrency_safe=False,
        requires_approval=False,
    )

    def __init__(self, manager: SubagentManager, parent: Agent) -> None:
        self.manager = manager
        self.parent = parent

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        return False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """执行直接父级鉴权后的 interrupt。"""

        del context
        child_id = SessionId(arguments["child_id"])
        await self.manager.interrupt(self.parent, child_id)
        return {"child_id": str(child_id), "status": "idle", "interrupted": True}


class ListSubagentsTool:
    """列出当前父 Agent 的直接孩子，不泄露其他分支。"""

    name = "list_subagents"
    description = "List direct child subagents and their latest status."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    timeout_seconds: float | None = 10.0
    capabilities = ToolCapabilities(
        read_only=True,
        destructive=False,
        open_world=False,
        concurrency_safe=True,
        requires_approval=False,
    )

    def __init__(self, manager: SubagentManager, parent: Agent) -> None:
        self.manager = manager
        self.parent = parent

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """读取 Manager 快照，不改变 Agent 状态。"""

        return True

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> list[dict[str, Any]]:
        """返回 JSON 化的直接 child 快照。"""

        del arguments, context
        infos = await self.manager.list_children(self.parent.id)
        return [info.model_dump(mode="json") for info in infos]


def management_tools(manager: SubagentManager, parent: Agent) -> list[Any]:
    """为一个具体父 Agent 创建全套绑定工具实例。"""

    return [
        SpawnAgentTool(manager, parent),
        SubagentFollowupTool(manager, parent),
        SubagentInterruptTool(manager, parent),
        ListSubagentsTool(manager, parent),
    ]


__all__ = [
    "ListSubagentsTool",
    "SpawnAgentTool",
    "SubagentFollowupTool",
    "SubagentInterruptTool",
    "management_tools",
]
