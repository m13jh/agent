"""受 DeletePolicyEngine 保护的递归目录删除工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from python_agent.errors import ToolError
from python_agent.tools.builtins._paths import workspace_root
from python_agent.tools.capabilities import FILESYSTEM_WORKSPACE_WRITE
from python_agent.tools.definition import ToolCapabilities
from python_agent.tools.delete_policy import DeleteDecision, DeletePolicyEngine
from python_agent.tools.types import ToolContext


class DeleteDirectoryTool:
    """递归删除目录；即使目录在 workspace 内也默认需要审批。"""

    name = "delete_directory"
    description = "Recursively delete a workspace directory after enumerating its risks."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Workspace-relative directory"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 30.0
    capabilities = ToolCapabilities(
        read_only=False,
        destructive=True,
        open_world=False,
        concurrency_safe=False,
        requires_approval=False,
        required_capabilities=frozenset({FILESYSTEM_WORKSPACE_WRITE}),
    )

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        del arguments
        return False

    @staticmethod
    def _engine(context: ToolContext) -> DeletePolicyEngine:
        candidate = context.delete_policy
        if isinstance(candidate, DeletePolicyEngine):
            return candidate
        return DeletePolicyEngine(
            workspace=workspace_root(context),
            manifest=context.task_manifest,
            approval_service=context.approval_service,
        )

    @staticmethod
    def _stored_decision(context: ToolContext, path: Path) -> DeleteDecision | None:
        values = context.metadata.get("__delete_decisions__", {})
        if not isinstance(values, dict):
            return None
        candidate = values.get(str(path.expanduser().resolve()))
        return candidate if isinstance(candidate, DeleteDecision) else None

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """枚举并删除目录；软删除会把整个目录移入 task trash。"""

        engine = self._engine(context)
        raw_path = str(arguments["path"])
        path = (workspace_root(context) / raw_path).absolute()
        decision = self._stored_decision(context, path)
        if decision is None:
            decision = await engine.authorize(
                raw_path,
                context=context,
                call_id=context.session_id,
                tool_name=self.name,
                arguments=arguments,
                recursive=True,
                batch=True,
            )
        if not decision.allowed:
            raise ToolError(decision.reason)
        if not decision.paths:
            raise ToolError("delete policy produced no target")
        path = decision.paths[0]
        if not path.is_dir() or path.is_symlink():
            raise ToolError(f"directory does not exist or is a symlink: {arguments['path']}")
        deleted = engine.execute(
            decision,
            task_id=str(context.session_id),
            context=context,
        )
        return {
            "path": str(path.resolve().relative_to(workspace_root(context))),
            "deleted": True,
            "risk": decision.risk,
            "soft_delete": decision.soft_delete,
            "target_count": decision.member_count,
            "location": str(deleted[0]) if deleted else str(path),
        }


__all__ = ["DeleteDirectoryTool"]
