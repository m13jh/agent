"""让模型先发现元数据、再按需把 Skill 指令加载进工具结果。"""

from __future__ import annotations

from typing import Any

from python_agent.skills.registry import SkillRegistry
from python_agent.tools.registry import ToolRegistry
from python_agent.tools.types import ToolContext


class ListSkillsTool:
    """列出可用 Skill 元数据，不消耗完整指令上下文。"""

    name = "list_skills"
    description = "List available on-demand skills without loading their full instructions."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    timeout_seconds: float | None = 10.0

    def __init__(self, skills: SkillRegistry) -> None:
        self.skills = skills

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        return True

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> list[dict[str, Any]]:
        """返回清单快照；工具结果会由 Session 正常记录。"""

        del arguments, context
        return [metadata.model_dump(mode="json") for metadata in self.skills.list()]


class LoadSkillTool:
    """加载一个 Skill 的完整指令，并验证其工具声明未越权。"""

    name = "load_skill"
    description = "Load one skill's full instructions into the current tool result."
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 10.0

    def __init__(self, skills: SkillRegistry, tools: ToolRegistry) -> None:
        self.skills = skills
        self.tools = tools

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        return True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """读取完整指令；动态内容先成为 tool/result，下一 Step 才进入模型请求。"""

        del context
        loaded = self.skills.load(arguments["name"], available_tools=set(self.tools.names()))
        return loaded.model_dump(mode="json")


__all__ = ["ListSkillsTool", "LoadSkillTool"]
