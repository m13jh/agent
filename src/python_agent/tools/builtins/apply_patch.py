"""在 workspace 内应用受控文本补丁的 apply_patch 工具。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from python_agent.errors import ToolError
from python_agent.tools.builtins._file_transaction import FileTransaction
from python_agent.tools.builtins._paths import safe_path, workspace_root
from python_agent.tools.definition import ToolCapabilities
from python_agent.tools.delete_policy import DeleteDecision, DeletePolicyEngine
from python_agent.tools.task_manifest import TaskFileManifest
from python_agent.tools.types import ToolContext

PatchAction = Literal["add", "update", "delete"]


@dataclass(slots=True)
class _PatchOperation:
    """解析后的单文件补丁操作，暂存到内存后再统一写入。"""

    action: PatchAction
    path: str
    lines: list[str]


class ApplyPatchTool:
    """解析常见的 Codex 风格补丁，并在校验所有文件后再开始写入。

    先计算全部文件变化、确认每个路径和上下文都有效，再进入写入循环；这样补丁格式
    或路径错误会在任何文件改变前暴露给模型。
    """

    name = "apply_patch"
    description = "Apply a structured text patch to files inside the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "A *** Begin Patch / *** End Patch formatted patch",
            }
        },
        "required": ["patch"],
        "additionalProperties": False,
    }
    timeout_seconds: float | None = 20.0
    capabilities = ToolCapabilities(
        read_only=False,
        destructive=True,
        open_world=False,
        concurrency_safe=False,
        requires_approval=False,
    )

    def is_concurrency_safe(self, arguments: dict[str, Any]) -> bool:
        """补丁可能同时修改多个文件，必须在工具调度中形成独占屏障。"""

        return False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """解析、预览并应用所有文件操作，返回变更文件列表而不是完整文件内容。"""

        if context.permission_mode != "workspace-write":
            raise ToolError("apply_patch requires workspace-write mode")
        FileTransaction.recover_pending(workspace_root(context))
        operations = self._parse(arguments["patch"])
        changes: list[tuple[Path, bytes | None]] = []
        delete_targets: list[Path] = []
        created_directories: list[Path] = []
        for operation in operations:
            path = safe_path(operation.path, context)
            if operation.action == "add":
                if path.exists():
                    raise ToolError(f"cannot add existing file: {operation.path}")
                content = self._added_content(operation.lines)
                cursor = path.parent
                while not cursor.exists():
                    created_directories.append(cursor)
                    cursor = cursor.parent
            elif operation.action == "delete":
                if not path.is_file():
                    raise ToolError(f"cannot delete missing file: {operation.path}")
                delete_targets.append(path)
                continue
            else:
                if not path.is_file():
                    raise ToolError(f"cannot update missing file: {operation.path}")
                try:
                    original = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise ToolError(f"cannot read {operation.path}: {exc}") from exc
                content = self._apply_update(original, operation.lines, operation.path)
            changes.append((path, None if content is None else content.encode("utf-8")))

        engine = context.delete_policy
        if not isinstance(engine, DeletePolicyEngine):
            engine = DeletePolicyEngine(
                workspace=workspace_root(context),
                manifest=context.task_manifest,
                approval_service=context.approval_service,
            )
        delete_decisions: list[DeleteDecision] = []
        stored = context.metadata.get("__delete_decisions__", {})
        for path in delete_targets:
            decision = (
                stored.get(str(path.expanduser().resolve())) if isinstance(stored, dict) else None
            )
            if not isinstance(decision, DeleteDecision):
                decision = await engine.authorize(
                    path,
                    context=context,
                    call_id=context.session_id,
                    tool_name=self.name,
                    arguments=arguments,
                )
            if not decision.allowed:
                raise ToolError(decision.reason)
            delete_decisions.append(decision)

        if changes:
            FileTransaction(workspace_root(context), changes).commit()
        for decision in delete_decisions:
            engine.execute(
                decision,
                task_id=str(context.session_id),
                context=context,
            )

        manifest = context.task_manifest
        if isinstance(manifest, TaskFileManifest):
            for operation in operations:
                if operation.action == "add":
                    path = safe_path(operation.path, context)
                    manifest.record_created(path)
            for directory in reversed(created_directories):
                manifest.record_generated_dir(directory)
        return {
            "files": [operation.path for operation in operations],
            "changed_files": len(changes) + len(delete_targets),
            "workspace": str(workspace_root(context)),
        }

    @staticmethod
    def _parse(patch: str) -> list[_PatchOperation]:
        """解析 ``*** Begin/End Patch`` 和每个文件操作的边界。"""

        lines = patch.splitlines()
        if lines and lines[0].strip() == "*** Begin Patch":
            lines = lines[1:]
        if lines and lines[-1].strip() == "*** End Patch":
            lines = lines[:-1]
        operations: list[_PatchOperation] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            matched: tuple[PatchAction, str] | None = None
            for prefix, action in (
                ("*** Add File: ", "add"),
                ("*** Update File: ", "update"),
                ("*** Delete File: ", "delete"),
            ):
                if line.startswith(prefix):
                    matched = (cast(PatchAction, action), line.removeprefix(prefix).strip())
                    break
            if matched is None:
                if not line.strip():
                    index += 1
                    continue
                raise ToolError(f"unsupported patch line: {line}")
            action, path = matched
            index += 1
            body: list[str] = []
            while index < len(lines) and not lines[index].startswith("*** "):
                body.append(lines[index])
                index += 1
            operations.append(_PatchOperation(action, path, body))
        if not operations:
            raise ToolError("patch contains no file operations")
        return operations

    @staticmethod
    def _added_content(lines: list[str]) -> str:
        """把以 ``+`` 开头的新增文件行还原成带结尾换行的文本。"""

        content: list[str] = []
        for line in lines:
            if not line.startswith("+"):
                raise ToolError("added file patch lines must start with +")
            content.append(line[1:])
        return "\n".join(content) + ("\n" if content else "")

    @staticmethod
    def _apply_update(original: str, patch_lines: list[str], path: str) -> str:
        """按每个 ``@@`` hunk 的上下文定位原文并生成更新后的文本。"""

        lines = original.splitlines()
        hunks: list[list[str]] = []
        current: list[str] | None = None
        for line in patch_lines:
            if line.startswith("@@"):
                current = []
                hunks.append(current)
            elif current is not None:
                if not line or line[0] in {" ", "+", "-"}:
                    current.append(line)
                elif line.startswith("\\ No newline"):
                    continue
                else:
                    raise ToolError(f"unsupported update line in {path}: {line}")
            elif line.strip():
                raise ToolError(f"update patch for {path} must contain a @@ hunk")
        if not hunks:
            raise ToolError(f"update patch for {path} contains no @@ hunk")
        cursor = 0
        for hunk in hunks:
            old_lines = [line[1:] for line in hunk if line.startswith((" ", "-"))]
            new_lines = [line[1:] for line in hunk if line.startswith((" ", "+"))]
            position = ApplyPatchTool._find_sequence(lines, old_lines, cursor)
            if position is None:
                raise ToolError(f"patch context not found in {path}")
            lines[position : position + len(old_lines)] = new_lines
            cursor = position + len(new_lines)
        return "\n".join(lines) + ("\n" if original.endswith("\n") else "")

    @staticmethod
    def _find_sequence(lines: list[str], wanted: list[str], start: int) -> int | None:
        """从 start 开始查找连续上下文，避免把同一旧文本错配到前一个 hunk。"""

        if not wanted:
            return start
        end = len(lines) - len(wanted) + 1
        for position in range(start, end):
            if lines[position : position + len(wanted)] == wanted:
                return position
        return None
