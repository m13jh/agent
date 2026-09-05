"""受 DeletePolicyEngine 保护的单文件删除工具。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from python_agent.errors import ToolError
from python_agent.tools.builtins._paths import workspace_root
from python_agent.tools.capabilities import FILESYSTEM_WORKSPACE_WRITE
from python_agent.tools.definition import ToolCapabilities
from python_agent.tools.delete_policy import DeleteDecision, DeletePolicyEngine
from python_agent.tools.types import ToolContext


class DeleteFileTool:
    """删除一个 workspace 文件；用户已有文件默认移动到 task 专属 trash。"""

    name = "delete_file"
    description = "Delete one file inside the workspace after the deletion policy check."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Workspace-relative file path"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 20.0
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

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, str]:
        """流式读取文件摘要，避免把大文件完整复制到内存或模型上下文。"""

        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """先确认策略决定，再执行 D0 永久删除或 D1 软删除。"""

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
            )
        if not decision.allowed:
            raise ToolError(decision.reason)
        if not decision.paths:
            raise ToolError("delete policy produced no target")
        path = decision.paths[0]
        if not path.is_file() and not path.is_symlink():
            raise ToolError(f"file does not exist: {arguments['path']}")
        original_size, original_sha256 = (
            self._fingerprint(path) if path.is_file() else (0, hashlib.sha256(b"").hexdigest())
        )
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
            "bytes": original_size,
            "sha256": original_sha256,
        }


__all__ = ["DeleteFileTool"]
